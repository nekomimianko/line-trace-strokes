#!/usr/bin/env python3
"""Refine strokes top-down: start from the straight line between a
piece's endpoints and subdivide intervals until the curve matches
the traced line.

Instead of judging curvature from neighbouring samples (noisy), the
hierarchy discovers it: a stroke piece starts as the 2 endpoint
control points (--init 4 seeds the arc-length thirds as well, with
no measured benefit). Each interval is checked against the original
samples inside it; an interval that deviates more than --tol is
split in two by adding the original sample nearest its arc midpoint
as a new control point (on the line, with its original radius).
Rounds repeat until every interval matches. Gentle regions stay
coarse, tight regions refine down — subdivision depth is the
multi-scale curvature measure. Closed strokes are seeded with 4
points around the loop (2 would degenerate) and refined cyclically.

Sharp corners (>= --corner-deg) split the stroke into pieces first,
as in thin_strokes.py, so corners stay corners. An optional radius
criterion (--eps-r) also splits an interval whose linearly
interpolated radius deviates from the traced radii too much.

The defaults are deliberately conservative (tol 0.3 px, eps_r
0.15 px): the result barely deviates from the traced line. Raise
--tol to smooth the stroke shape more, raise --eps-r to smooth the
line-width (brush size) variation more; both are in pixels.

Usage:
  python refine_strokes.py strokes_chained.json -o strokes_ref.json
  python refine_strokes.py in.json -o out.json --tol 1.5 --eps-r 0.35
"""
import argparse, json
from pathlib import Path

import numpy as np

from thin_strokes import turn_angles


def cr_point(p0, p1, p2, p3, t):
    """One point of the uniform Catmull-Rom segment p1 -> p2
    (same formula as thin_strokes.catmull_rom_samples)."""
    return 0.5 * (2 * p1 + (-p0 + p2) * t
                  + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t)


def arc_params(xy):
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)]), seg


def span_deviation(ctrl_xy, k, samp_xy, samp_arc, a0, a1):
    """Max distance from the original samples in arc range [a0, a1]
    to the Catmull-Rom curve of span k (controls k -> k+1)."""
    m = len(ctrl_xy)
    p0 = ctrl_xy[k - 1] if k > 0 else ctrl_xy[k]
    p1, p2 = ctrl_xy[k], ctrl_xy[k + 1]
    p3 = ctrl_xy[k + 2] if k + 2 < m else ctrl_xy[k + 1]
    span_len = max(a1 - a0, 1e-9)
    n_t = max(int(span_len / 0.5), 4)
    ts = np.linspace(0.0, 1.0, n_t)
    dense = np.array([cr_point(p0, p1, p2, p3, t) for t in ts])
    sel = (samp_arc >= a0 - 1e-9) & (samp_arc <= a1 + 1e-9)
    pts = samp_xy[sel]
    if len(pts) == 0:
        return 0.0, sel
    d = np.linalg.norm(pts[:, None, :] - dense[None, :, :], axis=2)
    return float(d.min(axis=1).max()), sel


def refine_piece(xy, r, tol, eps_r, init=2):
    """xy, r: one corner-free piece (open). Returns control indices."""
    n = len(xy)
    if n <= 4:
        return list(range(n))
    arc, _ = arc_params(xy)
    total = arc[-1]
    if init == 2:
        ctrl = [0, n - 1]
    else:
        ctrl = sorted({0, n - 1,
                       int(np.argmin(np.abs(arc - total / 3.0))),
                       int(np.argmin(np.abs(arc - 2.0 * total / 3.0)))})
    for _ in range(60):
        ctrl_xy = xy[ctrl]
        ctrl_arc = arc[ctrl]
        ctrl_r = r[ctrl]
        splits = []
        for k in range(len(ctrl) - 1):
            a0, a1 = ctrl_arc[k], ctrl_arc[k + 1]
            dev, sel = span_deviation(ctrl_xy, k, xy, arc, a0, a1)
            need = dev > tol
            if not need and eps_r > 0:
                idx = np.where(sel)[0]
                if len(idx):
                    r_lin = np.interp(arc[idx], [a0, a1],
                                      [ctrl_r[k], ctrl_r[k + 1]])
                    need = float(np.abs(r[idx] - r_lin).max()) > eps_r
            if need:
                mid = 0.5 * (a0 + a1)
                cand = [i for i in range(ctrl[k] + 1, ctrl[k + 1])
                        if i not in ctrl]
                if cand:
                    j = min(cand, key=lambda i: abs(arc[i] - mid))
                    splits.append(j)
        if not splits:
            break
        ctrl = sorted(set(ctrl) | set(splits))
    return ctrl


def refine_closed(xy, r, tol, eps_r):
    """Cyclic version: 4 seed points at quarter arcs, spans wrap."""
    n = len(xy)
    if n <= 5:
        return list(range(n))
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    seg = np.append(seg, np.linalg.norm(xy[0] - xy[-1]))
    arc = np.concatenate([[0.0], np.cumsum(seg)])  # n+1, arc[n] = total
    total = arc[-1]
    ctrl = sorted({int(np.argmin(np.abs(arc[:-1] - q * total)))
                   for q in (0.0, 0.25, 0.5, 0.75)})
    ext_xy = np.vstack([xy, xy[:1]])  # index n == point 0

    def span_samples(k):
        """(sample indices, their arc) in span ctrl[k] -> ctrl[k+1]."""
        i0 = ctrl[k]
        i1 = ctrl[(k + 1) % len(ctrl)]
        if i1 <= i0:
            i1 += n
        idx = list(range(i0, i1 + 1))
        arcs = np.array([arc[i] if i <= n else arc[i - n] + total
                         for i in idx])
        pts = np.array([xy[i % n] for i in idx])
        return idx, arcs, pts, arc[i0], (arc[i1] if i1 <= n else arc[i1 - n] + total)

    for _ in range(60):
        m = len(ctrl)
        splits = []
        for k in range(m):
            idx, arcs, pts, a0, a1 = span_samples(k)
            # CR tangents on the cycle
            prev = xy[ctrl[k - 1]]
            nxt = xy[ctrl[(k + 2) % m]] if m > 2 else xy[ctrl[(k + 1) % m]]
            p1, p2 = xy[ctrl[k]], xy[ctrl[(k + 1) % m]]
            span_len = max(a1 - a0, 1e-9)
            n_t = max(int(span_len / 0.5), 4)
            ts = np.linspace(0.0, 1.0, n_t)
            dense = np.array([cr_point(prev, p1, p2, nxt, t) for t in ts])
            d = np.linalg.norm(pts[:, None, :] - dense[None, :, :], axis=2)
            dev = float(d.min(axis=1).max()) if len(pts) else 0.0
            need = dev > tol
            if not need and eps_r > 0 and len(pts):
                r_ext = np.array([r[i % n] for i in idx])
                r_lin = np.interp(arcs, [a0, a1],
                                  [r[ctrl[k]], r[ctrl[(k + 1) % m]]])
                need = float(np.abs(r_ext - r_lin).max()) > eps_r
            if need:
                mid = 0.5 * (a0 + a1)
                cand = [(i % n, arcs[j]) for j, i in enumerate(idx)
                        if i % n not in ctrl]
                if cand:
                    j = min(cand, key=lambda t: abs(t[1] - mid))[0]
                    splits.append(j)
        if not splits:
            break
        ctrl = sorted(set(ctrl) | set(splits))
    return ctrl


def refine_stroke(stroke, tol, eps_r, corner_deg, init=2):
    pts = stroke["points"]
    xy = np.array([[p["x"], p["y"]] for p in pts], dtype=float)
    r = np.array([p["r"] for p in pts], dtype=float)
    n = len(xy)
    if n <= 4:
        return pts
    if stroke.get("closed"):
        keep = refine_closed(xy, r, tol, eps_r)
        return [pts[i] for i in keep]
    ang = turn_angles(xy)
    bounds = [0] + [i for i in range(1, n - 1) if ang[i] >= corner_deg] + [n - 1]
    keep = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        idx = refine_piece(xy[a:b + 1], r[a:b + 1], tol, eps_r, init)
        keep.extend(a + i for i in idx)
    keep = sorted(set(keep))
    return [pts[i] for i in keep]


def main(argv=None):
    ap = argparse.ArgumentParser(description="top-down subdivision refinement of strokes")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--tol", type=float, default=0.3,
                    help="max allowed deviation from the traced line, px")
    ap.add_argument("--eps-r", type=float, default=0.15,
                    help="max allowed radius deviation, px (0 = off)")
    ap.add_argument("--corner-deg", type=float, default=50.0)
    ap.add_argument("--init", type=int, default=2, choices=[2, 4],
                    help="seed control points per open piece (2 = straight line)")
    a = ap.parse_args(argv)

    d = json.loads(Path(a.input).read_text())
    total_in = total_out = 0
    for s in d["strokes"]:
        total_in += len(s["points"])
        s["points"] = refine_stroke(s, a.tol, a.eps_r, a.corner_deg, a.init)
        total_out += len(s["points"])
    Path(a.output).write_text(json.dumps(d))
    print(f"{a.input} -> {a.output}: points {total_in} -> {total_out} "
          f"({100.0 * total_out / total_in:.1f}%), tol={a.tol}px, "
          f"eps_r={a.eps_r}px, strokes={len(d['strokes'])}")


if __name__ == "__main__":
    main()
