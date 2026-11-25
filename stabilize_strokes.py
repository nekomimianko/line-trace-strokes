#!/usr/bin/env python3
"""Stabilize strokes: treat each traced stroke as a hand-drawn stroke
and correct its fine jitter, the way pen-stabilization in drawing
apps works.

thin_strokes.py interpolates: kept points stay exactly on the traced
centre line and every traced wiggle is honoured within eps. This
script does the opposite. Each stroke (split at sharp corners) is
fitted with a *smoothing* spline (scipy splprep) whose smoothing
budget is set by --sigma, the assumed jitter amplitude in px. The
fitted curve is the "intended" simple curve; traced points lying off
it are treated as shake and are NOT preserved. The fitted curve is
then sampled and thinned with the usual insertion rule
(--eps-fit), so the output is a small set of control points lying on
the fitted curve, with radii interpolated from the traced radii at
the matching arc positions.

Endpoints are fitted with a large weight so stroke ends (and corner
points, which bound the pieces) stay where they were drawn; closed
strokes are fitted with a periodic spline.

The trade is explicit: fidelity to the raster drops by roughly the
jitter that was removed. The script reports that correction amount
(mean / p95 distance the original points moved).

Usage:
  python stabilize_strokes.py strokes_chained.json -o strokes_stab.json
  python stabilize_strokes.py in.json -o out.json --sigma 0.6
"""
import argparse, json
from pathlib import Path

import numpy as np
from scipy.interpolate import splprep, splev
from scipy.spatial import cKDTree

from thin_strokes import thin_stroke, turn_angles


def fit_piece(xy, sigma, closed=False):
    """Smoothing-spline fit of one piece. Returns dense samples of the
    fitted curve and, for each sample, the fractional index into xy
    (for radius lookup)."""
    m = len(xy)
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    if closed:
        # wrap the data (repeat the first points past the seam) and fit
        # as an open spline; sample only the first loop
        seg_loop = np.append(seg, np.linalg.norm(xy[0] - xy[-1]))
        cum = np.concatenate([[0.0], np.cumsum(seg_loop)])  # m+1 entries
        total = cum[-1]
        if total <= 0:
            return xy.copy(), np.arange(m, dtype=float)
        ext_xy = np.vstack([xy, xy[:3]])
        ext_u = np.concatenate([cum[:-1], total + cum[1:4]])
        tck, _ = splprep([ext_xy[:, 0], ext_xy[:, 1]], u=ext_u,
                         k=3, s=(m + 3) * sigma * sigma, quiet=2)
        n_smp = max(int(total / 0.5), m)
        us = np.linspace(0.0, total, n_smp, endpoint=False)
        fx, fy = splev(us, tck)
        samples = np.column_stack([fx, fy])
        frac_idx = np.interp(us, cum, np.arange(m + 1)) % m
        return samples, frac_idx
    u = np.concatenate([[0.0], np.cumsum(seg)])
    total = u[-1]
    if total <= 0:
        return xy.copy(), np.arange(m, dtype=float)
    w = np.ones(m)
    w[0] = w[-1] = 25.0
    s = m * sigma * sigma
    tck, uu = splprep([xy[:, 0], xy[:, 1]], u=u, w=w, k=3,
                      s=s, quiet=2)
    n_smp = max(int(total / 0.5), m)
    us = np.linspace(0.0, total, n_smp)
    fx, fy = splev(us, tck)
    samples = np.column_stack([fx, fy])
    frac_idx = np.interp(us, u, np.arange(m))
    samples[0] = xy[0]
    samples[-1] = xy[-1]
    return samples, frac_idx


def process_stroke(stroke, sigma, eps_fit, corner_deg):
    pts = stroke["points"]
    closed = bool(stroke.get("closed"))
    xy = np.array([[p["x"], p["y"]] for p in pts], dtype=float)
    r = np.array([p["r"] for p in pts], dtype=float)
    n = len(xy)
    if n <= 6:
        return pts, xy, xy  # too short to fit; keep, no correction

    if closed:
        samples, frac = fit_piece(xy, sigma, closed=True)
        rs = np.interp(frac, np.arange(n + 1), np.append(r, r[0]))
        # thin cyclically: open the cycle at sample 0, thin, re-close
        ext_xy = np.vstack([samples, samples[:1]])
        ext_r = np.append(rs, rs[0])
        tol = np.full(len(ext_xy), eps_fit)
        keep = thin_stroke(ext_xy, ext_r, tol, 0.0, 999.0)
        keep = [k for k in keep if k < len(samples)]
        out = [{"x": float(samples[k][0]), "y": float(samples[k][1]),
                "r": float(rs[k])} for k in keep]
        return out, xy, samples

    ang = turn_angles(xy)
    bounds = [0] + [i for i in range(1, n - 1) if ang[i] >= corner_deg] + [n - 1]
    out = []
    fitted_all = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        piece_xy, piece_r = xy[a:b + 1], r[a:b + 1]
        if len(piece_xy) <= 6:
            samples, frac = piece_xy, np.arange(len(piece_xy), dtype=float)
        else:
            samples, frac = fit_piece(piece_xy, sigma)
        fitted_all.append(samples)
        rs = np.interp(frac, np.arange(len(piece_r)), piece_r)
        tol = np.full(len(samples), eps_fit)
        keep = thin_stroke(samples, rs, tol, 0.0, 999.0)
        for k in keep:
            p = {"x": float(samples[k][0]), "y": float(samples[k][1]),
                 "r": float(rs[k])}
            if out and abs(out[-1]["x"] - p["x"]) < 1e-9 and abs(out[-1]["y"] - p["y"]) < 1e-9:
                continue
            out.append(p)
    return out, xy, np.vstack(fitted_all)


def main():
    ap = argparse.ArgumentParser(description="stabilize strokes (jitter correction by smoothing-spline fit)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--sigma", type=float, default=0.3,
                    help="assumed jitter amplitude in px (smoothing budget)")
    ap.add_argument("--eps-fit", type=float, default=0.3,
                    help="thinning tolerance against the fitted curve, px")
    ap.add_argument("--corner-deg", type=float, default=50.0)
    a = ap.parse_args()

    d = json.loads(Path(a.input).read_text())
    total_in = total_out = 0
    devs = []
    for s in d["strokes"]:
        out, orig_xy, fitted = process_stroke(s, a.sigma, a.eps_fit, a.corner_deg)
        total_in += len(s["points"])
        total_out += len(out)
        if len(orig_xy) > 6:
            dense, _ = (None, None)
            # correction: distance from original points to fitted curve
            dist, _ = cKDTree(fitted).query(orig_xy)
            devs.append(dist)
        s["points"] = out
    Path(a.output).write_text(json.dumps(d))
    if devs:
        dev = np.concatenate(devs)
        print(f"correction vs original centre line: mean {dev.mean():.3f}px, "
              f"p95 {np.percentile(dev, 95):.3f}px, max {dev.max():.3f}px")
    print(f"{a.input} -> {a.output}: points {total_in} -> {total_out} "
          f"({100.0 * total_out / total_in:.1f}%), sigma={a.sigma}px, "
          f"eps_fit={a.eps_fit}px, strokes={len(d['strokes'])}")


if __name__ == "__main__":
    main()
