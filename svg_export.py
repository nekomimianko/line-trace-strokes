#!/usr/bin/env python3
"""Export variable-radius stroke JSON as SVG.

Two modes:

- outline (既定): the exact silhouette of the stroke model -- the union of
  per-segment capsules (disk sweep) -- as filled paths. Preserves taper.
  It edits as a filled vector shape. Needs shapely.
  Measured on the anime test (chained, subpixel-refined data):
  IoU 96.09% vs the original ink at the default simplify 0.1 (2.2 MB);
  96.40% with --simplify 0 (8.3 MB).
- centerline: one stroked path per stroke, width = median diameter.
  Per-point width variation (taper) is lost: an SVG stroke carries
  exactly one width per path, so line width variation cannot be
  expressed in this format.

Coordinate note: the stroke JSON uses pixel-CENTER coordinates ((0,0) is
the center of the top-left pixel), while SVG uses corner coordinates.
All geometry is therefore wrapped in translate(0.5, 0.5). Omitting this
half-pixel shift drops the measured IoU from ~96% to ~79%.

Radius offset: the geometric optimum for the outline is -0.2 (the stamp
renderer in render_strokes.py prefers -0.35 because its disk stamps are
drawn slightly fat; the difference is the rasterizer's bias, not data).
"""
import argparse, json, math
from pathlib import Path

def fmt(v): return f"{v:.2f}"

def centerline_paths(d, offset):
    out = []
    for s in d["strokes"]:
        pts = s["points"]
        if not pts: continue
        rs = sorted(max(0.2, p["r"] + offset) for p in pts)
        med = rs[len(rs)//2]
        dd = "M" + " L".join(f"{fmt(p['x'])} {fmt(p['y'])}" for p in pts)
        out.append(f'<path d="{dd}" fill="none" stroke="black" stroke-width="{fmt(2*med)}" stroke-linecap="round" stroke-linejoin="round"/>')
    return "\n".join(out)

def outline_path(d, offset, simplify, quad_segs):
    try:
        from shapely import LineString, Point, unary_union
    except ImportError:
        raise SystemExit("outline mode needs shapely: pip install shapely")
    caps = []
    for s in d["strokes"]:
        pts = s["points"]
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            r = max(0.2, (a["r"] + b["r"]) / 2 + offset)
            if a["x"] == b["x"] and a["y"] == b["y"]:
                caps.append(Point(a["x"], a["y"]).buffer(r, quad_segs=quad_segs))
            else:
                caps.append(LineString([(a["x"], a["y"]), (b["x"], b["y"])]).buffer(
                    r, quad_segs=quad_segs, cap_style=1, join_style=1))
        if len(pts) == 1:
            p = pts[0]
            caps.append(Point(p["x"], p["y"]).buffer(max(0.2, p["r"] + offset), quad_segs=quad_segs))
    u = unary_union(caps)
    if simplify > 0:
        u = u.simplify(simplify)
    polys = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
    def ring(coords):
        return "M" + " L".join(f"{fmt(x)} {fmt(y)}" for x, y in coords) + " Z"
    subs = []
    for p in polys:
        subs.append(ring(p.exterior.coords))
        for hole in p.interiors:
            subs.append(ring(hole.coords))
    return f'<path d="{" ".join(subs)}" fill="black" fill-rule="evenodd"/>'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strokes")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--mode", choices=["outline", "centerline"], default="outline")
    ap.add_argument("--radius-offset", type=float, default=-0.2)
    ap.add_argument("--simplify", type=float, default=0.1,
                    help="outline polygon simplification tolerance px (0 = none)")
    a = ap.parse_args()
    d = json.loads(Path(a.strokes).read_text())
    W, H = d["image"]["width"], d["image"]["height"]
    body = (outline_path(d, a.radius_offset, a.simplify, 6) if a.mode == "outline"
            else centerline_paths(d, a.radius_offset))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">\n<g transform="translate(0.5 0.5)">\n{body}\n</g>\n</svg>\n')
    Path(a.output).write_text(svg)
    print(f"{a.output}: {len(svg)/1e6:.2f} MB, mode={a.mode}")

if __name__ == "__main__":
    main()
