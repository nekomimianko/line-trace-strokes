#!/usr/bin/env python3
"""One-shot: raster line art -> CLIP STUDIO PAINT .clip vector layer.

Runs the whole pipeline with the measured-good defaults:

  line_trace.py (medial-axis) -> subpixel_refine.py (EDT centroid +
  radius re-sampling) -> chain_strokes.py -> refine_strokes.py
  (top-down subdivision thinning, conservative defaults) ->
  clipvec import

and writes a .clip whose vector layer carries every stroke with its
per-point line width, ready to edit in CLIP STUDIO PAINT.

The .clip writing itself is done by the companion project `clipvec`
(.clip format library, https://github.com/nekomimianko/clipvec),
which ships inside this repository as a git submodule in ./clipvec
(fetch it with `git clone --recursive` or `git submodule update
--init`). This script looks for it, in order: --clipvec PATH,
$CLIPVEC_PATH, ./clipvec, ../clipvec, an importable `clipvec`
package. If none is found it prints how to fetch it and stops.

Mapping into CSP (same as clipvec/tools/from_line_trace.py): pressure
is fixed at 1.0, the stroke radius is the stroke's maximum traced
radius, and each point's width factor is r_i / max_r, so the drawn
radius equals the traced radius at every point. Curve type is CSP's
native spline (uniform Catmull-Rom) with sharp turns flagged as
corners; --curve straight keeps the exact polyline instead.
Coordinates are shifted +0.5 px (the stroke JSON uses pixel-centre
coordinates, CSP uses corner origin).

Options:
  --svg PATH   also write a curve-fitted outline SVG (fit_curves.py)
  --no-chain   skip chain_strokes.py (strokes stay split at junctions)
  --tol PX     shape tolerance of the thinning step (default 0.3 px;
               raise it to smooth stroke shapes more)
  --eps-r PX   line-width tolerance of the thinning step (default
               0.15 px; raise it to smooth brush-size variation more)
  --no-refine  skip refine_strokes.py (keep every traced point)
  --keep-work DIR  keep the intermediate JSONs in DIR for inspection

Examples:
  python line_to_clip.py input.png -o output.clip
  python line_to_clip.py input.png -o output.clip --svg output.svg
"""
import argparse, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_clipvec(explicit):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("CLIPVEC_PATH"):
        candidates.append(Path(os.environ["CLIPVEC_PATH"]))
    candidates += [HERE / "clipvec", HERE.parent / "clipvec"]
    for c in candidates:
        if (c / "clipvec" / "document.py").is_file():
            return c
    try:
        import clipvec  # noqa: F401
        return None  # importable already; no path juggling needed
    except ImportError:
        pass
    raise SystemExit(
        "clipvec not found. It ships as a submodule of this repository:\n"
        "  git submodule update --init\n"
        "(or clone https://github.com/nekomimianko/clipvec into ./clipvec,\n"
        "or pass --clipvec PATH / set CLIPVEC_PATH.)")


def load_bridge(clipvec_root):
    """Load clipvec/tools/from_line_trace.py (the canonical bridge)."""
    if clipvec_root is None:
        import clipvec
        path = Path(clipvec.__file__).resolve().parent.parent / "tools" / "from_line_trace.py"
    else:
        path = clipvec_root / "tools" / "from_line_trace.py"
    spec = importlib.util.spec_from_file_location("from_line_trace", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.convert


def run(cmd):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def main():
    ap = argparse.ArgumentParser(description="raster line art -> .clip vector layer")
    ap.add_argument("image")
    ap.add_argument("-o", "--output", required=True, help="output .clip path")
    ap.add_argument("--svg", help="also write a curve-fitted outline SVG here")
    ap.add_argument("--clipvec", help="path to the clipvec repository")
    ap.add_argument("--threshold", type=int, default=180)
    ap.add_argument("--curve", choices=["spline", "straight"], default="spline")
    ap.add_argument("--dpi", type=float, default=72.0)
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--tol", type=float, default=0.3)
    ap.add_argument("--eps-r", type=float, default=0.15)
    ap.add_argument("--no-refine", action="store_true")
    ap.add_argument("--keep-work", help="directory for intermediate JSONs (default: temp)")
    a = ap.parse_args()

    clipvec_root = find_clipvec(a.clipvec)
    if clipvec_root is not None:
        sys.path.insert(0, str(clipvec_root))
    convert = load_bridge(clipvec_root)
    from clipvec.document import ClipDocument
    from clipvec.interchange import apply_dict

    work = Path(a.keep_work) if a.keep_work else Path(tempfile.mkdtemp(prefix="line_to_clip_"))
    work.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    raw = work / "strokes.json"
    sub = work / "strokes_subpixel.json"
    run([py, HERE / "line_trace.py", a.image, "-o", raw,
         "--method", "medial-axis", "--threshold", a.threshold])
    run([py, HERE / "subpixel_refine.py", a.image, raw, "-o", sub,
         "--threshold", a.threshold])
    final = sub
    if not a.no_chain:
        chained = work / "strokes_chained.json"
        run([py, HERE / "chain_strokes.py", sub, "-o", chained])
        final = chained
    if not a.no_refine:
        refined = work / "strokes_refined.json"
        run([py, HERE / "refine_strokes.py", final, "-o", refined,
             "--tol", a.tol, "--eps-r", a.eps_r])
        final = refined

    d = json.loads(final.read_text())
    data = convert(d, a.curve, 0.5, 0.0, 65.0, a.dpi)
    doc = ClipDocument.new(d["image"]["width"], d["image"]["height"], a.dpi)
    first = doc.vector_layers()[0]
    data["layers"][0] = dict(data["layers"][0], id=first.layer_id)
    apply_dict(doc, data, replace=True)
    doc.save(a.output)
    n_strokes = len(d["strokes"])
    n_points = sum(len(s["points"]) for s in d["strokes"])
    print(f"{a.output}: {n_strokes} strokes, {n_points} points, curve={a.curve}")

    if a.svg:
        run([py, HERE / "fit_curves.py", final, "-o", a.svg])
    if not a.keep_work:
        print(f"(intermediates in {work}, delete when done)")


if __name__ == "__main__":
    main()
