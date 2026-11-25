#!/usr/bin/env python3
"""Thin variable-radius strokes: far fewer control points, same curve.

The traced JSON carries one point per skeleton sample (~1 px apart),
which is far too dense to edit by hand. This script selects, per
stroke, a small subset of the original points such that the
Catmull-Rom spline through the subset stays within --eps px of the
original polyline everywhere:

  * start from the two endpoints plus every sharp turn
    (turn angle >= --corner-deg; those points also keep the curve
    from rounding off hooks and corners),
  * sample the spline through the current subset, find the original
    point farthest from it, insert that point, repeat until every
    original point is within eps.

This is interpolation, not smoothing: kept points sit exactly on the
original centre line with their original radius, and the spline is
only allowed to drift where the drift is invisible at eps scale.
Larger eps gives cleaner, more "drawn" lines at the cost of fidelity
-- the trade-off is the point of the tool, so eps is the one knob.

The spline is evaluated in pieces split at corner points (turn angle
>= --corner-deg), matching how CSP treats corner-flagged spline
points; endpoints of each piece are duplicated (clamped ends).

Input/output are the usual variable-radius-strokes JSON. Points keep
their original x, y, r. Usage:

  python thin_strokes.py strokes_chained.json -o strokes_thin.json
  python thin_strokes.py strokes_chained.json -o out.json --eps 1.0
"""
import argparse, json, math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree


def catmull_rom_samples(pts, per_seg=10):
    """Dense samples of the clamped uniform Catmull-Rom through pts."""
    p = np.asarray(pts, dtype=np.float64)
    if len(p) < 2:
        return p
    ext = np.vstack([p[0], p, p[-1]])
    out = []
    t = np.linspace(0.0, 1.0, per_seg, endpoint=False)
    t2, t3 = t * t, t * t * t
    for i in range(len(ext) - 3):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        seg = 0.5 * ((2 * p1)[:, None]
                     + (-p0 + p2)[:, None] * t
                     + (2 * p0 - 5 * p1 + 4 * p2 - p3)[:, None] * t2
                     + (-p0 + 3 * p1 - 3 * p2 + p3)[:, None] * t3)
        out.append(seg.T)
    out.append(p[-1:])
    return np.vstack(out)


def turn_angles(xy):
    """Direction change (deg) at each interior point."""
    d = np.diff(xy, axis=0)
    n = np.linalg.norm(d, axis=1)
    ok = (n[:-1] > 1e-9) & (n[1:] > 1e-9)
    cos = np.ones(len(d) - 1)
    dot = np.einsum('ij,ij->i', d[:-1], d[1:])
    cos[ok] = dot[ok] / (n[:-1] * n[1:])[ok]
    ang = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
    out = np.zeros(len(xy))
    out[1:-1] = ang
    return out


def thin_stroke(xy, r, eps, eps_r, corner_deg):
    """Indices to keep so the spline stays within eps of the centre
    line AND the linearly interpolated radius stays within eps_r of
    the traced radius. Both criteria insert their worst offender."""
    n = len(xy)
    if n <= 2:
        return list(range(n))
    ang = turn_angles(xy)
    corners = set(np.nonzero(ang >= corner_deg)[0].tolist())
    keep = sorted({0, n - 1} | corners)
    while True:
        # spline pieces split at corner points
        bounds = [0] + sorted(c for c in keep if c in corners) + [n - 1]
        pieces = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            idx = [k for k in keep if a <= k <= b]
            if len(idx) >= 2:
                pieces.append(catmull_rom_samples(xy[idx]))
        samples = np.vstack(pieces) if pieces else xy[keep]
        dist, _ = cKDTree(samples).query(xy)
        worst_c = int(np.argmax(dist))
        score_c = dist[worst_c] / eps
        # radius deviation from linear interpolation between kept points
        karr = np.array(keep)
        r_interp = np.interp(np.arange(n), karr, r[karr])
        dev_r = np.abs(r - r_interp)
        worst_r = int(np.argmax(dev_r))
        score_r = dev_r[worst_r] / eps_r if eps_r > 0 else 0.0
        if score_c <= 1.0 and score_r <= 1.0:
            return keep
        keep.append(worst_c if score_c >= score_r else worst_r)
        keep.sort()


def main():
    ap = argparse.ArgumentParser(description="thin strokes to spline control points")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--eps", type=float, default=0.75,
                    help="max spline deviation from the original line, px")
    ap.add_argument("--eps-r", type=float, default=0.15,
                    help="max radius deviation from linear interpolation, px")
    ap.add_argument("--corner-deg", type=float, default=50.0,
                    help="turn angle at/above which a point is a kept corner")
    a = ap.parse_args()

    d = json.loads(Path(a.input).read_text())
    total_in = total_out = 0
    max_dev_all = 0.0
    for s in d["strokes"]:
        xy = np.array([[p["x"], p["y"]] for p in s["points"]])
        r = np.array([p["r"] for p in s["points"]])
        keep = thin_stroke(xy, r, a.eps, a.eps_r, a.corner_deg)
        total_in += len(xy)
        total_out += len(keep)
        s["points"] = [s["points"][i] for i in keep]
    Path(a.output).write_text(json.dumps(d))
    print(f"{a.input} -> {a.output}: points {total_in} -> {total_out} "
          f"({100.0 * total_out / total_in:.1f}%), eps={a.eps}px, "
          f"eps_r={a.eps_r}px, strokes={len(d['strokes'])}")


if __name__ == "__main__":
    main()
