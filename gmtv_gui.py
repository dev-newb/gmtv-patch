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
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Android TV Patcher"


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
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.patcher, self.load_err = load_patcher()
        self.apk = tk.StringVar()
        self.abi_mode = tk.StringVar(value="device")
        self.audio_on = tk.BooleanVar(value=False)
        self.bitrate = tk.StringVar(value="128")
        self.device = tk.StringVar()
        self.dry = tk.BooleanVar(value=False)
        self.install_after = tk.BooleanVar(value=True)
        self.abi_vars = {}
        self.devices = []
        self.busy = False
        self._build()
        # Everything that shells out (adb, ffmpeg) runs off the main thread AFTER the
        # first paint. Probing during construction leaves the user staring at an empty
        # window for as long as those subprocesses take -- which is exactly what a
        # disconnected TV or a slow ffmpeg makes happen.
        self.root.after(80, self._drain)
        self.root.after(120, self.refresh_devices)
        self.root.after(120, self._probe_tools)

    # ---- layout
    def _build(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry("760x720")
        r.minsize(640, 560)

        head = ttk.Frame(r, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text=APP_TITLE, font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(head, foreground="#666",
                  text="Make a GameMaker game run on Android TV, Fire TV, Shield and Google TV."
                  ).pack(anchor="w")

        body = ttk.Frame(r, padding=(14, 0, 14, 0))
        body.pack(fill="both", expand=True)

        # 1. APK
        f1 = ttk.LabelFrame(body, text=" 1. Choose the game's APK ", padding=10)
        f1.pack(fill="x", pady=6)
        row = ttk.Frame(f1); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.apk).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.pick_apk).pack(side="left", padx=(8, 0))
        ttk.Label(f1, foreground="#666", text="A copy you already have. The original is never modified."
                  ).pack(anchor="w", pady=(6, 0))

        # 2. trim
        f2 = ttk.LabelFrame(body, text=" 2. Make it smaller (optional) ", padding=10)
        f2.pack(fill="x", pady=6)
        ttk.Label(f2, text="Processor architectures:").pack(anchor="w")
        for val, txt in (("device", "Match the selected TV (recommended)"),
                         ("manual", "Choose manually"),
                         ("all", "Keep all of them")):
            ttk.Radiobutton(f2, text=txt, value=val, variable=self.abi_mode,
                            command=self._sync).pack(anchor="w", padx=12)
        self.abi_box = ttk.Frame(f2); self.abi_box.pack(fill="x", padx=24, pady=(4, 6))
        for a in ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86"):
            v = tk.BooleanVar(value=a in ("arm64-v8a", "armeabi-v7a", "armeabi"))
            self.abi_vars[a] = v
            ttk.Checkbutton(self.abi_box, text=a, variable=v).pack(side="left", padx=(0, 10))

        ttk.Separator(f2).pack(fill="x", pady=6)
        ttk.Checkbutton(f2, text="Re-encode music to save space  (slightly alters audio)",
                        variable=self.audio_on, command=self._sync).pack(anchor="w")
        self.audio_box = ttk.Frame(f2); self.audio_box.pack(fill="x", padx=24, pady=(4, 0))
        ttk.Label(self.audio_box, text="Bitrate:").pack(side="left")
        ttk.Combobox(self.audio_box, textvariable=self.bitrate, width=6, state="readonly",
                     values=("96", "128", "160", "192")).pack(side="left", padx=6)
        ttk.Label(self.audio_box, foreground="#666",
                  text="kbps — tracks already smaller than this are left alone"
                  ).pack(side="left")

        # 3. device
        f3 = ttk.LabelFrame(body, text=" 3. Your TV (optional) ", padding=10)
        f3.pack(fill="x", pady=6)
        row = ttk.Frame(f3); row.pack(fill="x")
        self.dev_combo = ttk.Combobox(row, textvariable=self.device, state="readonly")
        self.dev_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Rescan", command=self.refresh_devices).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(f3, text="Install to it when finished",
                        variable=self.install_after).pack(anchor="w", pady=(6, 0))
        self.dev_info = ttk.Label(f3, foreground="#666", text="")
        self.dev_info.pack(anchor="w")

        # actions
        act = ttk.Frame(body); act.pack(fill="x", pady=(10, 4))
        self.go_btn = ttk.Button(act, text="Patch", command=self.run)
        self.go_btn.pack(side="left")
        ttk.Checkbutton(act, text="Preview only (write nothing)",
                        variable=self.dry).pack(side="left", padx=12)
        self.status = ttk.Label(act, text="", foreground="#2a7")
        self.status.pack(side="right")

        # log
        f4 = ttk.LabelFrame(body, text=" Output ", padding=6)
        f4.pack(fill="both", expand=True, pady=(4, 8))
        self.log = tk.Text(f4, height=12, wrap="word", font=("Menlo", 10),
                           background="#111318", foreground="#cfd3dd", insertbackground="#fff")
        sb = ttk.Scrollbar(f4, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(fill="both", expand=True)
        self.log.tag_config("err", foreground="#f0907e")
        self.log.tag_config("ok", foreground="#7fd39b")

        # env bar
        self.envbar = ttk.Label(r, padding=(14, 4), foreground="#666", text="")
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
            self.q.put(("env", "   •   ".join(bits)))
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
        self.log.insert("end", text, tag or ())
        self.log.see("end")

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
