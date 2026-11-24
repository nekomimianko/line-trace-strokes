#!/usr/bin/env python3
"""Experimental restore: stroke the JSON as smooth curves, not pixel stamps.

The traced points sit on integer pixel centres, so the plain renderer
(render_strokes.py) reproduces a 1px staircase in both the centre line and
the EDT radius. This variant interpolates each stroke with a Catmull-Rom
spline (which passes through every traced point, so no ink geometry is
invented or dropped) and stamps an anti-aliased brush along the curve on a
supersampled canvas.

Measured on examples/anime (medial-axis strokes): IoU 93.01% vs 93.43% for
the plain renderer. The pixel-frequency jitter is reduced but not fully
removed; Gaussian smoothing removes more jitter but cuts corners
(IoU 91.9% at sigma=0.6). Kept as an experiment — the plain renderer plus
chain_strokes.py is the verified main route.

Dependencies: numpy, Pillow.
"""
import argparse, json, math
from pathlib import Path
from PIL import Image, ImageDraw


def catmull_rom(raw, closed, seg=6):
    pts = list(raw)
    if len(pts) < 3:
        return pts
    if closed:
        ring = [pts[-1]] + pts + [pts[0], pts[1]]
    else:
        ring = [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(len(ring) - 3):
        p0, p1, p2, p3 = ring[i], ring[i + 1], ring[i + 2], ring[i + 3]
        for k in range(seg):
            t = k / seg; t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t
                       + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                       + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)
                for j in range(3)))
    out.append(pts[0] if closed else pts[-1])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_file", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=Path("smooth_restored.png"))
    ap.add_argument("--ss", type=int, default=4, help="supersampling factor")
    ap.add_argument("--step", type=float, default=0.25, help="brush stamp spacing (px)")
    ap.add_argument("--radius-offset", type=float, default=-0.5)
    ap.add_argument("--min-radius", type=float, default=0.2)
    args = ap.parse_args()

    data = json.loads(args.json_file.read_text(encoding="utf-8"))
    W, H = data["image"]["width"], data["image"]["height"]
    S = args.ss
    hi = Image.new("L", (W * S, H * S), 255)
    draw = ImageDraw.Draw(hi)

    def dab(x, y, r):
        rr = max(args.min_radius, r + args.radius_offset) * S
        cx, cy = (x + 0.5) * S, (y + 0.5) * S
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=0)

    for stroke in data["strokes"]:
        raw = [(p["x"], p["y"], p["r"]) for p in stroke["points"]]
        if not raw:
            continue
        pts = catmull_rom(raw, stroke.get("closed", False))
        dab(*pts[0])
        pairs = list(zip(pts, pts[1:]))
        if stroke.get("closed") and len(pts) > 1:
            pairs.append((pts[-1], pts[0]))
        for a, b in pairs:
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(1, math.ceil(length / args.step))
            for k in range(1, n + 1):
                t = k / n
                dab(a[0] + (b[0] - a[0]) * t,
                    a[1] + (b[1] - a[1]) * t,
                    a[2] + (b[2] - a[2]) * t)
    hi.resize((W, H), Image.LANCZOS).save(args.output)
    print("saved", args.output)


if __name__ == "__main__":
    main()
