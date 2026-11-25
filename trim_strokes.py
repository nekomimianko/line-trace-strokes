#!/usr/bin/env python3
"""ベタ領域の輪郭でストロークを切りそろえる。

黒ベタ領域（フィル領域）が検出できている前提で、各ストロークを
輪郭の内側と外側に分ける:

- 内側にある点は全部削除する（ベタ内部の骨格は足場であり、
  塗りがその領域を担う）。
- ストロークが輪郭とクロスする場所には、交点に制御点を挿入する
  （位置・半径とも線形補間）。線は輪郭の上でぴたりと終わる。
- 全部が内側のストロークは消える。全部が外側のストロークは不変。

内外の判定は領域マスクの符号付き距離場（内側が正）で行い、
交点は符号のゼロ交差としてサブピクセルに求める。

使い方:
    python trim_strokes.py strokes.json --mask beta_mask.png -o trimmed.json

マスクはベタ領域を黒で描いた画像（グレースケール ≤180 を領域とする）。
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi


def signed_field(mask):
    """領域内が正・外が負の符号付き距離場。"""
    inside = ndi.distance_transform_edt(mask)
    outside = ndi.distance_transform_edt(~mask)
    return inside - outside


def sample(field, xs, ys):
    return ndi.map_coordinates(field, [ys, xs], order=1, mode="nearest")


def trim_stroke(points, field):
    """1ストロークを輪郭で切りそろえ、外側の断片リストを返す。"""
    if not points:
        return [], 0
    xs = np.array([p["x"] for p in points])
    ys = np.array([p["y"] for p in points])
    s = sample(field, xs, ys)
    inside = s > 0.0

    runs = []
    crossings = 0
    current = []

    def flush():
        nonlocal current
        # 交点だけの断片（長さゼロ）は捨てる。元の点を含む断片は、
        # 1点だけのストローク（点）も含めて残す。
        if len(current) >= 2 or (len(current) == 1 and len(points) == 1):
            runs.append(current)
        current = []

    def crossing(p0, p1, s0, s1):
        t = s0 / (s0 - s1) if s1 != s0 else 0.5
        return {
            "x": p0["x"] + (p1["x"] - p0["x"]) * t,
            "y": p0["y"] + (p1["y"] - p0["y"]) * t,
            "r": p0["r"] + (p1["r"] - p0["r"]) * t,
        }

    for i, p in enumerate(points):
        if i > 0 and inside[i] != inside[i - 1]:
            c = crossing(points[i - 1], p, s[i - 1], s[i])
            crossings += 1
            if inside[i]:
                # 外→内：交点で断片を閉じる
                current.append(c)
                flush()
            else:
                # 内→外：交点から新しい断片を始める
                current = [c]
        if not inside[i]:
            current.append(p)
    flush()
    return runs, crossings


def main():
    ap = argparse.ArgumentParser(description="Trim strokes at solid-region contours")
    ap.add_argument("json_file")
    ap.add_argument("--mask", required=True, help="image of the solid regions (dark = region)")
    ap.add_argument("--threshold", type=int, default=180)
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    mask_img = np.asarray(Image.open(args.mask).convert("L"))
    mask = mask_img <= args.threshold
    field = signed_field(mask)

    data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    n_strokes_before = len(data["strokes"])
    out = []
    total_cross = 0
    n_before = 0
    for s in data["strokes"]:
        n_before += len(s["points"])
        runs, cross = trim_stroke(s["points"], field)
        total_cross += cross
        for run in runs:
            out.append({**s, "points": run})
    n_after = sum(len(s["points"]) for s in out)

    data = {**data, "strokes": out}
    Path(args.output).write_text(json.dumps(data), encoding="utf-8")
    print(f"{args.json_file} -> {args.output}: strokes {n_strokes_before} -> {len(out)}, "
          f"points {n_before} -> {n_after}, crossings added {total_cross}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
