# Build the LineToClip GUI exe (Windows, folder distribution).
#
# Prerequisites (once):
#   - Python 3.11+ for Windows (with tkinter, i.e. the python.org installer)
#   - git submodule update --init   (the clipvec folder must be present)
#
# Usage (from this folder, in PowerShell):
#   python -m venv .venv-build
#   .\.venv-build\Scripts\Activate.ps1
#   pip install -r requirements.txt pyinstaller
#   .\build_windows.ps1
# Result: dist\LineToClip\LineToClip.exe  (zip the LineToClip folder to ship)
#
# Smoke test without the GUI:
#   dist\LineToClip\LineToClip.exe --selftest input.png output.clip
#   (writes output.clip and output.clip.log)

$ErrorActionPreference = "Stop"
pyinstaller --noconfirm --clean line_to_clip_gui.spec
Write-Host "Built: dist\LineToClip\LineToClip.exe"
