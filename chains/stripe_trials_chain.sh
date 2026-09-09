#!/bin/zsh
cd "$(dirname "$0")/.."
while ! grep -q '"2025"' stats/striping/summary.json 2>/dev/null; do sleep 10; done
echo "=== score done $(date)"
PICKS=$(uv run python - <<'PY'
import json, numpy as np
out = []
for y in [2005, 2008, 2012, 2016, 2020, 2023]:
    b = [x for x in json.load(open(f"stats/striping/{y}.json")) if x["mean_count"] >= 4]
    nr = np.array([x["nir_ratio"] for x in b])
    k = int(np.argsort(nr)[int(0.9 * len(nr))])
    row, col = b[k]["by"] * 2, b[k]["bx"] * 2
    tile = f"r{row // 4096:02d}c{col // 4096:02d}"
    sub = (row % 4096) // 2048 * 2 + (col % 4096) // 2048
    print(f"{tile} {y} {sub} {b[k]['nir_ratio']}")
PY
)
echo "$PICKS"
echo "$PICKS" | while read tile year sub ratio; do
  echo "=== trial $tile $year sub $sub (pyramid nir_ratio $ratio) $(date)"
  uv run python src/landsat_mosaic/stripe_trials.py $tile $year --only $sub 2>&1 | grep -v -E "Warn|warn|^\s*$"
  if [ "$year" = "2012" ]; then
    echo "=== trial $tile 2012 sub $sub span 1 $(date)"
    uv run python src/landsat_mosaic/stripe_trials.py $tile 2012 --only $sub --span 1 2>&1 | grep -v -E "Warn|warn|^\s*$"
  fi
done
echo "=== trials done $(date)"
