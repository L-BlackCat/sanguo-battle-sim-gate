# 注：本脚本保留自真实项目。第 2 步跑量依赖未开源的战斗引擎（Java 侧）；
# 判红步骤（analyze.py）随本仓开源，配 data/ 样例可直接复现判红行为。
# 保留此脚本用于展示编排形态：批次哈希目录 / 判红交接 / CI 预留。
#!/usr/bin/env bash
# 全量仿真门禁（B4）：5 阵容全矩阵 × 20 局 = 500 局（policy v2 默认；旧称 L0 门禁，2026-11-20 更名）
# 用法：run_sim_gate.sh [seed] [parallel] [iterations]（默认 420000/8/20；重锚升格验证用 iterations=50）
# 输出目录带阵容内容哈希（2026-11-22 0a）：阵容任何改动=新目录新批次，断点续跑/analyze 永不混批
# 批次哈希边界（2026-11-22 立档）：跨批次不作逐字节对比——旧无哈希目录与新目录 game_id 不对齐
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export JAVA_HOME="${JAVA_HOME:-$(/usr/libexec/java_home -v 17 2>/dev/null || true)}"
LINEUPS="$ROOT/design/balance/sim-lineups.json"
HASH8="$(shasum -a 256 "$LINEUPS" | awk '{print substr($1, 1, 8)}')"
OUT="logs/sim-gate-500-$HASH8"
echo "[gate] 输出目录 ${OUT}（阵容 sha256 前 8 位 ${HASH8}）"
cd "$ROOT/server"
mvn -q test-compile dependency:build-classpath -Dmdep.outputFile=target/sim-cp.txt
CP="target/classes:target/test-classes:$(cat target/sim-cp.txt)"
# 0b 判红自动化：runner 退出码仅供日志，判定权统一在 analyze --exit-code（优先级 5>2>3>4>0）
set +e
java -cp "$CP" com.sanguocard.game.sim.SimMatchRunner \
  --matrix --iterations "${3:-20}" --seed "${1:-420000}" \
  --out "$OUT" --txt-every 0 --parallel "${2:-8}"
RUNNER_RC=$?
set -e
# bash 3.2 坑（macOS 自带版）：$VAR 后紧跟全角字符会被并进变量名，一律 ${VAR} 花括号隔离
echo "[gate] 跑量退出码 ${RUNNER_RC}（2=有局崩溃；最终判定见 analyze）"
# CI 接入（阶段 3.5）时：nightly job summary 单独抓 analyze 输出的 ⚠️ COVERAGE WARN 行
python3 "$ROOT/tools/sim/analyze.py" "$OUT" \
  --exit-code --coverage warn \
  --lineups "$ROOT/design/balance/sim-lineups.json" \
  --ledger "$ROOT/design/balance/coverage-l4.json"
