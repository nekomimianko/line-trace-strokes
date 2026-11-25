#!/usr/bin/env python3
"""半径フィルタ：大きすぎる半径の点を「線ではない」として除去する。

中心線＋半径の表現では、黒ベタ領域の内部が「ごく短い骨格＋巨大な半径」
に縮退する。半径が明らかに大きい点は線の標本ではなくベタの足場なので、
取り除く。除去した点の位置でストロークは分割される。

しきい値は画像サイズに対する相対指定（--max-r-rel、既定 0.01 ＝
長い辺の 1%）か、px の絶対指定（--max-r）で与える。ベタ領域そのものは
このフィルタでは扱わない（残ったインクがベタ抽出の対象になる）。

使い方:
    python filter_radius.py strokes.json -o filtered.json
    python filter_radius.py strokes.json -o filtered.json --max-r 12
"""

import argparse
import json
import sys
from pathlib import Path


def filter_strokes(strokes, max_r):
    """半径 max_r を超える点を除去し、残りを連続ランごとに分割する。"""
    out = []
    removed = 0
    for s in strokes:
        pts = s["points"]
        if len(pts) == 1:
            if pts[0]["r"] <= max_r:
                out.append(s)
            else:
                removed += 1
            continue
        run = []
        for p in pts:
            if p["r"] <= max_r:
                run.append(p)
            else:
                removed += 1
                if len(run) >= 2:
                    out.append({**s, "points": run})
                elif run:
                    removed += len(run)
                run = []
        if len(run) >= 2:
            out.append({**s, "points": run})
        elif run:
            removed += len(run)
    return out, removed


def main():
    ap = argparse.ArgumentParser(description="Remove points whose radius is too large to be a line")
    ap.add_argument("json_file")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--max-r", type=float, default=None,
                    help="absolute radius threshold in px")
    ap.add_argument("--max-r-rel", type=float, default=0.01,
                    help="radius threshold as a fraction of the longer image side (default 0.01)")
    args = ap.parse_args()

    data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    strokes = data["strokes"]
    if args.max_r is not None:
        max_r = args.max_r
    else:
        info = data["image"]
        max_r = args.max_r_rel * max(info["width"], info["height"])

    n_before = sum(len(s["points"]) for s in strokes)
    filtered, removed = filter_strokes(strokes, max_r)
    n_after = sum(len(s["points"]) for s in filtered)

    data = {**data, "strokes": filtered}
    Path(args.output).write_text(json.dumps(data), encoding="utf-8")
    print(f"{args.json_file} -> {args.output}: max_r={max_r:.1f}px, "
          f"strokes {len(strokes)} -> {len(filtered)}, "
          f"points {n_before} -> {n_after} (removed {removed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
