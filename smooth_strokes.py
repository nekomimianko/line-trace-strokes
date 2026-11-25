#!/usr/bin/env python3
"""Post-process stroke JSON with epsilon-bounded smoothing.

This is POST-processing (editing), not measurement: unconstrained smoothing
provably costs fidelity (moving-average radius smoothing: IoU 96.91% ->
96.29% at +-1pt, worse wider; Gaussian center smoothing similar). So every
point is constrained to stay within --epsilon px of its measured position
and every radius within --epsilon-radius of its measured value, while a
Laplacian relaxation removes the high-frequency jitter inside that tube.
Endpoints are kept fixed so junction connectivity survives.

Measured trade-off on the anime test data (baseline: center jitter
0.249 px, radius jitter 0.159 px, IoU 97.40%):

    epsilon / eps_r / iters    center   radius   IoU
    0.15 / 0.08 / 15 (default) 0.093    0.097    96.19%
    0.20 / 0.10 / 20           0.064    0.085    95.61%
    0.30 / 0.15 / 40           0.037    0.060    94.30%

The cost is structural: Laplacian relaxation spends the tube budget
cutting curvature inward, so jitter and fidelity trade directly. Use it
when the destination (a vector editor, a CSP line) rewards smoothness
more than the last point of fidelity -- not in the measurement pipeline.
"""
import argparse, json
import numpy as np
from pathlib import Path

def smooth(data, eps, eps_r, iters, lam):
    moved = 0; total = 0
    for s in data["strokes"]:
        pts = s["points"]; n = len(pts)
        if n < 3: continue
        closed = s.get("closed", False)
        xy = np.array([[p["x"], p["y"]] for p in pts], float)
        r = np.array([p["r"] for p in pts], float)
        xy0, r0 = xy.copy(), r.copy()
        idx = np.arange(n)
        prev_i = (idx - 1) % n if closed else np.maximum(idx - 1, 0)
        next_i = (idx + 1) % n if closed else np.minimum(idx + 1, n - 1)
        free = np.ones(n, bool)
        if not closed: free[0] = free[-1] = False
        for _ in range(iters):
            lap = (xy[prev_i] + xy[next_i]) / 2 - xy
            xy[free] += lam * lap[free]
            d = xy - xy0
            dist = np.hypot(d[:, 0], d[:, 1])
            over = dist > eps
            if over.any():
                xy[over] = xy0[over] + d[over] * (eps / dist[over])[:, None]
            lr = (r[prev_i] + r[next_i]) / 2 - r
            r[free] += lam * lr[free]
            r = np.clip(r, r0 - eps_r, r0 + eps_r)
            r = np.maximum(r, 0.2)
        total += n
        moved += int((np.hypot(*(xy - xy0).T) > 0.01).sum())
        for p, (x, y), rr in zip(pts, xy, r):
            p["x"], p["y"], p["r"] = round(float(x), 3), round(float(y), 3), round(float(rr), 3)
    return moved, total

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strokes")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--epsilon", type=float, default=0.15, help="max center displacement px")
    ap.add_argument("--epsilon-radius", type=float, default=0.08, help="max radius change px")
    ap.add_argument("--iterations", type=int, default=15)
    a = ap.parse_args()
    d = json.loads(Path(a.strokes).read_text())
    moved, total = smooth(d, a.epsilon, a.epsilon_radius, a.iterations, 0.5)
    Path(a.output).write_text(json.dumps(d, separators=(",", ":")))
    print(f"{a.output}: smoothed ({moved}/{total} pts moved, eps={a.epsilon}, eps_r={a.epsilon_radius})")

if __name__ == "__main__":
    main()
