#!/usr/bin/env python3
"""prune_strokes.py — 短いストロークを除去する (remove very short strokes).

Usage:
    python prune_strokes.py strokes.json MAX_POINTS out.json

点数が MAX_POINTS 以下のストロークを取り除いた JSON を書く。
幾何には触れない (残すストロークの点はそのまま)。

注意: 除去はチェイン化 (chain_strokes.py) の後に行うのが安全。
未チェインの断片には「長い線の一部」や目の刻みなどの実細部が多く、
先に捨てると復元が目に見えて劣化する (README の除去実験参照)。
チェイン後に残る短いストロークは太いインク内部の冗長な棘が中心で、
≤5点まで除去しても復元 IoU はほぼ変わらない。
"""

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    src, n, out = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    d = json.loads(src.read_text())
    keep = [s for s in d["strokes"] if len(s["points"]) > n]
    gone = [s for s in d["strokes"] if len(s["points"]) <= n]
    d["strokes"] = keep
    for i, s in enumerate(keep):
        s["id"] = i
    out.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    print(
        f"{src.name} <={n}pts: removed {len(gone)} strokes / "
        f"{sum(len(s['points']) for s in gone)} pts, kept {len(keep)} strokes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
