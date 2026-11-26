#!/usr/bin/env python3
"""line-to-clip GUI: 画像をドラッグ＆ドロップして .clip に変換する
Windows 向けの薄いラッパー。

プレビューで画像を確認し、主要パラメータだけを設定して「変換する」を
押すと、line_to_clip.py の全工程（トレース → ベタ検出 → 切り取り →
間引き → .clip 化＋ベタ塗りラスター同梱）をそのまま実行する。
変換後は結果の合成プレビュー（線＋ベタ塗り）を表示する。

配布は PyInstaller で exe 化する（build_windows.ps1 参照）。
凍結 exe では Python 本体に subprocess できないため、工程は
line_to_clip.stage() 経由でインプロセス実行される。

使い方:
    python line_to_clip_gui.py
    python line_to_clip_gui.py --selftest input.png output.clip
        （ウィンドウを出さずに変換だけ実行する。exe の動作確認用）
"""

import queue
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
from PIL import Image

# 工程モジュールを明示 import しておく（PyInstaller に確実に拾わせる）
import chain_strokes  # noqa: F401
import detect_beta  # noqa: F401
import fit_curves  # noqa: F401
import line_to_clip
import line_trace  # noqa: F401
import refine_strokes  # noqa: F401
import render_strokes  # noqa: F401
import subpixel_refine  # noqa: F401
import trim_strokes  # noqa: F401

SELFTEST = sys.argv[1:2] == ["--selftest"]
if not SELFTEST:  # --selftest では GUI ライブラリなしで動けるようにする
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from PIL import ImageTk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        Root = TkinterDnD.Tk
        HAS_DND = True
    except ImportError:
        Root = tk.Tk
        HAS_DND = False

PREVIEW = 520


def build_argv(p: dict) -> list[str]:
    """パラメータ dict から line_to_clip.main() の argv を作る。"""
    argv = [str(p["input"]), "-o", str(p["output"]),
            "--threshold", str(int(p["threshold"])),
            "--tol", str(float(p["tol"])),
            "--eps-r", str(float(p["eps_r"])),
            "--curve", p["curve"],
            "--keep-work", str(p["work"])]
    if not p["chain"]:
        argv.append("--no-chain")
    if not p["refine"]:
        argv.append("--no-refine")
    if not p["beta"]:
        argv.append("--no-beta")
    else:
        argv += ["--beta-depth", str(float(p["beta_depth"])),
                 "--min-region-area", str(int(p["min_region_area"])),
                 "--contour-tol", str(float(p["contour_tol"])),
                 "--contour-radius", str(float(p["contour_radius"]))]
        if not p["fill_layer"]:
            argv.append("--no-fill-layer")
    return argv


class QueueWriter:
    """print の出力先を GUI のログへ流すためのファイル風オブジェクト。"""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("line-trace-strokes — 画像から .clip を作る")
        root.minsize(880, 640)
        self.input_path: Path | None = None
        self.input_gray = None  # 2値化プレビュー用の元グレースケール
        self.region_mask = None  # 検出済みベタ領域（プレビューで赤く重ねる）
        self.region_count = 0
        self._region_seq = 0
        self._region_busy = False
        self._region_pending = False
        self._region_after = None
        self.photo = None
        self.log_queue: queue.Queue = queue.Queue()
        self.work_dir = Path(tempfile.mkdtemp(prefix="line_to_clip_gui_"))

        # ---- 左：プレビュー
        left = ttk.Frame(root, padding=8)
        left.pack(side="left", fill="both", expand=True)
        self.preview = ttk.Label(left, text="画像をここにドラッグ＆ドロップ\n（または下のボタンで選択）",
                                 anchor="center", relief="groove")
        self.preview.pack(fill="both", expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="画像を開く…", command=self.open_file).pack(side="left")
        self.info = ttk.Label(btns, text="")
        self.info.pack(side="left", padx=10)
        ttk.Label(left, text="ログ").pack(anchor="w")
        self.log = tk.Text(left, height=10, wrap="word", state="disabled")
        self.log.pack(fill="x")

        # ---- 右：パラメータ
        right = ttk.Frame(root, padding=8, width=320)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        def num(label, default, row, from_, to, inc=1.0):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.DoubleVar(value=default)
            ttk.Spinbox(right, textvariable=var, from_=from_, to=to, increment=inc,
                        width=10).grid(row=row, column=1, sticky="e", pady=2)
            return var

        ttk.Label(right, text="パラメータ", font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.v_threshold = num("二値化しきい値", 180, 1, 1, 254)
        self.v_threshold.trace_add("write", self._on_threshold_change)
        self.v_tol = num("間引き：形の許容誤差 (px)", 0.3, 2, 0.0, 5.0, 0.05)
        self.v_eps_r = num("間引き：線幅の許容誤差 (px)", 0.15, 3, 0.0, 5.0, 0.05)

        self.v_beta = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="黒ベタを分離する", variable=self.v_beta).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.v_beta_depth = num("ベタ判定の深さ (px)", 6.0, 5, 1.0, 30.0, 0.5)
        self.v_min_region = num("ベタの最小面積 (px)", 200, 6, 0, 100000, 50)
        for var in (self.v_beta, self.v_beta_depth, self.v_min_region):
            var.trace_add("write", self._on_beta_param_change)
        self.v_contour_tol = num("輪郭の間引き許容誤差 (px)", 0.75, 7, 0.0, 10.0, 0.05)
        self.v_contour_radius = num("輪郭線の半径 (px)", 1.0, 8, 0.2, 10.0, 0.1)
        self.v_fill = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="ベタ塗りラスターレイヤーを同梱", variable=self.v_fill).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=2)

        self.v_chain = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="ストロークをチェイン化", variable=self.v_chain).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.v_refine = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="間引きする", variable=self.v_refine).grid(
            row=11, column=0, columnspan=2, sticky="w")
        ttk.Label(right, text="曲線の種類").grid(row=12, column=0, sticky="w", pady=2)
        self.v_curve = tk.StringVar(value="spline")
        ttk.Combobox(right, textvariable=self.v_curve, values=["spline", "straight"],
                     state="readonly", width=10).grid(row=12, column=1, sticky="e")

        ttk.Label(right, text="出力 .clip").grid(row=13, column=0, sticky="w", pady=(12, 2))
        self.v_output = tk.StringVar()
        ttk.Entry(right, textvariable=self.v_output, width=30).grid(
            row=14, column=0, columnspan=2, sticky="we", pady=2)
        ttk.Button(right, text="出力先を選ぶ…", command=self.pick_output).grid(
            row=15, column=0, columnspan=2, sticky="we")

        self.run_btn = ttk.Button(right, text="変換する", command=self.run_conversion)
        self.run_btn.grid(row=16, column=0, columnspan=2, sticky="we", pady=(16, 4))
        self.status = ttk.Label(right, text="")
        self.status.grid(row=17, column=0, columnspan=2, sticky="w")

        if HAS_DND:
            self.preview.drop_target_register(DND_FILES)
            self.preview.dnd_bind("<<Drop>>", self.on_drop)
        self.root.after(100, self.drain_log)

    # ------------------------------------------------------------ files
    def on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self.set_input(Path(paths[0]))

    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("画像", "*.png *.jpg *.jpeg *.bmp *.webp"), ("すべて", "*.*")])
        if path:
            self.set_input(Path(path))

    def set_input(self, path: Path):
        try:
            img = Image.open(path)
            img.load()
        except Exception as e:
            messagebox.showerror("画像を開けません", str(e))
            return
        self.input_path = path
        self.input_gray = np.asarray(img.convert("L"))
        self.region_mask = None
        self.region_count = 0
        self.v_output.set(str(path.with_suffix(".clip")))
        self.status.config(text="")
        self.refresh_preview()
        self._schedule_region_update()

    def _on_threshold_change(self, *_args):
        self.refresh_preview()
        self._schedule_region_update()

    def _on_beta_param_change(self, *_args):
        self._schedule_region_update()

    def _schedule_region_update(self):
        """ベタ領域の再計算を少し遅らせて予約する（ spinbox の連続変更で
        計算が雪崩れないように）。"""
        if self._region_after is not None:
            self.root.after_cancel(self._region_after)
        self._region_after = self.root.after(400, self._start_region_update)

    def _start_region_update(self):
        self._region_after = None
        if self.input_gray is None:
            return
        try:
            beta_on = bool(self.v_beta.get())
            th = int(self.v_threshold.get())
            depth = float(self.v_beta_depth.get())
            min_area = int(self.v_min_region.get())
        except (tk.TclError, ValueError):
            return  # 入力途中の不正値は無視する
        if not beta_on:
            self.region_mask = None
            self.region_count = 0
            self.refresh_preview()
            return
        if self._region_busy:
            self._region_pending = True
            return
        self._region_seq += 1
        self._region_busy = True
        ink = self.input_gray <= th
        threading.Thread(target=self._region_worker,
                         args=(self._region_seq, ink, depth, min_area),
                         daemon=True).start()
        self.refresh_preview()

    def _region_worker(self, seq, ink, depth, min_area):
        try:
            regions, union = detect_beta.detect_regions(ink, depth, 30, min_area)
            self.log_queue.put(("__regions__", seq, union, len(regions)))
        except Exception:
            self.log_queue.put(("__regions__", seq, None, 0))

    def refresh_preview(self):
        """プレビューは常に2値化後の状態で出す（パイプラインの判定と
        同じく gray <= しきい値 がインク）。しきい値を変えると即追従し、
        検出済みのベタ領域は赤で重ねて示す。"""
        if self.input_gray is None or self.input_path is None:
            return
        try:
            th = int(self.v_threshold.get())
        except (tk.TclError, ValueError):
            return  # 入力途中の不正値は無視する
        ink = self.input_gray <= th
        h, w = self.input_gray.shape
        info = (f"{self.input_path.name}  {w}×{h}"
                f"  しきい値 {th}：インク {ink.mean() * 100:.1f}%")
        if self.region_mask is not None:
            reg = self.region_mask & ink
            rgb = np.full((*ink.shape, 3), 255, np.uint8)
            rgb[ink] = (0, 0, 0)
            rgb[reg] = (216, 48, 48)
            self.show_image(Image.fromarray(rgb, "RGB"))
            ratio = reg.sum() / max(ink.sum(), 1) * 100
            info += f" ／ ベタ {self.region_count}領域（インクの{ratio:.0f}%）"
        else:
            binary = np.where(ink, 0, 255).astype(np.uint8)
            self.show_image(Image.fromarray(binary, "L"))
            if self._region_busy:
                info += " ／ ベタ計算中…"
        self.info.config(text=info)

    def show_image(self, img: Image.Image):
        img = img.convert("RGB")
        img.thumbnail((PREVIEW, PREVIEW))
        self.photo = ImageTk.PhotoImage(img)
        self.preview.config(image=self.photo, text="")

    def pick_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".clip", filetypes=[("CLIP STUDIO PAINT", "*.clip")])
        if path:
            self.v_output.set(path)

    # ------------------------------------------------------------ run
    def params(self) -> dict:
        return {"input": self.input_path, "output": Path(self.v_output.get()),
                "threshold": self.v_threshold.get(), "tol": self.v_tol.get(),
                "eps_r": self.v_eps_r.get(), "curve": self.v_curve.get(),
                "work": self.work_dir, "chain": self.v_chain.get(),
                "refine": self.v_refine.get(), "beta": self.v_beta.get(),
                "beta_depth": self.v_beta_depth.get(),
                "min_region_area": self.v_min_region.get(),
                "contour_tol": self.v_contour_tol.get(),
                "contour_radius": self.v_contour_radius.get(),
                "fill_layer": self.v_fill.get()}

    def run_conversion(self):
        if self.input_path is None:
            messagebox.showwarning("画像がありません", "先に画像を選んでください。")
            return
        if not self.v_output.get():
            messagebox.showwarning("出力先がありません", "出力 .clip のパスを指定してください。")
            return
        p = self.params()
        argv = build_argv(p)
        self.run_btn.config(state="disabled")
        self.status.config(text="変換中…")
        threading.Thread(target=self._worker, args=(argv, p), daemon=True).start()

    def _worker(self, argv, p):
        old = sys.stdout
        sys.stdout = QueueWriter(self.log_queue)
        try:
            line_to_clip.main(argv)
            self.log_queue.put(("__done__", p))
        except BaseException as e:  # SystemExit も含めて拾って表示する
            self.log_queue.put(("__error__", f"{type(e).__name__}: {e}"))
        finally:
            sys.stdout = old

    def drain_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__regions__":
                    self._region_busy = False
                    if item[1] == self._region_seq:
                        self.region_mask = item[2]
                        self.region_count = item[3]
                        self.refresh_preview()
                    if self._region_pending:
                        self._region_pending = False
                        self._schedule_region_update()
                elif isinstance(item, tuple) and item[0] == "__done__":
                    self.on_done(item[1])
                elif isinstance(item, tuple) and item[0] == "__error__":
                    self.on_error(item[1])
                else:
                    self.append_log(item)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_log)

    def append_log(self, s):
        self.log.config(state="normal")
        self.log.insert("end", s)
        self.log.see("end")
        self.log.config(state="disabled")

    def on_done(self, p):
        self.run_btn.config(state="normal")
        self.status.config(text=f"完了：{p['output']}")
        self.show_result_preview(p)

    def on_error(self, msg):
        self.run_btn.config(state="normal")
        self.status.config(text="失敗しました")
        messagebox.showerror("変換に失敗しました", msg)

    def show_result_preview(self, p):
        """線レンダリング＋ベタ塗りの合成を結果プレビューとして出す。"""
        merged = self.work_dir / "strokes_merged.json"
        lines = self.work_dir / "strokes_refined.json"
        src = merged if merged.exists() else lines
        if not src.exists():
            return
        try:
            out_png = self.work_dir / "preview.png"
            render_strokes.main([str(src), "-o", str(out_png)])
            img = Image.open(out_png).convert("L")
            mask_png = self.work_dir / "beta_mask.png"
            if mask_png.exists():
                import numpy as np
                region = np.asarray(Image.open(mask_png).convert("L")) <= p["threshold"]
                arr = np.array(img)  # copy: asarray of a PIL image is read-only
                arr[region] = 0
                img = Image.fromarray(arr)
            self.show_image(img)
            self.info.config(text=f"結果プレビュー（線＋ベタ塗り）：{p['output'].name}")
        except Exception:
            pass  # プレビューは補助。失敗しても変換結果には影響しない


def selftest(image: str, output: str) -> int:
    """ウィンドウを出さずに既定値で変換する（exe の動作確認用）。
    ログは output の隣の .log ファイルにも書く（windowed exe は
    コンソールを持たないため）。"""
    import contextlib
    p = {"input": image, "output": output, "threshold": 180, "tol": 0.3,
         "eps_r": 0.15, "curve": "spline",
         "work": tempfile.mkdtemp(prefix="line_to_clip_selftest_"),
         "chain": True, "refine": True, "beta": True, "beta_depth": 6.0,
         "min_region_area": 200, "contour_tol": 0.75, "contour_radius": 1.0,
         "fill_layer": True}
    log_path = str(output) + ".log"
    real_stdout = sys.stdout
    with open(log_path, "w", encoding="utf-8") as log:

        class Tee:
            def write(self, s):
                real_stdout.write(s)
                log.write(s)

            def flush(self):
                real_stdout.flush()
                log.flush()

        with contextlib.redirect_stdout(Tee()):
            line_to_clip.main(build_argv(p))
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        raise SystemExit(selftest(args[1], args[2]))
    root = Root()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
