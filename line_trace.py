#!/usr/bin/env python3
"""Raster line art -> skeleton graph -> variable-radius strokes.

Dependencies: numpy, scipy, scikit-image, Pillow.

Output JSON stores ordered (x, y, r) point samples in pixel coordinates.
The radius comes from the Euclidean distance transform of the ORIGINAL
binary mask (never from the already-thinned skeleton).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt
from skimage.morphology import medial_axis, skeletonize


OFFSETS8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0),
            (-1, 1), (0, 1), (1, 1)]


def load_mask(path: Path, threshold: int, invert: bool) -> np.ndarray:
    """Return True for ink. Composite transparency over white first."""
    with Image.open(path) as source:
        rgba = source.convert('RGBA')
        background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        gray = np.asarray(background.convert('RGB').convert('L'))
    mask = (gray <= threshold)
    return ~mask if invert else mask


def binary_skeleton(mask: np.ndarray, method: str) -> np.ndarray:
    if method == 'medial-axis':
        # rng must be fixed: with rng=None, medial_axis seeds from OS
        # entropy and the skeleton (hence stroke counts) drifts by
        # ~0.1% from run to run.
        return medial_axis(mask, rng=0)
    return skeletonize(mask)


def graph_from_skeleton(skeleton: np.ndarray):
    """8-neighbor graph with diagonal shortcuts across L-corners suppressed.

    A diagonal between A and B is ignored if A and B are already connected
    via an occupied orthogonal neighbor. This avoids many spurious triangles
    introduced by naive 8-connectivity. It is a heuristic, not a proof that
    true visual line crossing relationships are recovered.
    """
    ys, xs = np.nonzero(skeleton)
    coords = {(int(x), int(y)) for x, y in zip(xs, ys)}
    neighbors = {p: set() for p in coords}
    for x, y in coords:
        for dx, dy in OFFSETS8:
            q = (x + dx, y + dy)
            if q not in coords:
                continue
            if dx != 0 and dy != 0:
                if (x + dx, y) in coords or (x, y + dy) in coords:
                    continue
            neighbors[(x, y)].add(q)
    return neighbors


def edge(a, b):
    return (a, b) if a < b else (b, a)


def trace_paths(neighbors):
    """Cover each graph edge exactly once; split strokes at non-degree-2 nodes.

    Handles end points (degree 1), junctions (degree >=3), isolated pixels,
    and cycles consisting entirely of degree-2 pixels.
    """
    visited = set()
    paths = []
    order = lambda p: (p[1], p[0])
    nodes = sorted((p for p, adj in neighbors.items() if len(adj) != 2), key=order)

    def follow(start, nxt):
        path = [start]
        prev, cur = start, nxt
        visited.add(edge(prev, cur))
        while True:
            path.append(cur)
            if len(neighbors[cur]) != 2:
                break
            candidates = [p for p in neighbors[cur] if p != prev]
            if not candidates:
                break
            following = candidates[0]
            e = edge(cur, following)
            if e in visited:
                break
            visited.add(e)
            prev, cur = cur, following
        closed = len(path) > 2 and path[0] == path[-1]
        if closed:
            path.pop()
        return {'closed': closed, 'pixels': path}

    for start in nodes:
        if len(neighbors[start]) == 0:
            paths.append({'closed': False, 'pixels': [start]})
        for nxt in sorted(neighbors[start], key=order):
            if edge(start, nxt) not in visited:
                paths.append(follow(start, nxt))

    # Remaining unvisited edges necessarily belong to pure degree-2 cycles
    # (unless a degenerate branching structure appears).
    for start in sorted(neighbors, key=order):
        for nxt in sorted(neighbors[start], key=order):
            if edge(start, nxt) not in visited:
                paths.append(follow(start, nxt))

    edge_count = sum(len(adj) for adj in neighbors.values()) // 2
    if len(visited) != edge_count:
        raise RuntimeError(f'Graph coverage error: {len(visited)} / {edge_count} edges')
    return paths, nodes, edge_count


def reorder_paths(paths):
    """Greedy nearest-endpoint ordering; reverses open paths when beneficial."""
    pending = list(paths)
    result = []
    cursor = (0, 0)
    while pending:
        best_index, best_reverse, best_dist = 0, False, float('inf')
        for i, item in enumerate(pending):
            pts = item['pixels']
            options = [(False, pts[0])]
            if not item['closed'] and len(pts) > 1:
                options.append((True, pts[-1]))
            for reverse, pos in options:
                d2 = (pos[0]-cursor[0])**2 + (pos[1]-cursor[1])**2
                if d2 < best_dist:
                    best_index, best_reverse, best_dist = i, reverse, d2
        item = pending.pop(best_index)
        if best_reverse:
            item = {'closed': False, 'pixels': list(reversed(item['pixels']))}
        result.append(item)
        cursor = item['pixels'][0] if item['closed'] else item['pixels'][-1]
    return result


def points_with_radius(pixels, closed, distances, spacing):
    """Sample along the skeleton pixel path; optional subpixel interpolation."""
    base = [(float(x), float(y), float(distances[y, x])) for x, y in pixels]
    if spacing <= 0 or len(base) == 1:
        return base
    sampled = [base[0]]
    pairs = list(zip(base, base[1:]))
    if closed:
        pairs.append((base[-1], base[0]))
    for a, b in pairs:
        length = math.hypot(b[0]-a[0], b[1]-a[1])
        count = max(1, int(math.ceil(length / spacing)))
        for k in range(1, count+1):
            if closed and b is base[0] and k == count:
                break
            t = k / count
            sampled.append(tuple(float(a[j] + (b[j]-a[j])*t) for j in range(3)))
    return sampled


def export_json(output, source, mask, method, threshold, invert, spacing,
                paths, nodes, edges, distances):
    strokes = []
    count = 0
    for i, path in enumerate(paths):
        raw = points_with_radius(path['pixels'], path['closed'], distances, spacing)
        points = [{'x': round(x, 4), 'y': round(y, 4), 'r': round(r, 4)}
                  for x, y, r in raw]
        strokes.append({'id': i, 'closed': path['closed'], 'points': points})
        count += len(points)
    document = {
        'format': 'variable-radius-strokes',
        'version': 1,
        'image': {'filename': source.name, 'width': int(mask.shape[1]),
                  'height': int(mask.shape[0]), 'threshold': threshold,
                  'invert': invert},
        'geometry': {
            'coordinate_unit': 'pixel',
            'pixel_center': '(0,0) is center of upper-left pixel',
            'radius_unit': 'pixel',
            'radius_definition': ('Euclidean distance from each ink pixel center '
                                  'to the nearest background pixel center '
                                  'in the original binary mask; interpolated '
                                  'between skeleton pixels when resampled'),
            'skeleton_method': method,
            'sample_spacing_px': spacing,
            'junction_handling': 'split stroke at skeleton graph nodes with degree != 2',
        },
        'stats': {'ink_pixels': int(mask.sum()),
                  'skeleton_pixels': int(sum(len(p['pixels']) for p in paths)),
                  'graph_edges': edges, 'graph_nodes_degree_not_2': len(nodes),
                  'stroke_count': len(strokes), 'exported_points': count},
        'strokes': strokes,
    }
    # The per-path sum duplicates junction pixels. Report unique skeleton pixels separately.
    document['stats']['skeleton_pixels'] = int(sum(1 for _ in
        {p for path in paths for p in path['pixels']}))
    output.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')),
                      encoding='utf-8')
    return document


def export_csv(output, strokes):
    with output.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['stroke_id', 'closed', 'point_index', 'x_px', 'y_px', 'r_px'])
        for stroke in strokes:
            for i, p in enumerate(stroke['points']):
                writer.writerow([stroke['id'], int(stroke['closed']), i,
                                 p['x'], p['y'], p['r']])


def preview_images(prefix, mask, strokes, preview_step=0.35, scale=3):
    h, w = mask.shape
    Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), 'L').save(
        str(prefix) + '_binary.png')

    # Overlay stroke order using distinct repeating colors.
    palette = [(220, 54, 68), (40, 135, 230), (34, 165, 95),
               (177, 91, 215), (220, 130, 30), (30, 158, 165)]
    overlay = Image.fromarray(np.stack([np.where(mask, 190, 255)]*3,
                                         axis=-1).astype(np.uint8), 'RGB')
    odraw = ImageDraw.Draw(overlay)
    for i, stroke in enumerate(strokes):
        coords = [(p['x']+0.5, p['y']+0.5) for p in stroke['points']]
        if stroke['closed'] and len(coords)>1:
            coords.append(coords[0])
        if len(coords) > 1:
            odraw.line(coords, fill=palette[i % len(palette)], width=1)
        else:
            cx, cy = coords[0]
            odraw.point((cx, cy), fill=palette[i % len(palette)])
    overlay.save(str(prefix) + '_paths.png')

    hi = Image.new('L', (w*scale, h*scale), 255)
    draw = ImageDraw.Draw(hi)

    def dab(x, y, r):
        # EDT measures center-to-center distance to nearest white pixel.
        # Subtract half a pixel to estimate the ink boundary (preview choice).
        rr = max(0.2, r - 0.5) * scale
        cx, cy = (x + 0.5)*scale, (y + 0.5)*scale
        draw.ellipse((cx-rr, cy-rr, cx+rr, cy+rr), fill=0)

    for stroke in strokes:
        pts = stroke['points']
        if not pts:
            continue
        dab(pts[0]['x'], pts[0]['y'], pts[0]['r'])
        pairs = list(zip(pts, pts[1:]))
        if stroke['closed'] and len(pts)>1:
            pairs.append((pts[-1], pts[0]))
        for a, b in pairs:
            length = math.hypot(b['x']-a['x'], b['y']-a['y'])
            intervals = max(1, math.ceil(length/preview_step))
            for j in range(1, intervals+1):
                t = j / intervals
                dab(a['x']+(b['x']-a['x'])*t,
                    a['y']+(b['y']-a['y'])*t,
                    a['r']+(b['r']-a['r'])*t)

    recon = hi.resize((w,h), Image.Resampling.LANCZOS)
    recon.save(str(prefix) + '_reconstructed.png')
    recon_mask = np.asarray(recon) < 128
    missed = mask & ~recon_mask
    extra = ~mask & recon_mask
    differences = np.full((h,w,3), 255, dtype=np.uint8)
    differences[missed] = (32, 105, 235)  # blue: missed original ink
    differences[extra] = (225, 50, 55)   # red: ink outside original
    Image.fromarray(differences).save(str(prefix) + '_difference.png')
    intersection = int(np.count_nonzero(mask & recon_mask))
    union = int(np.count_nonzero(mask | recon_mask))
    return {'iou': round(intersection/union, 4) if union else 1.0,
            'missing_ink_pixels': int(missed.sum()),
            'excess_ink_pixels': int(extra.sum())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('image', type=Path, help='Input raster image (PNG/JPEG/etc)')
    parser.add_argument('-o', '--output', type=Path, default=Path('strokes.json'),
                        help='Output JSON file (default: strokes.json)')
    parser.add_argument('--csv', type=Path, help='Optional CSV export')
    parser.add_argument('--threshold', type=int, default=180, metavar='0..255',
                        help='Pixels at or below this grayscale level become ink (default: 180)')
    parser.add_argument('--invert', action='store_true',
                        help='Use light pixels as ink on dark background')
    parser.add_argument('--method', choices=['skeleton', 'medial-axis'],
                        default='skeleton', help='Thinning method (default: skeleton)')
    parser.add_argument('--spacing', type=float, default=0.0,
                        help='Resample strokes at most N pixels apart; 0 means raw skeleton pixels')
    parser.add_argument('--optimize-order', action='store_true',
                        help='Greedy stroke sequencing to reduce empty pen movement')
    parser.add_argument('--preview-prefix', type=Path,
                        help='Save binary, paths, reconstructed, and difference PNGs')
    args = parser.parse_args(argv)
    if not args.image.is_file():
        parser.error(f'Image not found: {args.image}')
    if not 0 <= args.threshold <= 255:
        parser.error('--threshold must be between 0 and 255')
    if args.spacing < 0:
        parser.error('--spacing cannot be negative')
    mask = load_mask(args.image, args.threshold, args.invert)
    if not np.any(mask):
        parser.error('No foreground pixels; adjust --threshold or --invert')
    # Padding ensures background exists even when ink reaches image boundaries.
    distances = distance_transform_edt(np.pad(mask, 1, constant_values=False))[1:-1, 1:-1]
    skeleton = binary_skeleton(mask, args.method)
    graph = graph_from_skeleton(skeleton)
    paths, nodes, edges = trace_paths(graph)
    if args.optimize_order:
        paths = reorder_paths(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = export_json(args.output, args.image, mask, args.method,
                           args.threshold, args.invert, args.spacing,
                           paths, nodes, edges, distances)
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        export_csv(args.csv, document['strokes'])
    print(f"{len(paths)} strokes, {document['stats']['exported_points']} points, "
          f"{edges} graph edges. JSON: {args.output}")
    if args.preview_prefix:
        args.preview_prefix.parent.mkdir(parents=True, exist_ok=True)
        quality = preview_images(args.preview_prefix, mask, document['strokes'])
        print('Preview: '+str(args.preview_prefix)+'_{binary,paths,reconstructed,difference}.png')
        print('Preview comparison:', json.dumps(quality))
        print('Difference colors: blue=missing original ink, red=extra ink')


if __name__ == '__main__':
    main()
