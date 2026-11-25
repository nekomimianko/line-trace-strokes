#!/usr/bin/env python3
"""黒ベタ領域（フィル領域）の検出と、その輪郭の抽出。

検出の手順:

1. 距離変換 (EDT) で局所幅を測り、深さ `--depth` 以上の芯を取る。
   芯は「線をかなり太めに想定」した既定 6px で、幅 12px までの線は
   線として残り、太い塊にだけ芯が立つ。しきい値はすべて px の
   絶対値で、画像に合わせた調整は利用者が行う。
2. 各芯から、元マスクの中だけを伝う測地膨張を `--depth` 回まで
   行い、領域を主張する。ベタの縁は芯から depth px 以内にあるので
   塊は丸ごと復元され（輪郭は元のインク境界と一致）、付着した線は
   根元から depth px までしか主張されない。複数の芯の主張がぶつかる
   場所（くびれ）が領域の割れ目になる。

輪郭（`--contours-out`）は2方式:

- polygon（既定）: 領域ごとにマーチングスクエアで境界多角形を取り、
  閉ストロークとして出す。半径は `--contour-radius` の一定値。
  間引きは refine_strokes.py の閉曲線細分が担う。
- band: 領域を `--band-depth` だけ浸食した差分（境界の帯）を通常の
  トレースにかけ、得た中心線を半径ぶん外側へずらして境界上に載せる。

使い方:
    python detect_beta.py input.png -o beta_mask.png \
        --regions-json beta_regions.json --contours-out beta_contours.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from skimage.measure import find_contours
from skimage.morphology import disk, erosion

HERE = Path(__file__).resolve().parent


def detect_regions(mask, depth, min_core_area, min_region_area):
    """ベタ領域のリスト（bool 配列）と、その和集合を返す。"""
    edt = ndi.distance_transform_edt(mask)
    cores, ncores = ndi.label(edt >= depth)
    if ncores == 0:
        return [], np.zeros_like(mask)
    core_sizes = ndi.sum_labels(edt >= depth, cores, range(1, ncores + 1))
    keep = [i + 1 for i in range(ncores) if core_sizes[i] >= min_core_area]
    if not keep:
        return [], np.zeros_like(mask)

    # 多源の測地膨張：芯からマスク内だけを depth 回広げ、先着で主張する
    lab = np.where(np.isin(cores, keep), cores, 0).astype(np.int32)
    claimed = lab > 0
    structure = np.ones((3, 3), dtype=bool)
    for _ in range(int(round(depth))):
        grown = ndi.binary_dilation(claimed, structure=structure) & mask & ~claimed
        if not grown.any():
            break
        neigh = ndi.maximum_filter(np.where(claimed, lab, 0), size=3)
        lab[grown] = neigh[grown]
        claimed |= grown

    regions = []
    for c in keep:
        reg = lab == c
        if reg.sum() >= min_region_area:
            regions.append(reg)
    union = np.zeros_like(mask)
    for r in regions:
        union |= r
    return regions, union


def contours_polygon(regions, radius):
    """領域の境界多角形を閉ストロークとして返す。"""
    strokes = []
    for region_id, reg in enumerate(regions, start=1):
        padded = np.pad(reg, 1).astype(float)
        for loop in find_contours(padded, 0.5):
            if len(loop) < 8:
                continue
            pts = [{"x": float(c - 1), "y": float(r - 1), "r": float(radius)}
                   for r, c in loop]
            strokes.append({"closed": True, "role": "fill_contour",
                            "region": region_id, "points": pts})
    return strokes


def contours_band(union, band_depth, radius, work):
    """帯トレース方式：帯の中心線を境界上へずらして閉ストロークにする。"""
    band = union & ~erosion(union, disk(band_depth))
    band_img = work / "band.png"
    Image.fromarray(np.where(band, 0, 255).astype(np.uint8)).save(band_img)
    py = sys.executable
    raw, sub, chained = work / "band_raw.json", work / "band_sub.json", work / "band_chained.json"
    subprocess.run([py, str(HERE / "line_trace.py"), str(band_img), "-o", str(raw),
                    "--method", "medial-axis"], check=True, capture_output=True)
    subprocess.run([py, str(HERE / "subpixel_refine.py"), str(band_img), str(raw),
                    "-o", str(sub)], check=True, capture_output=True)
    subprocess.run([py, str(HERE / "chain_strokes.py"), str(sub), "-o", str(chained)],
                   check=True, capture_output=True)
    data = json.loads(chained.read_text(encoding="utf-8"))

    edt_in = ndi.distance_transform_edt(union)
    gy, gx = np.gradient(edt_in)
    strokes = []
    for s in data["strokes"]:
        pts = []
        for p in s["points"]:
            x, y = p["x"], p["y"]
            vx = ndi.map_coordinates(gx, [[y], [x]], order=1, mode="nearest")[0]
            vy = ndi.map_coordinates(gy, [[y], [x]], order=1, mode="nearest")[0]
            n = float(np.hypot(vx, vy))
            if n > 1e-6:
                # EDT は内側ほど大きい。勾配の逆向き＝外向きへ半径ぶんずらす
                x -= vx / n * p["r"]
                y -= vy / n * p["r"]
            pts.append({"x": float(x), "y": float(y), "r": float(radius)})
        strokes.append({"closed": bool(s.get("closed")), "role": "fill_contour",
                        "region": 0, "points": pts})
    return strokes


def main():
    ap = argparse.ArgumentParser(description="Detect solid (beta) regions and extract their contours")
    ap.add_argument("image")
    ap.add_argument("-o", "--output", required=True, help="region mask image (black = region)")
    ap.add_argument("--threshold", type=int, default=180)
    ap.add_argument("--depth", type=float, default=6.0,
                    help="core depth in px: solids must be locally 2x this wide (default 6)")
    ap.add_argument("--min-core-area", type=int, default=30)
    ap.add_argument("--min-region-area", type=int, default=200)
    ap.add_argument("--band-depth", type=int, default=3,
                    help="erosion depth in px for the band contour method (default 3)")
    ap.add_argument("--contour-method", choices=["polygon", "band"], default="polygon")
    ap.add_argument("--contour-radius", type=float, default=1.0,
                    help="stroke radius of contour lines in px (default 1.0)")
    ap.add_argument("--regions-json", help="write region metadata here")
    ap.add_argument("--contours-out", help="write contour strokes JSON here")
    args = ap.parse_args()

    img = np.asarray(Image.open(args.image).convert("L"))
    mask = img <= args.threshold
    regions, union = detect_regions(mask, args.depth, args.min_core_area,
                                    args.min_region_area)
    Image.fromarray(np.where(union, 0, 255).astype(np.uint8)).save(args.output)
    area = int(union.sum())
    print(f"{args.image}: {len(regions)} regions, {area}px "
          f"({100 * area / max(mask.sum(), 1):.1f}% of ink), depth={args.depth}px")

    if args.regions_json:
        meta = []
        for i, r in enumerate(regions, start=1):
            ys, xs = np.nonzero(r)
            meta.append({"id": i, "area": int(r.sum()),
                         "bbox": [int(xs.min()), int(ys.min()),
                                  int(xs.max()), int(ys.max())]})
        Path(args.regions_json).write_text(
            json.dumps({"depth": args.depth, "band_depth": args.band_depth,
                        "regions": meta}, ensure_ascii=False, indent=1),
            encoding="utf-8")

    if args.contours_out:
        if not regions:
            strokes = []
        elif args.contour_method == "polygon":
            strokes = contours_polygon(regions, args.contour_radius)
        else:
            with tempfile.TemporaryDirectory() as td:
                strokes = contours_band(union, args.band_depth,
                                        args.contour_radius, Path(td))
        data = {"format": "variable-radius-strokes", "version": 1,
                "image": {"filename": Path(args.image).name,
                          "width": int(img.shape[1]),
                          "height": int(img.shape[0])},
                "strokes": strokes}
        Path(args.contours_out).write_text(json.dumps(data), encoding="utf-8")
        n = sum(len(s["points"]) for s in strokes)
        print(f"  contours ({args.contour_method}): {len(strokes)} strokes, {n} points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
