# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: CONSOLE twin of line_to_clip_gui.spec, used only to
# verify builds headlessly over SSH (--selftest). Not shipped.
# Build:  pyinstaller --noconfirm --clean line_to_clip_gui.spec
# Result: dist/LineToClip/LineToClip.exe (+ supporting files; zip the folder)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = collect_submodules("skimage") + [
    # pipeline stages are invoked by name through importlib (line_to_clip.stage)
    "line_trace", "subpixel_refine", "chain_strokes", "detect_beta",
    "trim_strokes", "refine_strokes", "fit_curves", "render_strokes",
    "line_to_clip", "tkinterdnd2",
    # clipvec is shipped as data files and imported at runtime by path, so
    # the static analysis never sees its imports; name them explicitly
    # (sqlite3 was the one that actually broke: ModuleNotFoundError)
    "sqlite3", "uuid", "xml.sax.saxutils", "base64",
]

datas = [
    # clipvec is loaded by file path (bridge + template .clip), so its
    # sources must exist as real files inside the bundle
    ("clipvec/clipvec", "clipvec/clipvec"),
    ("clipvec/tools", "clipvec/tools"),
] + collect_data_files("tkinterdnd2")

a = Analysis(
    ["line_to_clip_gui.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "IPython", "pytest", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LineToClipCon",
    console=True,  # console twin for headless selftest verification
)
coll = COLLECT(exe, a.binaries, a.datas, name="LineToClipCon")
