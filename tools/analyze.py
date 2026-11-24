#!/usr/bin/env python3
"""判红分析器：读每局对局 JSON（仿真跑量批次产物），输出聚合报告与门禁判红退出码。

输出：
  ① 胜率矩阵（Wilson 95% 置信区间）
  ② 对局体检
  ③ 逐将/技能读数
  ④ 异常清单
  ⑤ 策略×牌堆×对阵分组
  ⑥ 同对阵下策略间显著性（≥50 局，|Δwin| > Wilson 重叠区）

用法：
  python3 tools/analyze.py [对局JSON目录] [--out FILE] [--exclude-fallback|--include-fallback]
随仓样例（本文档判红规则可直接跑出来验证）：
  python3 tools/analyze.py data/sample-games --exit-code --lineups data/sample-lineups.json --coverage warn
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

DEFAULT_DIR = os.path.join("data", "sample-games")
WIN_LO, WIN_HI = 45.0, 55.0
TURN_LO, TURN_HI = 5, 10
DRAW_MAX = 10.0
SIGNIFICANCE_MIN_N = 50
FALLBACK_EFFECTIVE = "v2_fallback"


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = p / 100.0
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return ((center - half) * 100, (center + half) * 100)


def wilson_overlap_width(lo1: float, hi1: float, lo2: float, hi2: float) -> float:
    """两 Wilson 区间重叠部分宽度（无重叠则为 0）。"""
    return max(0.0, min(hi1, hi2) - max(lo1, lo2))


def compare_win_rates(w1: int, n1: int, w2: int, n2: int) -> dict:
    """同口径先手 P1 胜率对比（显著性：n≥50 且 |Δ| > overlap）。"""
    p1 = w1 / n1 * 100 if n1 else 0.0
    p2 = w2 / n2 * 100 if n2 else 0.0
    lo1, hi1 = wilson(p1, n1)
    lo2, hi2 = wilson(p2, n2)
    delta = abs(p1 - p2)
    overlap = wilson_overlap_width(lo1, hi1, lo2, hi2)
    hard_n = min(n1, n2) >= SIGNIFICANCE_MIN_N
    significant = hard_n and delta > overlap
    return {
        "p1_pct_a": p1,
        "p1_pct_b": p2,
        "delta": delta,
        "overlap": overlap,
        "n_a": n1,
        "n_b": n2,
        "ci_a": (lo1, hi1),
        "ci_b": (lo2, hi2),
        "significant": significant,
        "directional_only": not hard_n and n1 >= 10 and n2 >= 10,
    }


def load_games(directory: str) -> list[dict]:
    games = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        base = os.path.basename(path)
        if base in ("progress.json", "run-meta.json"):
            continue
        with open(path, encoding="utf-8") as f:
            games.append(json.load(f))
    return games


def filter_games(games: list[dict], exclude_fallback: bool) -> tuple[list[dict], dict[str, int]]:
    stats = {"total": len(games), "kept": 0, "excluded_fallback": 0}
    if not exclude_fallback:
        stats["kept"] = len(games)
        return games, stats
    kept: list[dict] = []
    for g in games:
        if g.get("policy_effective") == FALLBACK_EFFECTIVE:
            stats["excluded_fallback"] += 1
            continue
        kept.append(g)
    stats["kept"] = len(kept)
    return kept, stats


def cohort_key(g: dict) -> tuple[str, str, str, str]:
    policy = g.get("policy_effective") or g.get("policy_requested") or "v2"
    deck = g.get("deck_p1_sha256") or "-"
    deck2 = g.get("deck_p2_sha256") or "-"
    deck_tag = deck if deck != "-" else "lineup"
    return (g.get("matchup", "?"), policy, deck_tag, deck2 if deck2 != "-" else "")


def fmt_ci(wins: int, n: int) -> str:
    if n == 0:
        return "—"
    p = wins / n * 100
    lo, hi = wilson(p, n)
    return f"{p:.1f}% [{lo:.0f}–{hi:.0f}%]"


def build_report(directory: str, games: list[dict], filter_stats: dict[str, int],
                 focus: str | None = None, opponent: str | None = None) -> str:
    cell: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    cohort_wins: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])

    for g in games:
        a, b = g["lineup_p1"], g["lineup_p2"]
        w = g["winner"]
        c = cell[(a, b)]
        c[2] += 1
        if w == "P1":
            c[0] += 1
        elif w == "draw":
            c[1] += 1

        ck = cohort_key(g)
        cw = cohort_wins[ck]
        cw[2] += 1
        if w == "P1":
            cw[0] += 1
        elif w == "draw":
            cw[1] += 1

    slugs = sorted({s for k in cell for s in k})

    lines: list[str] = []
    lines.append(f"# 仿真批次分析（{len(games)} 局，来源 {directory}）\n")
    if filter_stats["excluded_fallback"]:
        lines.append(
            f"> 口径：已剔除 `policy_effective={FALLBACK_EFFECTIVE}` "
            f"{filter_stats['excluded_fallback']}/{filter_stats['total']} 局\n"
        )

    lines.append("## 一、胜率矩阵（行=先手位阵容，单元格=先手位胜率，含 Wilson 95% 置信区间）\n")
    header = "| " + " | ".join([""] + slugs) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(slugs) + 1))
    for a in slugs:
        row = [a]
        for b in slugs:
            w, _d, n = cell[(a, b)]
            if n == 0:
                row.append("—")
            elif a == b:
                row.append(f"镜像·先手 {fmt_ci(w, n)}")
            else:
                row.append(fmt_ci(w, n))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    turns = [g["turn_count"] for g in games]
    draws = sum(1 for g in games if g["winner"] == "draw")
    all_dead = sum(1 for g in games if g["end_reason"] == "all_dead")
    first_p1 = sum(1 for g in games if g["first_player"] == "P1")
    p1_win_when_first = sum(1 for g in games if g["first_player"] == "P1" and g["winner"] == "P1")
    lines.append("## 二、对局体检\n")
    lines.append(
        f"- 平均回合数：{sum(turns) / len(turns):.2f}（健康带 {TURN_LO}–{TURN_HI}）；"
        f"最短 {min(turns)} / 最长 {max(turns)}"
    )
    hist = defaultdict(int)
    for t in turns:
        hist[t] += 1
    lines.append("- 回合分布：" + "，".join(f"{k}回合×{hist[k]}" for k in sorted(hist)))
    lines.append(f"- 平局率：{draws / len(games) * 100:.1f}%（{draws}/{len(games)}，上限 {DRAW_MAX}%）")
    lines.append(f"- 终局原因：all_dead {all_dead} 局 / max_turns {len(games) - all_dead} 局")
    if first_p1:
        lines.append(
            f"- P1 先手局 P1 胜率：{p1_win_when_first / first_p1 * 100:.1f}%"
            f"（{p1_win_when_first}/{first_p1}，先手偏差体检）"
        )
    fb_games = [g for g in games if g.get("fallback_steps", 0) > 0]
    if fb_games:
        total_fb = sum(g.get("fallback_steps", 0) for g in fb_games)
        lines.append(
            f"- L1 fallback 步数：{total_fb} 步 / {len(fb_games)} 局含 fallback（未剔除时可见）"
        )
    lines.append("")

    heroAgg: dict[tuple[str, int], dict[str, float]] = defaultdict(
        lambda: {
            "n": 0,
            "dmg": 0.0,
            "taken": 0.0,
            "heal": 0.0,
            "shield": 0.0,
            "casts": 0.0,
            "surv": 0.0,
            "c1": 0,
            "c2": 0,
            "c3": 0,
            "cap": 0,
        }
    )
    for g in games:
        for _hid, h in g["heroes"].items():
            key = (g["lineup_p1"] if h["side"] == "P1" else g["lineup_p2"], h["hero_id"])
            a = heroAgg[key]
            a["n"] += 1
            a["dmg"] += h["damage"]
            a["taken"] += h["taken"]
            a["heal"] += h["heal"]
            a["shield"] += h["shield_granted"]
            a["casts"] += sum(h["casts"].values())
            a["surv"] += h["death_round"] if h["death_round"] else g["turn_count"]
            a["cap"] += h["death_round"] if h["death_round"] else g["turn_count"]
            a["c1"] += h["casts"].get("1", 0)
            a["c2"] += h["casts"].get("2", 0)
            a["c3"] += h["casts"].get("3", 0)
    lines.append("## 三、逐将读数（场均，按出场局）\n")
    lines.append("| 阵容 | 武将 | 场均输出 | 场均承伤 | 场均治疗 | 场均上盾 | 场均施放 | 场均存活回合 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    names = {}
    for g in games:
        for h in g["heroes"].values():
            names[h["hero_id"]] = h["name"]
    for (slug, hid), a in sorted(heroAgg.items()):
        n = a["n"]
        lines.append(
            f"| {slug} | {names.get(hid, hid)} | {a['dmg'] / n:.1f} | {a['taken'] / n:.1f} "
            f"| {a['heal'] / n:.1f} | {a['shield'] / n:.1f} | {a['casts'] / n:.2f} "
            f"| {a['surv'] / n:.1f} |"
        )
    lines.append("")

    lines.append("## 三b、技能触发率（实际施放 ÷ 存活回合数上限；每回合每将至多一招）\n")
    lines.append("| 阵容 | 武将 | 合计触发率 | Lv1 | Lv2 | Lv3 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|")
    for (slug, hid), a in sorted(heroAgg.items()):
        cap = a["cap"]
        if cap == 0:
            continue
        total_rate = a["casts"] / cap * 100
        lv = lambda c: f"{c / cap * 100:.0f}%" if cap else "—"
        verdict = ""
        if total_rate < 50:
            verdict = "⚠️ 偏低（<50%，配方太难候选）"
        elif total_rate > 100:
            verdict = "❌ 超上限（数据异常，须查）"
        lines.append(
            f"| {slug} | {names.get(hid, hid)} | {total_rate:.0f}% "
            f"| {lv(a['c1'])} | {lv(a['c2'])} | {lv(a['c3'])} | {verdict} |"
        )
    lines.append("")

    lines.append("## 四、异常清单（P0=胜率出带且 CI 压过 50%）\n")
    anomalies: list[str] = []
    for (a, b), (w, _d, n) in sorted(cell.items()):
        if a == b:
            p = w / n * 100 if n else 50
            lo, _ = wilson(p, n)
            if lo > 55:
                anomalies.append(f"P0 先手优势过大：镜像 {a} 先手胜率 {p:.1f}%（CI 下限 {lo:.0f}% > 55%）")
            continue
        p = w / n * 100 if n else 50
        lo, hi = wilson(p, n)
        if p > WIN_HI and lo > 50:
            anomalies.append(f"P0 {a} 打 {b} 胜率 {p:.1f}% [{lo:.0f}–{hi:.0f}%]：显著过强")
        elif p < WIN_LO and hi < 50:
            anomalies.append(f"P0 {a} 打 {b} 胜率 {p:.1f}% [{lo:.0f}–{hi:.0f}%]：显著过弱")
        elif p > WIN_HI or p < WIN_LO:
            anomalies.append(f"观察 {a} 打 {b} 胜率 {p:.1f}% 出带但 CI 未压 50%：样本加压再判")
    avg_turn = sum(turns) / len(turns)
    if avg_turn < TURN_LO or avg_turn > TURN_HI:
        anomalies.append(f"观察 平均回合 {avg_turn:.2f} 出健康带 [{TURN_LO},{TURN_HI}]")
    if draws / len(games) * 100 >= DRAW_MAX:
        anomalies.append(f"观察 平局率 {draws / len(games) * 100:.1f}% ≥ {DRAW_MAX}%")
    for (slug, hid), a in sorted(heroAgg.items()):
        if a["cap"] and a["casts"] / a["cap"] < 0.5:
            anomalies.append(
                f"P1 {slug}·{names.get(hid, hid)} 技能触发率 "
                f"{a['casts'] / a['cap'] * 100:.0f}% < 50%（配方太难候选，§9）"
            )
        if a["n"] and a["surv"] / a["n"] < 3:
            anomalies.append(
                f"观察 {slug}·{names.get(hid, hid)} 平均存活 {a['surv'] / a['n']:.1f} 回合 < 3（查生存/站位，§9）"
            )
    if not anomalies:
        lines.append("（无出带项——首轮基线在健康带内）")
    for a_ in anomalies:
        lines.append(f"- {a_}")
    lines.append("")

    lines.append("## 五、分组摘要（matchup × policy × deck_sha）\n")
    lines.append("| 对阵 | policy | deck_p1 | n | P1 胜率 |")
    lines.append("|---|---|---|---|---|")
    for ck in sorted(cohort_wins.keys()):
        matchup, policy, deck1, _deck2 = ck
        w, _d, n = cohort_wins[ck]
        deck_show = deck1 if len(deck1) <= 12 else deck1[:12] + "…"
        lines.append(f"| {matchup} | {policy} | {deck_show} | {n} | {fmt_ci(w, n)} |")
    lines.append("")

    lines.append("## 六、策略对比显著性（同对阵、不同 policy；硬结论 n≥50）\n")
    by_matchup: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for ck in cohort_wins:
        by_matchup[ck[0]].append(ck)

    sig_rows: list[str] = []
    for matchup, keys in sorted(by_matchup.items()):
        policies = {k[1]: cohort_wins[k] for k in keys}
        pol_list = sorted(policies.keys())
        for i in range(len(pol_list)):
            for j in range(i + 1, len(pol_list)):
                pa, pb = pol_list[i], pol_list[j]
                wa, _da, na = policies[pa]
                wb, _db, nb = policies[pb]
                cmp = compare_win_rates(wa, na, wb, nb)
                tag = "significant: true" if cmp["significant"] else (
                    "directional" if cmp["directional_only"] else "insufficient_n"
                )
                lo_a, hi_a = cmp["ci_a"]
                lo_b, hi_b = cmp["ci_b"]
                sig_rows.append(
                    f"- **{matchup}** `{pa}` vs `{pb}`：Δ={cmp['delta']:.1f}pp，"
                    f"overlap={cmp['overlap']:.1f}pp，"
                    f"A={cmp['p1_pct_a']:.1f}% [{lo_a:.0f}–{hi_a:.0f}] (n={na})，"
                    f"B={cmp['p1_pct_b']:.1f}% [{lo_b:.0f}–{hi_b:.0f}] (n={nb}) → **{tag}**"
                )
    if sig_rows:
        lines.extend(sig_rows)
    else:
        lines.append("（仅单一 policy 或无对比组——合并多批 JSON 或再跑另一 policy 后可对比）")
    lines.append("")

    lines.append(
        "> 口径注：击杀归因（kills）与伤害溢出率暂缺；显著性规则：|Δwin| > Wilson 重叠宽度。"
    )

    # 三c：focus 下钻逐卡读数（2026-11-22 重锚轮工具）——从「P0 出带」到「改哪张卡」的映射证据：
    # 赢家侧超模卡/输家侧零贡献卡在此可见。击杀数不可信（归因缺口），伤害/出牌/挂层可信。
    if focus:
        agg: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"slug": focus, "plays": 0, "damage": 0, "heal": 0, "shield": 0, "stacks": 0, "games": set()}
        )
        for g in games:
            p1, p2 = g["lineup_p1"], g["lineup_p2"]
            for key, b in (g.get("cards") or {}).items():
                side, _sep, cid = key.partition(":")
                slug = p1 if side == "P1" else p2
                if slug != focus:
                    continue
                a = agg[(slug, cid)]
                a["plays"] += b.get("plays", 0)
                a["damage"] += b.get("damage", 0)
                a["heal"] += b.get("heal", 0)
                a["shield"] += b.get("shield", 0)
                st = b.get("stacks") or {}
                a["stacks"] += sum(st.values()) if isinstance(st, dict) else 0
                a["games"].add(g.get("game_id"))
        lines.append(f"## 三c、逐卡读数（focus={focus}" + (f"，对手限 {opponent}" if opponent else "") + "；击杀数因归因缺口不可信）\n")
        lines.append("| 阵容 | cardId | 出场局 | 出牌 | 总伤害 | 总治疗 | 总上盾 | 挂层合计 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for (slug, cid), a in sorted(agg.items(), key=lambda kv: -kv[1]["damage"]):
            lines.append(
                f"| {slug} | {cid} | {len(a['games'])} | {a['plays']} | {a['damage']} "
                f"| {a['heal']} | {a['shield']} | {a['stacks']} |"
            )
        lines.append("")
    return "\n".join(lines)



# ═══════════ 判红模式（2026-11-22 0b）：退出码优先级 5>2>3>4>0 ═══════════

COVERAGE_WARN_BANNER = (
    "⚠️ COVERAGE WARN: L1/L2/L3 覆盖账本未建（阶段 1–4 未落地），本次不判覆盖——缺口可见但不判红"
)


def load_anomalies(directory: str) -> tuple[list[dict], int]:
    """读 anomalies.jsonl；severity 缺失/非法按 hard 计（fail-closed，旧批次无该字段同理）。"""
    path = os.path.join(directory, "anomalies.jsonl")
    rows: list[dict] = []
    hard = 0
    if not os.path.exists(path):
        return rows, hard
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                hard += 1
                continue
            sev = row.get("severity")
            if sev not in ("hard", "quality"):
                hard += 1  # 旧格式/坏行 fail-closed
            elif sev == "hard":
                hard += 1
            rows.append(row)
    return rows, hard


def load_progress_failed(directory: str) -> int:
    """progress.json 的 failed 数；文件损坏返回 -1（环境失败信号）。"""
    path = os.path.join(directory, "progress.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("failed", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return -1


def load_lineup_status(path: str) -> dict[str, str]:
    """slug → status（缺字段按 experimental：无标记不享受 P0 判红，fail-closed 反向）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        l.get("slug", ""): l.get("status", "experimental")
        for l in data.get("lineups", [])
    }


def has_baseline_p0(games: list[dict], status_by_slug: dict[str, str]) -> bool:
    """退出码 3 门槛：仅当对阵两侧阵容均 status=baseline（任一侧 experimental 只写报告）。"""
    cell_stats: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for g in games:
        a, b = g["lineup_p1"], g["lineup_p2"]
        w = g["winner"]
        c = cell_stats[(a, b)]
        c[2] += 1
        if w == "P1":
            c[0] += 1
        elif w == "draw":
            c[1] += 1
    for (a, b), (w, _d, n) in cell_stats.items():
        if not (status_by_slug.get(a) == "baseline" and status_by_slug.get(b) == "baseline"):
            continue
        if n == 0:
            continue
        p = w / n * 100
        lo, hi = wilson(p, n)
        if a == b:
            if lo > 55:
                return True
            continue
        if (p > WIN_HI and lo > 50) or (p < WIN_LO and hi < 50):
            return True
    return False


def _finish(code: int, reason: str, report: str | None, out_path: str | None) -> None:
    if report is not None:
        print(report)
        if out_path:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(report + "\n")
    print(f"[exit-code] {code} {reason}")
    sys.exit(code)


def run_exit_code_mode(args: argparse.Namespace, raw: list[dict]) -> None:
    # 5 环境失败：一个 JSON 都没有，或 progress.json 损坏——连"跑没跑成"都无法判定
    progress_failed = load_progress_failed(args.directory)
    if not raw or progress_failed < 0:
        _finish(5, f"环境失败：无对局 JSON 或 progress.json 损坏（{args.directory}）", None, None)
    games, filter_stats = filter_games(raw, not args.include_fallback)
    if not games:
        _finish(5, f"环境失败：剔除 fallback 后无对局（原 {filter_stats['total']} 局）", None, None)

    report = build_report(args.directory, games, filter_stats)

    # 2 硬异常：局崩溃（progress.failed）+ anomalies hard 行（对账/审计/生命周期；旧格式 fail-closed）
    _rows, hard_count = load_anomalies(args.directory)
    gap_games = len({r.get('game_id') for r in _rows if r.get('source') == 'attribution_gap'})
    hard_total = hard_count + max(progress_failed, 0)
    if hard_total > 0:
        _finish(2, f"硬异常：anomalies hard {hard_count} 行 + progress.failed "
                   f"{max(progress_failed, 0)} 局", report, args.out)

    # 3 P0 平衡：仅对阵两侧均 baseline 时判红；experimental 的 P0 只在报告（当前全 experimental=死代码，
    #   直至重锚轮产出 v5-baseline——拍板记录见 sim-coverage-plays-distribution-2026-11-22.md §6）
    status_by_slug = load_lineup_status(args.lineups)
    if has_baseline_p0(games, status_by_slug):
        _finish(3, "P0 平衡异常（baseline 阵容出带且 CI 压线）", report, args.out)

    # 4 覆盖不达标：仅 strict 档（warn=可见不判红；账本未建=阶段 1–4 未落地）
    if args.coverage == "strict" and not os.path.exists(args.ledger):
        banner = f"❌ COVERAGE STRICT: 覆盖账本未建（{args.ledger}），判覆盖不达标"
        _finish(4, f"覆盖不达标：strict 档且账本未建（{args.ledger}）", banner + "\n\n" + report, args.out)

    # 0 绿：warn 高亮块置报告头 + 单独打印一行（CI nightly job summary 直接抓这一行）
    banner = COVERAGE_WARN_BANNER if args.coverage == "warn" else None
    if banner:
        print(banner)
        report = banner + "\n\n" + report
    baseline_n = sum(1 for v in status_by_slug.values() if v == "baseline")
    gap_note = f"；归因缺口 {gap_games} 局（quality，不判红）" if gap_games else ""
    _finish(0, f"绿（硬异常 0；P0 判红门槛=双方 baseline，当前 baseline 阵容数 {baseline_n}{gap_note}）",
            report, args.out)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SimMatchRunner 批次分析")
    parser.add_argument("directory", nargs="?", default=DEFAULT_DIR, help="对局 JSON 目录")
    parser.add_argument("--out", help="报告落盘路径")
    parser.add_argument(
        "--include-fallback",
        action="store_true",
        help="保留 policy_effective=v2_fallback 局（默认剔除）",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help=("判红模式（0b）：以退出码表达门禁结论，优先级 5>2>3>4>0。"
              "5=环境失败 2=硬异常（崩溃/对账/审计/生命周期） 3=P0 平衡 4=覆盖不达标 0=绿。"
              "注意：退出码 3 仅对阵双方均 status=baseline 时触发；experimental 阵容的 P0 只写报告不判红"
              "（当前五套全 experimental，码 3 为死代码，直至重锚轮产出 v5-baseline）。"),
    )
    parser.add_argument(
        "--coverage",
        choices=["off", "warn", "strict"],
        default="warn",
        help="覆盖判定档位：off=不判；warn=报告头高亮提示、退出码仍 0；strict=无覆盖账本即退出码 4（账本落地前勿用 strict）",
    )
    parser.add_argument("--lineups", default="../design/balance/sim-lineups.json",
                        help="阵容文件（读 status 字段，供退出码 3 判定）")
    parser.add_argument("--ledger", default="data/coverage-l4.json",
                        help="L4 覆盖账本路径（strict 档必需，阶段 1–4 后才有）")
    parser.add_argument("--focus", help="下钻：只统计涉及该 slug 的对局（重锚诊断用；与 --exit-code 互斥）")
    parser.add_argument("--opponent", help="下钻：再限定对手 slug（须与 --focus 同用）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exclude_fallback = not args.include_fallback

    raw = load_games(args.directory)
    if (args.focus or args.opponent) and args.exit_code:
        print("--focus/--opponent 是诊断下钻工具，与 --exit-code 判红互斥（防止过滤后的局部样本被判红）")
        sys.exit(2)
    if args.opponent and not args.focus:
        print("--opponent 须与 --focus 同用")
        sys.exit(2)
    if args.focus:
        raw = [g for g in raw if args.focus in (g.get("lineup_p1"), g.get("lineup_p2"))]
    if args.opponent:
        raw = [g for g in raw if args.opponent in (g.get("lineup_p1"), g.get("lineup_p2"))]
    if args.exit_code:
        run_exit_code_mode(args, raw)
        return
    if not raw:
        print(f"没有读到对局 JSON：{args.directory}")
        sys.exit(1)

    games, filter_stats = filter_games(raw, exclude_fallback)
    if not games:
        print(f"剔除 fallback 后无对局（原 {filter_stats['total']} 局）")
        sys.exit(1)

    report = build_report(args.directory, games, filter_stats, focus=args.focus, opponent=args.opponent)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n[analyze] 报告已落盘 {args.out}")


if __name__ == "__main__":
    main()
