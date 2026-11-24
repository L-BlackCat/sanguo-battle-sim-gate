#!/usr/bin/env bash
# 0b 验收工具（A2）：校验 anomalies.jsonl 每行 severity ∈ {hard, quality}。
# 用法：tools/sim/check_anomalies_schema.sh <anomalies.jsonl 路径>
#      文件不存在=该批无异常行，视为通过。
set -euo pipefail
FILE="${1:-}"
if [[ -z "$FILE" || ! -f "$FILE" ]]; then
  echo "[check_anomalies_schema] 无 anomalies 文件（${FILE}）——无异常行，通过"
  exit 0
fi
python3 - "$FILE" <<'PYEOF'
import json, sys
bad = total = 0
for i, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    line = line.strip()
    if not line:
        continue
    total += 1
    try:
        sev = json.loads(line).get("severity")
    except json.JSONDecodeError:
        sev = None
    if sev not in ("hard", "quality"):
        bad += 1
        print(f"  行 {i}: severity 缺失或非法")
print(f"[check_anomalies_schema] {total} 行，{bad} 行违规")
sys.exit(1 if bad else 0)
PYEOF
