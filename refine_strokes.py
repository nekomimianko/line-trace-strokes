#!/usr/bin/env python3
"""Refine strokes for hand editing in CLIP STUDIO PAINT:

1. Edge-preserving 1D bilateral smoothing on stroke width r(s) (protects
   true tapers and sharp transitions while removing subpixel width jitter;
   disabled automatically on fill_contour strokes or when --r-sigma-s 0).
2. Multi-scale corner apex detection so sharp corners stay crisp without
   locking onto subpixel jitter or pre-apex points.
3. Peak-error top-down Catmull-Rom subdivision: inserts new control points
   at actual curvature or width extrema instead of mechanical midpoints.
4. O(m) post-subdivision pruning: drops any redundant interior control point
   whose removal stays within (--tol, --eps-r), yielding minimal, cleanly
   placed control points.

Usage:
  python refine_strokes.py strokes_chained.json -o strokes_ref.json
  python refine_strokes.py in.json -o out.json --tol 0.75 --eps-r 1.0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def cr_eval(p0, p1, p2, p3, ts):
    t = ts[:, None]
    t2 = t * t
    t3 = t2 * t
    return 0.5 * (
        2.0 * p1
        + (-p0 + p2) * t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )


def arc_params(xy: np.ndarray) -> np.ndarray:
    if len(xy) < 2:
        return np.zeros(len(xy), dtype=float)
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)])


def smooth_radius_bilateral(r: np.ndarray, arc: np.ndarray,
                            sigma_s: float = 3.5, sigma_r: float = 0.65,
                            closed: bool = False) -> np.ndarray:
    """1D bilateral filter on r(s): removes small measurement jitter while
    preserving genuine stroke tapers and sharp width transitions."""
    n = len(r)
    if n < 5 or sigma_s <= 0:
        return r.copy()
    out = r.copy()
    rad_s = sigma_s * 2.5
    inv_2s2 = 1.0 / (2.0 * sigma_s * sigma_s)
    inv_2r2 = 1.0 / (2.0 * sigma_r * sigma_r)
    for i in range(n):
        if not closed and (i == 0 or i == n - 1):
            continue
        ai = arc[i]
        lo = int(np.searchsorted(arc, ai - rad_s))
        hi = int(np.searchsorted(arc, ai + rad_s, side="right"))
        if hi - lo < 2:
            continue
        ds = arc[lo:hi] - ai
        dr = r[lo:hi] - r[i]
        w = np.exp(-(ds * ds) * inv_2s2 - (dr * dr) * inv_2r2)
        out[i] = float((w * r[lo:hi]).sum() / w.sum())
    return out


def span_worst_error(ctrl_xy, k, xy, arc, ctrl_idx):
    """Return (max_dist, worst_orig_idx) for Catmull-Rom span k (ctrl[k] -> ctrl[k+1])."""
    m = len(ctrl_xy)
    i0, i1 = ctrl_idx[k], ctrl_idx[k + 1]
    if i1 - i0 <= 1:
        return 0.0, i0
    p0 = ctrl_xy[k - 1] if k > 0 else ctrl_xy[k]
    p1, p2 = ctrl_xy[k], ctrl_xy[k + 1]
    p3 = ctrl_xy[k + 2] if k + 2 < m else ctrl_xy[k + 1]
    span_len = max(arc[i1] - arc[i0], 1e-9)
    n_t = max(int(span_len / 0.5), 5)
    dense = cr_eval(p0, p1, p2, p3, np.linspace(0.0, 1.0, n_t))
    pts = xy[i0 + 1:i1]
    d = np.linalg.norm(pts[:, None, :] - dense[None, :, :], axis=2).min(axis=1)
    rel = int(np.argmax(d))
    return float(d[rel]), i0 + 1 + rel


def span_worst_radius_error(r, arc, i0, i1):
    if i1 - i0 <= 1:
        return 0.0, i0
    sub_arc = arc[i0 + 1:i1]
    r_lin = np.interp(sub_arc, [arc[i0], arc[i1]], [r[i0], r[i1]])
    err = np.abs(r[i0 + 1:i1] - r_lin)
    rel = int(np.argmax(err))
    return float(err[rel]), i0 + 1 + rel


def refine_piece(xy: np.ndarray, r: np.ndarray, tol: float, eps_r: float) -> list[int]:
    n = len(xy)
    if n <= 2:
        return list(range(n))
    arc = arc_params(xy)
    ctrl = [0, n - 1]

    for _ in range(80):
        ctrl_xy = xy[ctrl]
        splits = []
        for k in range(len(ctrl) - 1):
            i0, i1 = ctrl[k], ctrl[k + 1]
            if i1 - i0 <= 1:
                continue
            dev_xy, idx_xy = span_worst_error(ctrl_xy, k, xy, arc, ctrl)
            dev_r, idx_r = span_worst_radius_error(r, arc, i0, i1) if eps_r > 0 else (0.0, i0)
            sc_xy = dev_xy / tol if tol > 0 else 0.0
            sc_r = dev_r / eps_r if eps_r > 0 else 0.0
            if sc_xy > 1.0 or sc_r > 1.0:
                pick = idx_xy if sc_xy >= sc_r else idx_r
                if i0 < pick < i1:
                    splits.append(pick)
        if not splits:
            break
        ctrl = sorted(set(ctrl) | set(splits))

    # O(m) multi-pass sweep to drop redundant interior control points
    for _pass in range(4):
        if len(ctrl) <= 2:
            break
        removed_any = False
        pos = 1
        while pos < len(ctrl) - 1:
            trial = ctrl[:pos] + ctrl[pos + 1:]
            trial_xy = xy[trial]
            s_lo = max(0, pos - 2)
            s_hi = min(len(trial) - 1, pos + 1)
            ok = True
            for k in range(s_lo, s_hi):
                dev_xy, _ = span_worst_error(trial_xy, k, xy, arc, trial)
                if tol > 0 and dev_xy > tol:
                    ok = False
                    break
                if eps_r > 0:
                    dev_r, _ = span_worst_radius_error(r, arc, trial[k], trial[k + 1])
                    if dev_r > eps_r:
                        ok = False
                        break
            if ok:
                ctrl.pop(pos)
                removed_any = True
                pos += 1
            else:
                pos += 1
        if not removed_any:
            break

    return ctrl


def refine_closed(xy: np.ndarray, r: np.ndarray, tol: float, eps_r: float) -> list[int]:
    n = len(xy)
    if n <= 4:
        return list(range(n))
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    seg = np.append(seg, np.linalg.norm(xy[0] - xy[-1]))
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    total = arc[-1]
    ctrl = sorted({int(np.argmin(np.abs(arc[:-1] - q * total)))
                   for q in (0.0, 0.25, 0.5, 0.75)})

    def span_err(ctrl_list, k):
        m = len(ctrl_list)
        i0 = ctrl_list[k]
        i1 = ctrl_list[(k + 1) % m]
        i1_ext = i1 if i1 > i0 else i1 + n
        if i1_ext - i0 <= 1:
            return 0.0, i0, 0.0, i0
        idx = list(range(i0 + 1, i1_ext))
        pts = np.array([xy[i % n] for i in idx])
        arcs = np.array([arc[i] if i <= n else arc[i - n] + total for i in idx])
        a0 = arc[i0]
        a1 = arc[i1_ext] if i1_ext <= n else arc[i1_ext - n] + total
        p0 = xy[ctrl_list[(k - 1) % m]]
        p1 = xy[ctrl_list[k]]
        p2 = xy[ctrl_list[(k + 1) % m]]
        p3 = xy[ctrl_list[(k + 2) % m]]
        n_t = max(int(max(a1 - a0, 1e-9) / 0.5), 5)
        dense = cr_eval(p0, p1, p2, p3, np.linspace(0.0, 1.0, n_t))
        d = np.linalg.norm(pts[:, None, :] - dense[None, :, :], axis=2).min(axis=1)
        rel_xy = int(np.argmax(d))
        dev_xy, idx_xy = float(d[rel_xy]), idx[rel_xy] % n
        if eps_r > 0:
            r_sub = np.array([r[i % n] for i in idx])
            r_lin = np.interp(arcs, [a0, a1], [r[i0], r[i1]])
            er = np.abs(r_sub - r_lin)
            rel_r = int(np.argmax(er))
            dev_r, idx_r = float(er[rel_r]), idx[rel_r] % n
        else:
            dev_r, idx_r = 0.0, i0
        return dev_xy, idx_xy, dev_r, idx_r

    for _ in range(80):
        splits = []
        for k in range(len(ctrl)):
            dev_xy, idx_xy, dev_r, idx_r = span_err(ctrl, k)
            sc_xy = dev_xy / tol if tol > 0 else 0.0
            sc_r = dev_r / eps_r if eps_r > 0 else 0.0
            if sc_xy > 1.0 or sc_r > 1.0:
                pick = idx_xy if sc_xy >= sc_r else idx_r
                if pick not in ctrl:
                    splits.append(pick)
        if not splits:
            break
        ctrl = sorted(set(ctrl) | set(splits))

    for _pass in range(4):
        if len(ctrl) <= 4:
            break
        removed_any = False
        pos = 0
        while pos < len(ctrl) and len(ctrl) > 4:
            trial = ctrl[:pos] + ctrl[pos + 1:]
            mt = len(trial)
            ok = True
            for dk in (-2, -1, 0):
                k = (pos + dk) % mt
                dev_xy, _, dev_r, _ = span_err(trial, k)
                if (tol > 0 and dev_xy > tol) or (eps_r > 0 and dev_r > eps_r):
                    ok = False
                    break
            if ok:
                ctrl.pop(pos)
                removed_any = True
                pos += 1
            else:
                pos += 1
        if not removed_any:
            break

    return ctrl


def refine_stroke(stroke: dict, tol: float, eps_r: float, corner_deg: float,
                  r_sigma_s: float = 3.5, r_sigma_r: float = 0.65) -> list[dict]:
    pts = stroke["points"]
    n = len(pts)
    if n <= 2:
        return pts
    xy = np.array([[p["x"], p["y"]] for p in pts], dtype=float)
    r = np.array([p["r"] for p in pts], dtype=float)
    closed = bool(stroke.get("closed", False))

    if r_sigma_s > 0 and n >= 5 and stroke.get("role") != "fill_contour":
        arc = arc_params(xy)
        r = smooth_radius_bilateral(r, arc, sigma_s=r_sigma_s, sigma_r=r_sigma_r, closed=closed)

    if closed:
        keep = refine_closed(xy, r, tol, eps_r)
        return [{"x": round(float(xy[i, 0]), 3), "y": round(float(xy[i, 1]), 3),
                 "r": round(float(r[i]), 3)} for i in keep]

    arc = arc_params(xy)
    angs = np.zeros(n, dtype=float)
    for i in range(1, n - 1):
        ia = int(np.searchsorted(arc, arc[i] - 3.0))
        ib = min(n - 1, int(np.searchsorted(arc, arc[i] + 3.0)))
        if ia >= i:
            ia = i - 1
        if ib <= i:
            ib = i + 1
        v1 = xy[i] - xy[ia]
        v2 = xy[ib] - xy[i]
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 >= 0.8 and n2 >= 0.8:
            angs[i] = float(np.degrees(np.arccos(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))))

    bounds = [0]
    i = 1
    while i < n - 1:
        if angs[i] >= corner_deg:
            j = i
            best_i = i
            while j < n - 1 and angs[j] >= corner_deg:
                if angs[j] > angs[best_i]:
                    best_i = j
                j += 1
            if arc[best_i] - arc[bounds[-1]] >= 3.0 and arc[-1] - arc[best_i] >= 3.0:
                bounds.append(best_i)
            i = j
        else:
            i += 1
    bounds.append(n - 1)

    keep = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        idx = refine_piece(xy[a:b + 1], r[a:b + 1], tol, eps_r)
        keep.extend(a + i for i in idx)
    keep = sorted(set(keep))
    return [{"x": round(float(xy[i, 0]), 3), "y": round(float(xy[i, 1]), 3),
             "r": round(float(r[i]), 3)} for i in keep]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Peak-error subdivision + bilateral width refinement of strokes")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--tol", type=float, default=0.3,
                    help="max allowed deviation from the traced line, px")
    ap.add_argument("--eps-r", type=float, default=0.15,
                    help="max allowed radius deviation, px (0 = off)")
    ap.add_argument("--corner-deg", type=float, default=55.0)
    ap.add_argument("--r-sigma-s", type=float, default=3.5,
                    help="spatial sigma (px) for bilateral width smoothing (0 = off)")
    ap.add_argument("--r-sigma-r", type=float, default=0.65,
                    help="range sigma (px) for bilateral width smoothing")
    ap.add_argument("--init", type=int, default=2, choices=[2, 4],
                    help="(kept for CLI compatibility)")
    a = ap.parse_args(argv)

    d = json.loads(Path(a.input).read_text(encoding="utf-8"))
    total_in = total_out = 0
    for s in d["strokes"]:
        total_in += len(s["points"])
        s["points"] = refine_stroke(
            s, a.tol, a.eps_r, a.corner_deg, a.r_sigma_s, a.r_sigma_r
        )
        total_out += len(s["points"])
    Path(a.output).write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{a.input} -> {a.output}: points {total_in} -> {total_out} "
          f"({100.0 * total_out / max(total_in, 1):.1f}%), tol={a.tol}px, "
          f"eps_r={a.eps_r}px, strokes={len(d['strokes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
