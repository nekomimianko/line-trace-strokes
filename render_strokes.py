#!/usr/bin/env python3
"""Render variable-radius-strokes v1 JSON into an anti-aliased PNG.

The JSON alone determines the rendered image. A reference image is OPTIONAL,
used only for comparing the final raster against the original thresholded ink.

Dependencies: Pillow, NumPy.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps


def read_strokes(path: Path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != 'variable-radius-strokes' or data.get('version') != 1:
        raise ValueError('Only variable-radius-strokes JSON, version 1, is supported.')
    info = data.get('image', {})
    w, h = int(info['width']), int(info['height'])
    if not (1 <= w <= 20000 and 1 <= h <= 20000):
        raise ValueError('Invalid image dimensions.')
    strokes = data.get('strokes')
    if not isinstance(strokes, list):
        raise ValueError('Missing strokes array.')
    return data, w, h, strokes


def render(strokes, width: int, height: int, scale: int,
           step: float, radius_offset: float, min_radius: float):
    """Same supersampled circle stamping as the original tracer preview."""
    hi = Image.new('L', (width * scale, height * scale), 255)
    draw = ImageDraw.Draw(hi)
    dab_count = 0

    def dab(p):
        nonlocal dab_count
        x, y, r = float(p['x']), float(p['y']), float(p['r'])
        if not all(math.isfinite(value) for value in (x, y, r)) or r < 0:
            raise ValueError(f'Invalid stroke point: {p}')
        radius = max(min_radius, r + radius_offset) * scale
        cx = (x + 0.5) * scale
        cy = (y + 0.5) * scale
        draw.ellipse((cx - radius, cy - radius,
                      cx + radius, cy + radius), fill=0)
        dab_count += 1

    for stroke in strokes:
        points = stroke['points']
        if not points:
            continue
        dab(points[0])
        pairs = list(zip(points, points[1:]))
        if stroke.get('closed', False) and len(points) > 1:
            pairs.append((points[-1], points[0]))
        for a, b in pairs:
            ax, ay, ar = (float(a[k]) for k in ('x', 'y', 'r'))
            bx, by, br = (float(b[k]) for k in ('x', 'y', 'r'))
            length = math.hypot(bx - ax, by - ay)
            count = max(1, math.ceil(length / step))
            for j in range(1, count + 1):
                t = j / count
                dab({'x': ax + (bx - ax) * t,
                     'y': ay + (by - ay) * t,
                     'r': ar + (br - ar) * t})

    image = hi.resize((width, height), Image.Resampling.LANCZOS)
    return image, dab_count


def threshold_reference(path, size, ink_threshold):
    with Image.open(path) as source:
        rgba = source.convert('RGBA')
        background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        gray = np.asarray(background.convert('RGB').convert('L'))
    if gray.shape != (size[1], size[0]):
        raise ValueError(f'Reference image size {gray.shape[::-1]} != JSON size {size}')
    return gray <= ink_threshold


def assess(reference_mask, output_image, difference_path=None, overlay_path=None):
    drawn_mask = np.asarray(output_image) < 128
    overlap = reference_mask & drawn_mask
    missed = reference_mask & ~drawn_mask
    excess = ~reference_mask & drawn_mask
    intersection = int(overlap.sum())
    union = int((reference_mask | drawn_mask).sum())
    ref_pixels = int(reference_mask.sum())
    drawn_pixels = int(drawn_mask.sum())
    result = {
        'iou': intersection / union if union else 1.0,
        'recall': intersection / ref_pixels if ref_pixels else 1.0,
        'precision': intersection / drawn_pixels if drawn_pixels else 1.0,
        'reference_ink_pixels': ref_pixels,
        'drawn_ink_pixels': drawn_pixels,
        'common_ink_pixels': intersection,
        'missing_ink_pixels': int(missed.sum()),
        'excess_ink_pixels': int(excess.sum()),
    }
    if difference_path:
        diff = np.full((*reference_mask.shape, 3), 255, dtype=np.uint8)
        diff[missed] = (32, 105, 235)  # blue = missing original ink
        diff[excess] = (225, 50, 55)   # red = extra reconstructed ink
        Image.fromarray(diff).save(difference_path)
    if overlay_path:
        overlay = np.full((*reference_mask.shape, 3), 255, dtype=np.uint8)
        overlay[reference_mask] = (15, 15, 15)
        overlay[missed] = (32, 105, 235)
        overlay[excess] = (225, 50, 55)
        Image.fromarray(overlay).save(overlay_path)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('json_file', type=Path, help='variable-radius-strokes JSON file')
    p.add_argument('-o', '--output', type=Path, default=Path('restored.png'),
                   help='Rendered output PNG (default: restored.png)')
    p.add_argument('--reference', type=Path,
                   help='Optional original input image, ONLY for computing differences')
    p.add_argument('--difference', type=Path, help='Missing pixels (blue), extras (red)')
    p.add_argument('--overlay', type=Path, help='Original binary ink with error colors')
    p.add_argument('--metrics', type=Path, help='Optional JSON report with comparison metrics')
    p.add_argument('--reference-threshold', type=int,
                   help='Ink threshold for original; default is JSON image.threshold')
    p.add_argument('--scale', type=int, default=3, help='Supersampling factor (default: 3)')
    p.add_argument('--step', type=float, default=0.35,
                   help='Maximum gap between circle centers (default: 0.35 px)')
    p.add_argument('--radius-offset', type=float, default=-0.5,
                   help='Radius adjustment (default: -0.5 px, as in tracer preview)')
    p.add_argument('--min-radius', type=float, default=0.2,
                   help='Minimum brush radius (default: 0.2 px)')
    args = p.parse_args(argv)
    if args.scale < 1 or args.scale > 10 or args.step <= 0 or args.min_radius < 0:
        p.error('Use 1 <= --scale <= 10, --step > 0, --min-radius >= 0')
    if (args.difference or args.overlay or args.metrics) and not args.reference:
        p.error('--difference, --overlay and --metrics require --reference')

    data, width, height, strokes = read_strokes(args.json_file)
    restored, dab_count = render(strokes, width, height,
                                  args.scale, args.step,
                                  args.radius_offset, args.min_radius)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    restored.save(args.output)
    print(f'Rendered {len(strokes)} strokes, {dab_count:,} brush dabs at {width}x{height}')
    print(f'Output: {args.output}')

    if args.reference:
        threshold = (args.reference_threshold if args.reference_threshold is not None
                     else data['image'].get('threshold', 180))
        if not 0 <= threshold <= 255:
            p.error('Reference threshold must be in [0, 255]')
        ref = threshold_reference(args.reference, (width, height), threshold)
        if data['image'].get('invert', False):
            ref = ~ref
        for out in (args.difference, args.overlay, args.metrics):
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
        stats = assess(ref, restored, args.difference, args.overlay)
        print('Comparison with binarized reference image:')
        print(f"  IoU: {stats['iou']:.6f} ({stats['iou']*100:.4f}%)")
        print(f"  Missing ink pixels: {stats['missing_ink_pixels']:,}")
        print(f"  Extra ink pixels:   {stats['excess_ink_pixels']:,}")
        if args.metrics:
            args.metrics.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n',
                                    encoding='utf-8')


if __name__ == '__main__':
    main()
