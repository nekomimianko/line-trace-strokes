#!/usr/bin/env python3
"""ベタ領域の輪郭でストロークを切りそろえる。

黒ベタ領域（フィル領域）が検出できている前提で、各ストロークを
輪郭の内側と外側に分ける:

- 内側にある点は削除する（ベタ内部の骨格は足場であり、塗りがその領域を担う）。
- ストロークが輪郭とクロスする場所には交点に制御点を挿入する。既定では
  `--overlap` (0.8px) だけベタ内側に潜り込ませて着地させ、編集時の隙間を防ぐ。
- ベタ輪郭の縁に張り付いて残った微小な切れ端（`--debris-len` 以下かつ
  輪郭から `--debris-dist` 以内）は輪郭ストローク自身がカバーするため除去する。

使い方:
    python trim_strokes.py strokes.json --mask beta_mask.png -o trimmed.json
"""
from __future__ import annotations

import argparse
import json
import math
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


def run_arc_length(pts: list[dict]) -> float:
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        total += math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    return total


def trim_stroke(points, field, overlap: float = 0.8,
                debris_len: float = 8.0, debris_dist: float = 2.5):
    """1ストロークを輪郭で切りそろえ、外側の断片リストを返す。"""
    if not points:
        return [], 0, 0
    xs = np.array([p["x"] for p in points], dtype=float)
    ys = np.array([p["y"] for p in points], dtype=float)
    s = sample(field, xs, ys)
    inside = s > overlap

    runs = []
    crossings = 0
    dropped_debris = 0
    current = []
    current_s = []
    touched_beta = False

    def flush(touched: bool):
        nonlocal current, current_s, dropped_debris
        if len(current) >= 2 or (len(current) == 1 and len(points) == 1):
            if debris_len > 0 and debris_dist > 0:
                L = run_arc_length(current)
                max_outside = float(max(-v for v in current_s)) if current_s else 0.0
                if touched and L <= debris_len and max_outside <= debris_dist:
                    dropped_debris += 1
                    current = []
                    current_s = []
                    return
                if (not touched) and L <= debris_len * 0.5 and max_outside <= debris_dist * 0.6:
                    dropped_debris += 1
                    current = []
                    current_s = []
                    return
            runs.append(current)
        current = []
        current_s = []

    def crossing(p0, p1, s0, s1):
        denom = s1 - s0
        t = (overlap - s0) / denom if abs(denom) > 1e-9 else 0.5
        t = max(0.0, min(1.0, float(t)))
        return {
            "x": round(p0["x"] + (p1["x"] - p0["x"]) * t, 3),
            "y": round(p0["y"] + (p1["y"] - p0["y"]) * t, 3),
            "r": round(p0["r"] + (p1["r"] - p0["r"]) * t, 3),
        }

    for i, p in enumerate(points):
        if i > 0 and inside[i] != inside[i - 1]:
            c = crossing(points[i - 1], p, s[i - 1], s[i])
            crossings += 1
            if inside[i]:
                current.append(c)
                current_s.append(overlap)
                flush(True)
                touched_beta = False
            else:
                current = [c]
                current_s = [overlap]
                touched_beta = True
        if not inside[i]:
            current.append(p)
            current_s.append(float(s[i]))
    flush(touched_beta)
    return runs, crossings, dropped_debris


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trim strokes at solid-region contours")
    ap.add_argument("json_file")
    ap.add_argument("--mask", required=True, help="image of the solid regions (dark = region)")
    ap.add_argument("--threshold", type=int, default=180)
    ap.add_argument("--overlap", type=float, default=0.8,
                    help="px to tuck cut endpoints inside beta region (default 0.8; 0 = exact edge)")
    ap.add_argument("--debris-len", type=float, default=8.0,
                    help="max arc length px of beta-rim debris fragments to drop (0 = off)")
    ap.add_argument("--debris-dist", type=float, default=2.5,
                    help="max outside distance px from beta rim for debris removal (0 = off)")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args(argv)

    mask_img = np.asarray(Image.open(args.mask).convert("L"))
    mask = mask_img <= args.threshold
    field = signed_field(mask)

    data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    n_strokes_before = len(data["strokes"])
    out = []
    total_cross = 0
    total_debris = 0
    n_before = 0
    for s in data["strokes"]:
        n_before += len(s["points"])
        runs, cross, debris = trim_stroke(
            s["points"], field,
            overlap=args.overlap,
            debris_len=args.debris_len,
            debris_dist=args.debris_dist,
        )
        total_cross += cross
        total_debris += debris
        for run in runs:
            out.append({
                **s,
                "closed": False if cross > 0 else s.get("closed", False),
                "points": run,
            })
    for i, s in enumerate(out):
        s["id"] = i
    n_after = sum(len(s["points"]) for s in out)

    data = {**data, "strokes": out}
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{args.json_file} -> {args.output}: strokes {n_strokes_before} -> {len(out)} "
          f"(debris_dropped={total_debris}, crossings={total_cross}), "
          f"points {n_before} -> {n_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
