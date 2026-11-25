#!/usr/bin/env python3
"""Fit Bezier curves to the stroke silhouettes and export as SVG.

Post-processing (adopted 2026-11-25): replaces the polyline outline of
svg_export.py's outline mode with cubic Bezier curves, so long lines
become simple and smooth. It does not apply uniformly -- fitting error
concentrates where the geometry changes fastest, i.e. near the line
ends and in short strokes -- so those zones are treated separately:

- Each stroke's silhouette (per-segment capsule union, as in
  svg_export.py) is built per stroke, because the end zones are defined
  by arc length along the stroke.
- Outline vertices within --end-range px of arc length from either
  stroke end are anchors: they are fitted at the tight --eps-anchor
  and their Bezier handles are collapsed, so the curve hugs the
  measured geometry there. Vertices past a sharp turn (>65 deg) are
  treated as corners the same way.
- The middle of a long stroke is fitted at --eps-mid.
- A stroke no longer than twice --end-range is all end zone: it is
  fitted whole at --eps-short (dense, short segments) instead of being
  smoothed away. Everything is emitted as Bezier curves; what varies
  is segment density, not representation type.

Selection of control points is Douglas-Peucker per interval between
anchors; the curve through them is a periodic Catmull-Rom spline
converted to cubic Beziers.

Measured on the anime test (chained, subpixel-refined data, defaults):
IoU 96.01% vs the original ink -- the polyline outline's 96.09% with
the smoothness of curves on long lines. A single uniform fit
(eps 0.5 everywhere, no end exclusion) drops to 91.00%: the loss sits
almost entirely in the ends and the short strokes. Control points
~290k and ~13 MB on this image, so the win is smoothness and
editability of long lines, not file size.

Coordinate note: as in svg_export.py, the JSON uses pixel-CENTER
coordinates and SVG uses corner coordinates, so everything is wrapped
in translate(0.5, 0.5). Radius offset -0.2 is the geometric optimum
for outlines.

Needs numpy, scipy and shapely.
"""
import argparse, json
from pathlib import Path

import numpy as np


def dp_open(pts, eps):
    """Douglas-Peucker on an open polyline; returns kept indices."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        p0, p1 = pts[a], pts[b]
        dv = p1 - p0
        length = np.hypot(*dv) or 1.0
        seg = pts[a + 1:b]
        dist = np.abs((seg - p0)[:, 0] * dv[1] - (seg - p0)[:, 1] * dv[0]) / length
        i = int(np.argmax(dist))
        if dist[i] > eps:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return list(np.where(keep)[0])


def emit_bezier(K, corner):
    """Periodic Catmull-Rom through K as cubic Bezier path data.

    Handles are collapsed (factor 0) at corner/anchor points so the
    curve passes tightly there instead of overshooting.
    """
    m = len(K)
    parts = [f"M{K[0][0]:.2f} {K[0][1]:.2f}"]
    for i in range(m):
        p1 = K[i]
        p2 = K[(i + 1) % m]
        p0 = K[(i - 1) % m]
        p3 = K[(i + 2) % m]
        f1 = 0.0 if corner[i] else 1.0
        f2 = 0.0 if corner[(i + 1) % m] else 1.0
        c1 = p1 + (p2 - p0) / 6 * f1
        c2 = p2 - (p3 - p1) / 6 * f2
        parts.append(
            f"C{c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} "
            f"{p2[0]:.2f} {p2[1]:.2f}")
    parts.append("Z")
    return " ".join(parts)


def corners_of(K, anchor_flags, threshold_deg=65.0):
    m = len(K)
    corner = anchor_flags.copy()
    for i in range(m):
        v1 = K[i] - K[(i - 1) % m]
        v2 = K[(i + 1) % m] - K[i]
        a1, a2 = np.arctan2(v1[1], v1[0]), np.arctan2(v2[1], v2[0])
        da = abs((a2 - a1 + np.pi) % (2 * np.pi) - np.pi)
        if np.degrees(da) > threshold_deg:
            corner[i] = True
    return corner


def fit_ring(V, forced, eps_mid, eps_anchor):
    """Fit one closed outline ring.

    V: (n, 2) vertices, cyclic. forced: bool per vertex (anchor zone).
    Intervals between anchors are Douglas-Peucker'd at eps_anchor when
    both ends are anchors, at eps_mid in the open middle, and at an
    intermediate tolerance on the boundary intervals.
    Returns (path_data, n_control_points) or (None, n) if degenerate.
    """
    n = len(V)
    if n < 6:
        return None, n
    if forced.any():
        start = int(np.argmax(forced))
    else:
        c = V.mean(axis=0)
        start = int(np.argmax(np.hypot(*(V - c).T)))
    order = [(start + k) % n for k in range(n)]
    Q = V[order]
    F = forced[order]
    ext = np.vstack([Q, Q[0]])
    if not F.any():
        mid = int(np.argmax(np.hypot(*(Q - Q[0]).T)))
        bounds = [0, mid, n]
    else:
        bounds = list(np.where(F)[0])
        if bounds[0] != 0:
            bounds = [0] + bounds
        bounds = bounds + [n]
    kept = set()
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a < 1:
            continue
        end_forced = F[b] if b < n else F[0]
        if F[a] and end_forced:
            eps = eps_anchor
        elif F[a] or end_forced:
            eps = min(eps_mid, 0.2)
        else:
            eps = eps_mid
        for k in dp_open(ext[a:b + 1], eps):
            kept.add(a + k)
    kept = sorted(x for x in kept if x < n)
    K = Q[kept]
    Fk = F[kept]
    if len(K) < 3:
        return None, n
    return emit_bezier(K, corners_of(K, Fk)), len(K)


def polyline_ring(V):
    return "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in V) + " Z"


def build_paths(d, offset, end_range, eps_mid, eps_anchor, eps_short, quad_segs):
    from scipy.spatial import cKDTree
    from shapely import LineString, Point, unary_union

    all_paths = []
    tot_ctrl = 0
    for s in d["strokes"]:
        pts = s["points"]
        if not pts:
            continue
        if len(pts) == 1:
            p = pts[0]
            r = max(0.2, p["r"] + offset)
            all_paths.append(
                f'<circle cx="{p["x"]:.2f}" cy="{p["y"]:.2f}" '
                f'r="{r:.2f}" fill="black"/>')
            continue
        caps = []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            r = max(0.2, (a["r"] + b["r"]) / 2 + offset)
            if a["x"] == b["x"] and a["y"] == b["y"]:
                caps.append(Point(a["x"], a["y"]).buffer(r, quad_segs=quad_segs))
            else:
                caps.append(LineString([(a["x"], a["y"]), (b["x"], b["y"])]).buffer(
                    r, quad_segs=quad_segs, cap_style=1, join_style=1))
        u = unary_union(caps)
        polys = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
        P = np.array([[p["x"], p["y"]] for p in pts])
        arc = np.concatenate([[0], np.cumsum(np.hypot(*(P[1:] - P[:-1]).T))])
        total_len = arc[-1]
        tree = cKDTree(P)
        subs = []
        for poly in polys:
            for ring in [poly.exterior, *poly.interiors]:
                V = np.array(ring.coords[:-1])
                if len(V) < 3:
                    continue
                if total_len <= 2 * end_range:
                    # All end zone: dense tight fit, no smoothing.
                    path, m = fit_ring(V, np.zeros(len(V), bool),
                                       eps_short, eps_short)
                else:
                    _, ii = tree.query(V)
                    sv = arc[ii]
                    forced = np.minimum(sv, total_len - sv) <= end_range
                    path, m = fit_ring(V, forced, eps_mid, eps_anchor)
                if path:
                    subs.append(path)
                    tot_ctrl += m
                else:
                    subs.append(polyline_ring(V))
                    tot_ctrl += len(V)
        if subs:
            all_paths.append(
                f'<path d="{" ".join(subs)}" fill="black" fill-rule="evenodd"/>')
    return all_paths, tot_ctrl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strokes")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--radius-offset", type=float, default=-0.2)
    ap.add_argument("--end-range", type=float, default=10.0,
                    help="arc length px from each stroke end excluded from loose fitting")
    ap.add_argument("--eps-mid", type=float, default=0.3,
                    help="fitting tolerance for the middle of long strokes")
    ap.add_argument("--eps-anchor", type=float, default=0.1,
                    help="fitting tolerance inside the end zones")
    ap.add_argument("--eps-short", type=float, default=0.1,
                    help="fitting tolerance for strokes that are all end zone")
    a = ap.parse_args()
    d = json.loads(Path(a.strokes).read_text())
    W, H = d["image"]["width"], d["image"]["height"]
    paths, n_ctrl = build_paths(d, a.radius_offset, a.end_range,
                                a.eps_mid, a.eps_anchor, a.eps_short, 6)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}">\n<g transform="translate(0.5 0.5)">\n'
           + "\n".join(paths) + "\n</g>\n</svg>\n")
    Path(a.output).write_text(svg)
    print(f"{a.output}: {len(svg)/1e6:.2f} MB, {len(d['strokes'])} strokes, "
          f"{n_ctrl} bezier control points")


if __name__ == "__main__":
    main()
