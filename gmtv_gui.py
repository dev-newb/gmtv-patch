#!/usr/bin/env python3
"""
gmtv-patch GUI — Tkinter front end, designed to ship as one file via PyInstaller.

Why Tkinter: it is in the standard library, so the frozen bundle needs no extra
runtime and stays small. Why import the patcher instead of shelling out: PyInstaller
bundles the interpreter, so there is no "python3" on the user's PATH to call -- and
importing keeps it to one process with real, live output.

Build a standalone app with:
    pyinstaller --onefile --windowed --name "AM2R TV Patcher" \
        --add-data "gmtv-patch.py:." --add-data "gmtv_sign.py:." gmtv_gui.py
"""

import io
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, font as tkfont, messagebox, ttk

APP_TITLE = "Android TV Patcher"
GITHUB_URL = "https://github.com/dev-newb"
LOGO_MAX_H = 56
START_W = 980            # opening width; form + console both fit with no clipping

# Grey explanatory text sits a point below the control labels: it reads as
# secondary, which is what it is, and the smaller face buys back real estate on
# both axes without dropping a word of guidance. The size is derived from
# TkDefaultFont rather than hardcoded -- that font is 10pt on macOS but larger
# elsewhere, so a literal size can come out BIGGER than what it sits beneath.

# ttk.PanedWindow panes take only a -weight, with no per-pane minimum, so a
# window resize can squeeze either side to nothing: the form pane starves until
# "Browse…" collapses to an empty square, or the console vanishes entirely.
FORM_MIN = 300           # entry + Browse, and the widest control row, still usable
CONSOLE_MIN = 150        # enough to read a wrapped log line


def hint_font():
    base = tkfont.nametofont("TkDefaultFont")
    return (base.actual("family"), max(8, base.actual("size") - 1))


# ---------------------------------------------------------------- patcher import

def _base_dir():
    """Where our data files live -- differs under PyInstaller (`sys._MEIPASS`)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def load_patcher():
    """Import gmtv-patch.py by path (its filename has a hyphen, so no plain import)."""
    import importlib.util
    path = os.path.join(_base_dir(), "gmtv-patch.py")
    if not os.path.exists(path):
        return None, f"gmtv-patch.py not found next to the app ({path})"
    sys.path.insert(0, _base_dir())          # so it can `import gmtv_sign`
    spec = importlib.util.spec_from_file_location("gmtv_patch", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                    # pragma: no cover - defensive
        return None, f"could not load gmtv-patch.py: {e}"
    return mod, None


# ---------------------------------------------------------------- adb helpers

def adb_path():
    return shutil.which("adb")


def adb_devices():
    adb = adb_path()
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices", "-l"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p[6:].replace("_", " ")
            devs.append((parts[0], model or parts[0]))
    return devs


# ---------------------------------------------------------------- the app

class App:
    START_W = START_W

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.patcher, self.load_err = load_patcher()
        self.apk = tk.StringVar()
        self.abi_mode = tk.StringVar(value="device")
        self.audio_on = tk.BooleanVar(value=False)
        self.bitrate = tk.StringVar(value="128")
        self.orient = tk.StringVar(value="keep")
        self.autotools = tk.BooleanVar(value=False)
        self.device = tk.StringVar()
        self.dry = tk.BooleanVar(value=False)
        self.install_after = tk.BooleanVar(value=True)
        self.abi_vars = {}
        self.devices = []
        self.busy = False
        self.log_sinks = []       # every Text widget that mirrors output
        self._build()
        # Everything that shells out (adb, ffmpeg) runs off the main thread AFTER the
        # first paint. Probing during construction leaves the user staring at an empty
        # window for as long as those subprocesses take -- which is exactly what a
        # disconnected TV or a slow ffmpeg makes happen.
        self.root.after(0, self._fit_window)
        self.root.after(1, self._register_wrapping)
        self.root.after(80, self._drain)
        self.root.after(120, self.refresh_devices)
        self.root.after(120, self._probe_tools)

    def _register_wrapping(self):
        """Find the muted description labels and make them reflow on resize.

        ttk.Label never wraps on its own -- without a wraplength it just gets
        clipped by a narrow pane, which is why text vanished when the window was
        made small. wraplength is a fixed pixel value, so it has to be recomputed
        whenever the pane width changes.
        """
        self._wrap_labels = []
        self._full_width = {self.tagline, self.envbar}

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Label) and c not in self._full_width:
                    try:
                        if str(c.cget("foreground")) == "#666":
                            self._wrap_labels.append(c)
                    except tk.TclError:
                        pass
                walk(c)

        walk(self.root)
        form = self.root.nametowidget(self.paned.panes()[0])
        form.bind("<Configure>", self._rewrap)
        self.root.bind("<Configure>", self._rewrap_status)
        self._rewrap()
        self._rewrap_status()

    def _layout_abis(self, cols):
        """Re-grid the ABI checkboxes into `cols` columns."""
        if getattr(self, "_abi_cols", None) == cols:
            return
        self._abi_cols = cols
        for i, cb in enumerate(self._abi_boxes):
            cb.grid_forget()
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=(0, 10), pady=0)

    def _clamp_sash(self, event=None):
        """Keep both panes usable when the window is dragged narrow.

        Only nudges the sash when it is already out of bounds, so dragging it
        by hand anywhere in the legal range is left alone.
        """
        try:
            total = self.paned.winfo_width()
            if total < 50:                       # not laid out yet
                return
            lo = min(FORM_MIN, max(120, total - CONSOLE_MIN))
            hi = max(lo, total - CONSOLE_MIN)
            pos = self.paned.sashpos(0)
            if pos < lo:
                self.paned.sashpos(0, lo)
            elif pos > hi:
                self.paned.sashpos(0, hi)
        except tk.TclError:
            pass

    def _rewrap_status(self, event=None):
        """The status bar spans the window, so it wraps to the window."""
        try:
            self.envbar.configure(wraplength=max(200, self.root.winfo_width() - 24))
        except tk.TclError:
            pass

    def _rewrap_head(self, event=None):
        """Wrap the tagline against the space left of the logo block."""
        try:
            avail = (event.width if event is not None else self._head.winfo_width())
            avail -= self._brand.winfo_reqwidth() + 34
            self.tagline.configure(wraplength=max(150, avail))
        except (tk.TclError, AttributeError):
            pass

    def _layout_devrow(self, rows):
        """One row when there is width for it, two when there is not."""
        if self._dev_rows == rows:
            return
        self._dev_rows = rows
        for w in (self.dev_combo, self.btn_rescan, self.btn_scan):
            w.grid_forget()
        if rows == 1:
            self.dev_combo.grid(row=0, column=0, sticky="ew")
            self.btn_rescan.grid(row=0, column=1, padx=(6, 0))
            self.btn_scan.grid(row=0, column=2, padx=(4, 0))
            self.dev_grid.columnconfigure(0, weight=1)
            self.dev_grid.columnconfigure(2, weight=0)
        else:
            self.dev_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
            self.btn_rescan.grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.btn_scan.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))
            self.dev_grid.columnconfigure(0, weight=1)
            self.dev_grid.columnconfigure(2, weight=0)

    def _rewrap(self, event=None):
        """Reflow descriptions to the current pane width."""
        try:
            form = self.root.nametowidget(self.paned.panes()[0])
        except (tk.TclError, IndexError):
            return
        width = (event.width if event is not None else form.winfo_width())
        avail = max(140, width - 84)          # LabelFrame padding + indent + slack
        for lbl in getattr(self, "_wrap_labels", []):
            try:
                lbl.configure(wraplength=avail)
            except tk.TclError:
                pass
        if getattr(self, "_abi_boxes", None):
            self._layout_abis(3 if width >= 385 else 2)
        if getattr(self, "dev_grid", None):
            self._layout_devrow(1 if width >= 410 else 2)

    def _fit_window(self):
        """Open at a size where every control and every description is fully visible.

        Sizes come from what the widgets actually ask for (winfo_req*), not a
        hardcoded guess, so this stays correct as options are added. Two details
        that bite otherwise:
          * ttk Labels do not wrap -- they get CLIPPED by a narrow pane, so the
            form pane must be at least its requested width or text goes missing.
          * a window manager reports height including the title bar, while Tk
            geometry is content-only, so a little margin avoids clipping the
            last row.
        """
        r = self.root
        r.update_idletasks()

        form = r.nametowidget(self.paned.panes()[0])
        form_need = form.winfo_reqwidth()

        want_w = max(self.START_W, form_need + 380)     # form + a usable console
        want_h = r.winfo_reqheight() + 22               # margin for the last row

        max_w = r.winfo_screenwidth() - 80
        max_h = r.winfo_screenheight() - 120
        w, h = min(want_w, max_w), min(want_h, max_h)

        x = max(0, (r.winfo_screenwidth() - w) // 2)
        y = max(0, (r.winfo_screenheight() - h) // 3)
        r.geometry(f"{w}x{h}+{x}+{y}")

        r.update_idletasks()
        try:
            # give the form what it needs, hand the rest to the console
            self.paned.sashpos(0, max(form_need, w // 2))
        except tk.TclError:
            pass

    def _load_logo(self):
        """Load assets/logo.png if present. Missing logo is not an error."""
        # Prefer a pre-scaled asset: Tk's PhotoImage can only shrink by whole
        # integers, so downscaling a ~1000px original leaves it coarse.
        base = os.path.join(_base_dir(), "assets")
        path = None
        for name in ("logo_header.png", "logo.png"):
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                path = cand
                break
        if path is None:
            return None
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            return None                      # unsupported format; just skip the logo
        # PhotoImage can only shrink by integer factors, so pick the smallest
        # factor that fits the target height.
        if img.height() > LOGO_MAX_H:
            img = img.subsample(max(1, round(img.height() / LOGO_MAX_H)))
        return img

    # ---- layout
    def _build(self):
        r = self.root
        r.title(APP_TITLE)
        r.minsize(460, 600)
        self.hint = {"foreground": "#666", "font": hint_font()}

        head = ttk.Frame(r, padding=(10, 7, 10, 3))
        head.pack(fill="x")

        # right side first so it keeps its width when the title text is long
        brand = ttk.Frame(head)
        brand.pack(side="right", anchor="ne")
        self._logo_img = self._load_logo()
        if self._logo_img is not None:
            ttk.Label(brand, image=self._logo_img).pack(anchor="e")
        link = ttk.Label(brand, text="github.com/dev-newb", foreground="#4a90d9",
                         font=hint_font(), cursor="pointinghand")
        link.pack(anchor="e", pady=(2, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_URL))

        titles = ttk.Frame(head)
        titles.pack(side="left", anchor="nw")
        ttk.Label(titles, text=APP_TITLE,
                  font=(hint_font()[0], 14, "bold")).pack(anchor="w")
        self.tagline = ttk.Label(titles, **self.hint,
                                 text="Run GameMaker games on Android TV, Fire TV, "
                                      "Shield & Google TV.")
        self.tagline.pack(anchor="w")
        # The tagline spans the whole window, not the form pane, so it wraps
        # against the space left of the logo rather than with the form hints.
        self._head, self._brand = head, brand
        head.bind("<Configure>", self._rewrap_head)

        # A PanedWindow gives a real draggable sash between the form and the
        # console, so the user owns how the horizontal space is divided.
        self.paned = ttk.PanedWindow(r, orient="horizontal")
        self.paned.pack(fill="both", expand=True)
        self.paned.bind("<Configure>", self._clamp_sash)

        body = ttk.Frame(self.paned, padding=(10, 0, 10, 0))
        self.paned.add(body, weight=1)

        # 1. APK
        f1 = ttk.LabelFrame(body, text=" 1. Choose the game's APK ", padding=7)
        f1.pack(fill="x", pady=3)
        row = ttk.Frame(f1); row.pack(fill="x")
        # width=12 is a floor, not a cap -- the entry stretches to fill. Left at
        # its 20-character default it requests 194px and squeezes "Browse…" down
        # to a blank square once the pane is narrow.
        ttk.Entry(row, textvariable=self.apk, width=12).pack(side="left", fill="x",
                                                             expand=True)
        ttk.Button(row, text="Browse…", command=self.pick_apk).pack(side="left", padx=(6, 0))
        ttk.Label(f1, **self.hint, text="Your own copy — the original is never modified."
                  ).pack(anchor="w", pady=(4, 0))

        # 2. trim
        f2 = ttk.LabelFrame(body, text=" 2. Fit it to your TV (optional) ", padding=7)
        f2.pack(fill="x", pady=3)
        ttk.Label(f2, text="Processor architectures:").pack(anchor="w")
        for val, txt in (("device", "Match the selected TV"),
                         ("manual", "Choose manually"),
                         ("all", "Keep all of them")):
            ttk.Radiobutton(f2, text=txt, value=val, variable=self.abi_mode,
                            command=self._sync).pack(anchor="w", padx=10)
        self.abi_box = ttk.Frame(f2); self.abi_box.pack(fill="x", padx=18, pady=(2, 4))
        # two rows: five ABIs on one line is what forces the pane unnecessarily wide
        self._abi_boxes = []
        for a in ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86"):
            v = tk.BooleanVar(value=a in ("arm64-v8a", "armeabi-v7a", "armeabi"))
            self.abi_vars[a] = v
            self._abi_boxes.append(ttk.Checkbutton(self.abi_box, text=a, variable=v))
        self._layout_abis(3)

        ttk.Separator(f2).pack(fill="x", pady=3)
        ttk.Checkbutton(f2, text="Re-encode music to save space",
                        variable=self.audio_on, command=self._sync).pack(anchor="w")
        ttk.Label(f2, **self.hint, text="Shrinks big soundtracks; alters audio slightly."
                  ).pack(anchor="w", padx=18)
        self.audio_box = ttk.Frame(f2); self.audio_box.pack(fill="x", padx=18, pady=(2, 0))
        ttk.Label(self.audio_box, text="Bitrate:").pack(side="left")
        ttk.Combobox(self.audio_box, textvariable=self.bitrate, width=6, state="readonly",
                     values=("96", "128", "160", "192")).pack(side="left", padx=6)
        ttk.Label(self.audio_box, text="kbps").pack(side="left")
        self.audio_note = ttk.Label(f2, **self.hint,
                                    text="Tracks already smaller are left alone.")
        self.audio_note.pack(anchor="w", padx=18)

        ttk.Separator(f2).pack(fill="x", pady=3)
        orow = ttk.Frame(f2); orow.pack(fill="x")
        ttk.Label(orow, text="Screen orientation:").pack(side="left")
        ttk.Combobox(orow, textvariable=self.orient, width=12, state="readonly",
                     values=("keep", "landscape", "portrait")).pack(side="left", padx=8)
        ttk.Label(f2, **self.hint,
                  text="Portrait games pillarbox on a TV; landscape fills it."
                  ).pack(anchor="w", pady=(2, 0))

        ttk.Separator(f2).pack(fill="x", pady=3)
        ttk.Checkbutton(f2, text="Install missing tools for me",
                        variable=self.autotools).pack(anchor="w")
        ttk.Label(f2, **self.hint, text="Fetches adb / ffmpeg via your package manager, asking first."
                  ).pack(anchor="w", padx=18)

        # 3. device
        f3 = ttk.LabelFrame(body, text=" 3. Your TV (optional) ", padding=7)
        f3.pack(fill="x", pady=3)
        # Gridded rather than packed so the two buttons can drop to their own
        # row when the pane is narrow -- packed side="left" they just get pushed
        # off the edge, and "Scan network…" disappears with no way to reach it.
        self.dev_grid = ttk.Frame(f3); self.dev_grid.pack(fill="x")
        self.dev_combo = ttk.Combobox(self.dev_grid, textvariable=self.device,
                                      state="readonly", width=14)
        self.btn_rescan = ttk.Button(self.dev_grid, text="Rescan",
                                     command=self.refresh_devices)
        self.btn_scan = ttk.Button(self.dev_grid, text="Scan network…",
                                   command=self.scan_network)
        self._dev_rows = None
        self._layout_devrow(1)
        ttk.Checkbutton(f3, text="Install to it when finished",
                        variable=self.install_after).pack(anchor="w", pady=(4, 0))
        self.dev_info = ttk.Label(f3, **self.hint, text="")
        self.dev_info.pack(anchor="w")

        # actions
        act = ttk.Frame(body); act.pack(fill="x", pady=(7, 3))

        self.go_btn = ttk.Button(act, text="Patch", command=self.run)
        self.go_btn.pack(side="left")
        ttk.Checkbutton(act, text="Preview only — write nothing",
                        variable=self.dry).pack(side="left", padx=12)
        self.status = ttk.Label(act, text="", foreground="#2a7")
        self.status.pack(side="right")

        # log
        # Console occupies the right half permanently.
        f4 = ttk.LabelFrame(self.paned, text=" Console ", padding=5)
        self.paned.add(f4, weight=1)
        self.log = tk.Text(f4, width=44, wrap="word", font=("Menlo", 10),
                           background="#111318", foreground="#cfd3dd", insertbackground="#fff")
        sb = ttk.Scrollbar(f4, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(fill="both", expand=True)
        self.log_sinks.append(self.log)
        self.log.tag_config("err", foreground="#f0907e")
        self.log.tag_config("ok", foreground="#7fd39b")

        # env bar
        self.envbar = ttk.Label(r, padding=(10, 3), **self.hint, text="")
        self.envbar.pack(fill="x", side="bottom")
        self._sync()
        self.envbar.configure(text="checking tools…")

    def _sync(self):
        state = "normal" if self.abi_mode.get() == "manual" else "disabled"
        for ch in self.abi_box.winfo_children():
            ch.configure(state=state)
        for ch in self.audio_box.winfo_children():
            ch.configure(state="normal" if self.audio_on.get() else "disabled")

    def _probe_tools(self):
        """Detect adb / audio tools off the main thread, then update the status bar."""
        def work():
            bits = ["patcher " + ("OK" if self.patcher else "MISSING"),
                    "adb " + ("OK" if adb_path() else "not installed")]
            enc = None
            try:
                if self.patcher:
                    enc = self.patcher.find_vorbis_encoder()
            except Exception:
                pass
            bits.append("audio tools " + ("OK" if enc else "not installed"))
            bits.append("signing: built in (no Java needed)")
            self.q.put(("env", "  •  ".join(bits)))
        threading.Thread(target=work, daemon=True).start()
        if self.load_err:
            self.write(self.load_err + "\n", "err")

    # ---- actions
    def pick_apk(self):
        p = filedialog.askopenfilename(title="Choose a GameMaker APK",
                                       filetypes=[("Android package", "*.apk"), ("All files", "*.*")])
        if p:
            self.apk.set(p)

    def refresh_devices(self):
        """Scan for adb devices without freezing the window."""
        self.dev_combo.configure(values=["(scanning…)"])
        self.device.set("(scanning…)")
        self.dev_info.configure(text="")
        def work():
            devs = adb_devices()
            info = ""
            if devs:
                adb, s0 = adb_path(), devs[0][0]
                try:
                    g = lambda k: subprocess.run([adb, "-s", s0, "shell", "getprop", k],
                                                 capture_output=True, text=True,
                                                 timeout=15).stdout.strip()
                    info = f"Android {g('ro.build.version.release')} — loads: {g('ro.product.cpu.abilist')}"
                except Exception:
                    info = ""
            self.q.put(("devices", devs, info))
        threading.Thread(target=work, daemon=True).start()

    def _serial(self):
        i = self.dev_combo.current()
        if 0 <= i < len(self.devices):
            return self.devices[i][0]
        return None

    def _dev_info(self):
        s = self._serial()
        if not s:
            return
        self.dev_info.configure(text="reading device…")
        def work():
            adb = adb_path()
            try:
                g = lambda k: subprocess.run([adb, "-s", s, "shell", "getprop", k],
                                             capture_output=True, text=True,
                                             timeout=15).stdout.strip()
                txt = f"Android {g('ro.build.version.release')} — loads: {g('ro.product.cpu.abilist')}"
            except Exception:
                txt = ""
            self.q.put(("devinfo", txt))
        threading.Thread(target=work, daemon=True).start()

    def write(self, text, tag=None):
        """Mirror output to the main log and to the detached console, if open."""
        for sink in list(self.log_sinks):
            try:
                sink.insert("end", text, tag or ())
                sink.see("end")
            except tk.TclError:
                self.log_sinks.remove(sink)     # window was closed underneath us

    def scan_network(self):
        """Sweep the LAN for Android devices with adb open, off the main thread."""
        self.dev_info.configure(text="scanning the network…")
        self.write("\nscanning local network for Android devices…\n", "ok")
        def work():
            try:
                sys.path.insert(0, _base_dir())
                import gmtv_scan
                found = gmtv_scan.discover(
                    progress=lambda d, t: self.q.put(("scanprog", d, t)))
            except Exception as e:
                self.q.put(("log", f"scan failed: {e}\n", "err"))
                found = []
            self.q.put(("scandone", found))
        threading.Thread(target=work, daemon=True).start()

    def run(self):
        if self.busy:
            return
        if not self.patcher:
            messagebox.showerror(APP_TITLE, self.load_err or "patcher not loaded")
            return
        if not self.apk.get():
            messagebox.showwarning(APP_TITLE, "Choose an APK first.")
            return
        self.busy = True
        self.go_btn.configure(state="disabled")
        self.status.configure(text="Working…", foreground="#888")
        self.log.delete("1.0", "end")
        threading.Thread(target=self._work, daemon=True).start()

    def _argv(self, out):
        a = [self.apk.get(), "-o", out]
        if self.dry.get():
            a.append("--dry-run")
        mode = self.abi_mode.get()
        if mode == "device" and self._serial():
            a += ["--from-device", self._serial()]
        elif mode == "manual":
            keep = [k for k, v in self.abi_vars.items() if v.get()]
            if keep:
                a += ["--abis", ",".join(keep)]
        if self.audio_on.get():
            a += ["--shrink-audio", self.bitrate.get()]
        if self.orient.get() != "keep":
            a += ["--orientation", self.orient.get()]
        if self.autotools.get():
            # only meaningful alongside the features that need those tools
            if self.audio_on.get():
                a.append("--install-ffmpeg")
            if self.abi_mode.get() == "device":
                a.append("--install-adb")
            a.append("--yes")
        # key lives beside the app so re-patches upgrade in place
        a += ["--key", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmtv-key.pem")]
        return a

    def _work(self):
        src = self.apk.get()
        out = os.path.splitext(src)[0] + "-tv.apk"
        argv = self._argv(out)
        self.q.put(("log", "$ gmtv-patch " + " ".join(argv[1:]) + "\n\n", "ok"))

        # Run the CLI's main() in-process with stdout/stderr captured live.
        code = 0
        old_argv, old_out, old_err = sys.argv, sys.stdout, sys.stderr
        sys.argv = ["gmtv-patch"] + argv
        sys.stdout = sys.stderr = _Tee(self.q)
        try:
            self.patcher.main()
        except SystemExit as e:                # die() and argparse both use this
            code = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            code = 1
            self.q.put(("log", f"\nunexpected error: {e}\n", "err"))
        finally:
            sys.argv, sys.stdout, sys.stderr = old_argv, old_out, old_err

        if code != 0:
            self.q.put(("done", "Failed — see output above", False))
            return
        if self.dry.get():
            self.q.put(("done", "Preview complete", True))
            return
        if self.install_after.get() and self._serial():
            self._install(out)
        else:
            self.q.put(("done", f"Saved: {os.path.basename(out)}", True))

    def _install(self, out):
        adb, s = adb_path(), self._serial()
        self.q.put(("log", f"\ninstalling to {s}\n", None))
        def sh(*args):
            return subprocess.run([adb, "-s", s] + list(args), capture_output=True, text=True)
        r = sh("install", "-r", out)
        blob = r.stdout + r.stderr
        if "INSTALL_FAILED_VERIFICATION_FAILURE" in blob:
            self.q.put(("log", "Play Protect blocked it; disabling the adb-install verifier, "
                               "installing, then restoring it.\n", None))
            sh("shell", "settings", "put", "global", "verifier_verify_adb_installs", "0")
            r = sh("install", "-r", out)
            blob += r.stdout + r.stderr
            sh("shell", "settings", "put", "global", "verifier_verify_adb_installs", "1")
        self.q.put(("log", blob.strip() + "\n", None))
        ok = "Success" in blob
        self.q.put(("done", "Installed to your TV" if ok else "Install failed", ok))

    def _drain(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    text, tag = rest
                    self.write(text, tag)
                elif kind == "scanprog":
                    d, t = rest
                    self.dev_info.configure(text=f"scanning… {d}/{t} addresses")
                elif kind == "scandone":
                    found = rest[0]
                    if not found:
                        self.write("no Android devices found with adb reachable.\n"
                                   "  TVs need adb enabled: Settings > About > tap Build 7x,\n"
                                   "  then Developer options > Network/Wireless debugging.\n", "err")
                        self.dev_info.configure(text="nothing found — is adb enabled on the TV?")
                    else:
                        for d in found:
                            self.write(f"  found {d['model']}  {d['target']}\n"
                                       f"    Android {d['release']} (SDK {d['sdk']})  {d['abilist']}\n", "ok")
                        self.dev_info.configure(text=f"found {len(found)} device(s)")
                        self.refresh_devices()
                elif kind == "env":
                    self.envbar.configure(text=rest[0])
                elif kind == "devinfo":
                    self.dev_info.configure(text=rest[0])
                elif kind == "devices":
                    devs, info = rest
                    self.devices = devs
                    labels = [f"{m}  ({sn})" for sn, m in devs]
                    self.dev_combo.configure(values=labels or ["(no devices found)"])
                    if labels:
                        self.dev_combo.current(0)
                        self.dev_info.configure(text=info)
                    else:
                        self.device.set("(no devices found)")
                        self.dev_info.configure(
                            text="Connect with:  adb connect <tv-ip>:5555  — then Rescan."
                            if adb_path() else
                            "adb isn't installed — patching still works, you just can't auto-install.")
                elif kind == "done":
                    msg, ok = rest
                    self.status.configure(text=msg, foreground="#2a7" if ok else "#c55")
                    self.go_btn.configure(state="normal")
                    self.busy = False
        except queue.Empty:
            pass
        self.root.after(80, self._drain)


class _Tee(io.TextIOBase):
    """Send everything the CLI prints to the GUI log, line by line."""
    def __init__(self, q):
        self.q = q
    def write(self, s):
        if s:
            tag = "err" if s.lstrip().startswith("error") else None
            self.q.put(("log", s, tag))
        return len(s)
    def flush(self):
        pass


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
