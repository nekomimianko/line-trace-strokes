#!/usr/bin/env python3
"""subpixel_refine.py — ストローク中心のサブピクセル化 (sub-pixel center refinement).

Usage:
    python subpixel_refine.py input.png strokes.json -o refined.json [--mode edt]

トレース直後の中心は整数ピクセル標本であり、±0.5px の量子化誤差が
復元輪郭の階段状ジッターになる。本スクリプトは各内点について接線の
法線方向に重みプロファイルをサンプリングし、その重心へ中心を寄せる
ことで、ベクター側で平滑化 (推測) するのではなく、入力側に残っている
情報から誤差を実測寄りに消す。

- mode 'edt' (既定・推奨): 重みは二値マスクの距離変換 (EDT)。
  線の塊の中での中心を素直に表し、アニメ線画テストで IoU 93.43% → 95.90%。
- mode 'gray': 重みはグレー濃度。中心線の凹凸は最も減るが、隣接線や AA に
  引っ張られて IoU はほぼ改善しない (93.46%)。比較用。
- 端点は寄せない (交点の接続を保つ)。寄せ量は ±0.6px にクランプ。
- 半径は変更しない。濃度プロファイルの FWHM からの半径再推定は
  ベタ部の plateau で壊れることを確認済み (IoU 75.95%) のため不採用。

中心を直すと復元時の半径オフセットの最適も -0.5 → -0.4 に移る
 (render_strokes.py --radius-offset -0.4 で IoU 96.91%)。
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt, map_coordinates

OFFS = np.arange(-4.0, 4.0001, 0.2)


def refine_stroke(pts, closed, field, clamp):
    n = len(pts)
    out_pts = []
    shifts = []
    for i in range(n):
        p = pts[i]
        if n < 3 or (not closed and not (0 < i < n - 1)):
            out_pts.append(dict(p))
            continue
        a = pts[(i - 1) % n]
        b = pts[(i + 1) % n]
        tx, ty = b["x"] - a["x"], b["y"] - a["y"]
        length = math.hypot(tx, ty)
        if length == 0:
            out_pts.append(dict(p))
            continue
        tx, ty = tx / length, ty / length
        nx, ny = -ty, tx
        offs = OFFS[np.abs(OFFS) <= p["r"] + 1.0]
        rows = p["y"] + ny * offs
        cols = p["x"] + nx * offs
        w = map_coordinates(field, [rows, cols], order=1, mode="nearest")
        if w.sum() <= 0:
            out_pts.append(dict(p))
            continue
        delta = float((offs * w).sum() / w.sum())
        delta = max(-clamp, min(clamp, delta))
        shifts.append(abs(delta))
        out_pts.append(
            {
                "x": round(p["x"] + nx * delta, 3),
                "y": round(p["y"] + ny * delta, 3),
                "r": p["r"],
            }
        )
    return out_pts, shifts


def main() -> int:
    ap = argparse.ArgumentParser(description="Sub-pixel center refinement of traced strokes")
    ap.add_argument("image", help="original raster image (grayscale source)")
    ap.add_argument("strokes", help="input variable-radius-strokes JSON")
    ap.add_argument("-o", "--output", required=True, help="output JSON")
    ap.add_argument("--mode", choices=["edt", "gray"], default="edt")
    ap.add_argument("--threshold", type=int, default=180, help="ink threshold for the binary mask (edt mode)")
    ap.add_argument("--clamp", type=float, default=0.6, help="max center shift in px")
    args = ap.parse_args()

    gray = np.asarray(Image.open(args.image).convert("L")).astype(np.float64)
    if args.mode == "edt":
        field = distance_transform_edt(gray <= args.threshold)
    else:
        field = (255.0 - gray) / 255.0

    d = json.loads(Path(args.strokes).read_text())
    all_shifts = []
    for s in d["strokes"]:
        s["points"], shifts = refine_stroke(s["points"], s.get("closed", False), field, args.clamp)
        all_shifts.extend(shifts)
    Path(args.output).write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    print(
        f"{args.output}: refined {len(all_shifts)} pts, "
        f"mean|shift|={np.mean(all_shifts):.3f}px, p90={np.percentile(all_shifts, 90):.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
