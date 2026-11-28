#!/usr/bin/env python3
"""Chain variable-radius strokes across junctions by direction continuity.

Strokes produced by line_trace.py are split at every skeleton node whose
degree != 2, so one pen line crossing another becomes many fragments.
At each shared endpoint (junction), this script pairs up stroke ends whose
directions are most nearly straight-through (arriving direction vs leaving
direction), then concatenates the paired strokes into longer chains.
Unpaired ends stay as chain boundaries. Geometry is untouched: the chained
JSON renders exactly the same ink, only the stroke segmentation changes.
"""
import argparse, json, math
from collections import defaultdict
from pathlib import Path

def end_dir(points, side, look=5):
    """Unit vector pointing from the endpoint INTO the stroke."""
    if side == 'start':
        a, seg = points[0], points[1:look+1]
        if not seg: return None
        b = seg[-1]
    else:
        a, seg = points[-1], points[-look-1:-1]
        if not seg: return None
        b = seg[0]
    vx, vy = b['x']-a['x'], b['y']-a['y']
    n = math.hypot(vx, vy)
    return (vx/n, vy/n) if n > 0 else None

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('json_file', type=Path)
    ap.add_argument('-o', '--output', type=Path, default=Path('strokes_chained.json'))
    ap.add_argument('--max-dot', type=float, default=-0.3,
                    help='Pair ends only if dot(dir_in_a, dir_in_b) <= this '
                         '(-1 = perfectly straight through the junction)')
    args = ap.parse_args(argv)

    data = json.loads(args.json_file.read_text())
    strokes = data['strokes']
    # ends: (stroke_id, side) ; key = endpoint coordinate
    at = defaultdict(list)
    for s in strokes:
        pts = s['points']
        if not pts: continue
        at[(pts[0]['x'], pts[0]['y'])].append((s['id'], 'start'))
        if not s.get('closed') and len(pts) > 1:
            at[(pts[-1]['x'], pts[-1]['y'])].append((s['id'], 'end'))

    by_id = {s['id']: s for s in strokes}
    link = {}  # (id, side) -> (id, side) paired across a junction
    junctions = 0
    for coord, ends in at.items():
        if len(ends) < 2: continue
        junctions += 1
        dirs = {}
        for e in ends:
            d = end_dir(by_id[e[0]]['points'], e[1])
            if d: dirs[e] = d
        # candidate pairs: most negative dot = straightest
        cand = []
        el = [e for e in ends if e in dirs]
        for i in range(len(el)):
            for j in range(i+1, len(el)):
                a, b = el[i], el[j]
                if a[0] == b[0]: continue  # same stroke (loop back) - skip
                dot = dirs[a][0]*dirs[b][0] + dirs[a][1]*dirs[b][1]
                if dot <= args.max_dot:
                    cand.append((dot, a, b))
        cand.sort()
        used = set()
        for dot, a, b in cand:
            if a in used or b in used: continue
            used.add(a); used.add(b)
            link[a] = b; link[b] = a

    # Walk chains through links. Each stroke used once; orientation chosen
    # so consecutive strokes connect end-to-start.
    visited = set()
    chains = []
    def emit(seq):
        """seq: list of (stroke, reversed?) already oriented head->tail."""
        pts = []
        closed = False
        for k, (s, rev) in enumerate(seq):
            p = list(reversed(s['points'])) if rev else list(s['points'])
            if k > 0 and pts and p and pts[-1]['x'] == p[0]['x'] and pts[-1]['y'] == p[0]['y']:
                p = p[1:]
            pts.extend(p)
        if len(seq) == 1 and seq[0][0].get('closed'):
            closed = True
        chains.append({'id': len(chains), 'closed': closed, 'points': pts})

    for s in strokes:
        if s['id'] in visited or not s['points']: continue
        # extend backwards from this stroke's start, then forwards from end
        seq = [(s, False)]
        visited.add(s['id'])
        # backward: current head is seq[0]; its entering side is 'start' if not rev else 'end'
        while True:
            head, hrev = seq[0]
            side = 'end' if hrev else 'start'
            nxt = link.get((head['id'], side))
            if not nxt or nxt[0] in visited: break
            ns = by_id[nxt[0]]
            # nxt side is the side touching the junction; ns must be oriented
            # so that its touching side is at its tail (it connects to head's head)
            nrev = (nxt[1] == 'start')  # if junction touches ns start, reverse so start becomes tail
            seq.insert(0, (ns, nrev)); visited.add(ns['id'])
        while True:
            tail, trev = seq[-1]
            side = 'start' if trev else 'end'
            nxt = link.get((tail['id'], side))
            if not nxt or nxt[0] in visited: break
            ns = by_id[nxt[0]]
            nrev = (nxt[1] == 'end')  # junction touches ns end -> reverse so end becomes head
            seq.append((ns, nrev)); visited.add(ns['id'])
        emit(seq)

    out = dict(data)
    out['strokes'] = chains
    out['stats'] = dict(data.get('stats', {}))
    out['stats']['stroke_count_before_chaining'] = len(strokes)
    out['stats']['stroke_count'] = len(chains)
    out['stats']['junction_points'] = junctions
    out['stats']['junction_links'] = len(link)//2
    out['stats']['exported_points'] = sum(len(c['points']) for c in chains)
    args.output.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    import numpy as np
    before = np.array([len(s['points']) for s in strokes])
    after = np.array([len(c['points']) for c in chains])
    print(f"strokes: {len(strokes)} -> {len(chains)}  (junctions {junctions}, links {len(link)//2})")
    print(f"length median: {np.median(before):.0f} -> {np.median(after):.0f} pts, "
          f"mean {before.mean():.1f} -> {after.mean():.1f}, max {before.max()} -> {after.max()}")
    print(f"strokes <=3pts: {(before<=3).sum()} -> {(after<=3).sum()}, "
          f"<=5pts: {(before<=5).sum()} -> {(after<=5).sum()}")
    print(f"total points: {before.sum()} -> {after.sum()}")

if __name__ == '__main__':
    main()
