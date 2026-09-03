#!/usr/bin/env python3
"""
gmtv-patch — make a GameMaker Studio 1.4 Android APK run on Android TV.

GameMaker Studio 1.4's Android runner refuses to start on TV devices with:

    FATAL ERROR in action number 1 of <Unknown Event> for object oTestKeys:
    Incorrect Android target... this executable targets Android TV devices.
    This build is for Android

The check lives in the native runner (lib/*/libyoyo.so), NOT in AndroidManifest.xml.
The runner asks PackageManager.hasSystemFeature("android.software.leanback"); if the
device says yes, it decides it is on a TV and bails out because the package was built
for the plain "Android" target. YoYo never shipped an Android TV target for 1.x
(GameMaker 2.x added one), so the documented fixes all require rebuilding from the
original project -- useless if you only have an APK.

The gate is actually TWO conditions OR'd together, and both must be neutralised:

    android.software.leanback  ->  android.software.leanbacz   (all leanback TVs)
    AMAZON                     ->  AMAZOZ                      (Amazon Fire TV)

hasSystemFeature() then finds nothing and Build.MANUFACTURER never matches, so the
runner concludes it is on an ordinary Android device and the game boots. Each
replacement is the same length, so nothing in the binary shifts. Two bytes per ABI.

It contains no game data and no GameMaker runtime -- it operates on an APK you
already have.

Usage:
    python3 gmtv-patch.py <input.apk> [-o out.apk] [--dry-run]
                          [--from-device | --abis LIST | --drop-abis LIST]
                          [--shrink-audio 128] [--install-adb] [--install-ffmpeg] [--yes]

Requires: Python 3.8+ and the `cryptography` package. **No JDK.** Both signature
schemes are implemented in pure Python -- v1/JAR in gmtv_sign.py and v2 in
gmtv_sign_v2.py. Verified by the per-run self-check, which re-derives every digest
from the finished archive, and by installing on Android 11 through 14.

Optional, and only for optional features: `adb` (--from-device, installing) and
`ffmpeg`/`vorbis-tools` (--shrink-audio). Both can be fetched for you via your
platform's package manager with --install-adb / --install-ffmpeg, which always
show the exact command and ask first.
"""

import argparse
import binascii
import os
import shutil
import struct
import subprocess
import sys
import textwrap
import zipfile
import zlib

# The runner decides "this is a TV" via TWO conditions OR'd together:
#
#     isTV = hasSystemFeature("android.software.leanback")
#            || Build.MANUFACTURER == "AMAZON"
#
# Both must be neutralised. Patching only the first leaves Amazon Fire TV blocked,
# because Fire devices report the leanback feature inconsistently and YoYo added an
# explicit vendor check to catch them.
#
# Each replacement is the same length as its original, so nothing in the binary shifts.
PATCHES = [
    (b"android.software.leanback", b"android.software.leanbacz",
     "leanback feature probe  (Shield, Bravia, all leanback TVs)"),
    (b"AMAZON", b"AMAZOZ",
     "Build.MANUFACTURER check (Amazon Fire TV)"),
]
TARGET_ENTRY = "libyoyo.so"

# (allow_install, assume_yes) for adb, set from CLI flags in main()
_ADB_INSTALL_OK = [False, False]

# Marker used to recognise a GameMaker Android package before touching anything.
GM_MARKERS = ("assets/game.droid", "libyoyo.so")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def raw_entry(fp, info):
    """Return the entry's stored (still-compressed) bytes, straight from the file.

    The local header may declare different filename/extra lengths than the central
    directory, so the data offset has to be read from the local header itself.
    """
    fp.seek(info.header_offset)
    lh = fp.read(30)
    if lh[:4] != b"PK\x03\x04":
        die(f"bad local header for {info.filename}")
    name_len, extra_len = struct.unpack("<HH", lh[26:30])
    fp.seek(info.header_offset + 30 + name_len + extra_len)
    return fp.read(info.compress_size)


def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def patch_blob(blob):
    """Replace only the standalone, NUL-terminated feature-name constant.

    The runner's .rodata holds the name twice: once as a bare C string (the argument
    handed to hasSystemFeature(), which is the actual gate) and once inside the log
    format string "android.software.leanback = %d\\n". Only the first is rewritten --
    leaving the format string intact keeps that diagnostic line readable in logcat,
    which is exactly how you confirm the patch took effect:

        I/yoyo: android.software.leanback = 0     <- 0 means the patch is live

    Also neutralises Build.MANUFACTURER == "AMAZON", the second half of the gate,
    without disturbing the unrelated GameMaker OS constant "os_amazon". A constant
    qualifies only if it is NUL-terminated *and* NUL-preceded -- a whole string in
    .rodata, never a fragment of a longer one.

    Returns (patched_bytes, {needle: count}).
    """
    out = bytearray(blob)
    counts = {}
    for needle, repl, _desc in PATCHES:
        n, i = 0, out.find(needle)
        while i >= 0:
            end = i + len(needle)
            if (end < len(out) and out[end] == 0
                    and (i == 0 or out[i - 1] == 0)):
                out[i:end] = repl
                n += 1
            i = out.find(needle, i + 1)
        counts[needle] = n
    return bytes(out), counts


def find_vorbis_encoder():
    """Pick a way to produce Ogg Vorbis, or return None.

    Nothing on macOS, Windows or a stock Linux desktop ships a Vorbis *encoder* by
    default -- unlike MP3/AAC, Vorbis has no OS-level codec anywhere. So we probe,
    in order of quality:

      1. ffmpeg + libvorbis        best, but many ffmpeg builds omit libvorbis
      2. oggdec | oggenc           vorbis-tools; oggenc cannot read Ogg, hence oggdec
      3. ffmpeg native 'vorbis'    marked experimental, needs -strict -2, but it is
                                   built into essentially every ffmpeg and the output
                                   measures the same size and loudness as libvorbis

    Returns (kind, tool_path) or None.
    """
    ff = shutil.which("ffmpeg")
    if ff:
        try:
            enc = subprocess.run([ff, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=60).stdout
            if "libvorbis" in enc:
                return ("ffmpeg-libvorbis", ff)
        except Exception:
            pass
    if shutil.which("oggenc") and shutil.which("oggdec"):
        return ("oggtools", shutil.which("oggenc"))
    if ff:
        return ("ffmpeg-native", ff)
    return None


def abi_inventory(apk):
    """Return {abi: (file_count, compressed_bytes)} for everything under lib/."""
    inv = {}
    for i in zipfile.ZipFile(apk).infolist():
        parts = i.filename.split("/")
        if len(parts) >= 3 and parts[0] == "lib" and parts[1]:
            n, b = inv.get(parts[1], (0, 0))
            inv[parts[1]] = (n + 1, b + i.compress_size)
    return inv


# ABIs a device can actually load, most-capable first. Anything a modern Android TV
# or phone runs is in here; mips/mips64 are long dead and armeabi is pre-2012.
KNOWN_ABIS = ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86", "mips64", "mips")

# Plain-language context, so "can I delete this?" is answerable without research.
# (verdict, description)
ABI_NOTES = {
    "arm64-v8a": (
        "keep",
        "64-bit ARM. Standard on essentially every Android device since ~2015 -- the "
        "Nexus 9 (2014) was the first to run it. Google Play has required 64-bit builds "
        "since 2019."),
    "armeabi-v7a": (
        "keep",
        "32-bit ARM with hardware floating point. The workhorse of the 2010s, from the "
        "Nexus One era onward. This is the one most Android TV boxes and TVs actually "
        "load from a 32-bit-only APK like this -- both the NVIDIA Shield and Sony "
        "BRAVIA run it."),
    "armeabi": (
        "usually safe to drop",
        "ARMv5TE with software floating point -- Android's launch-era baseline, roughly "
        "2008-2011. Removed from the Android NDK in r17 (2018). Nothing built in the "
        "last decade needs it, though it is harmless to keep as a fallback."),
    "x86_64": (
        "drop unless you use an emulator",
        "64-bit Intel/AMD. Almost exclusively Android emulators on PCs and Chromebooks "
        "running Android apps. No mainstream phone or TV ships it."),
    "x86": (
        "drop unless you use an emulator",
        "32-bit Intel Atom. Briefly shipped in phones such as the Motorola Razr i (2012) "
        "and Asus ZenFone 2 (2015) before Intel left the mobile market in 2016. Today it "
        "mostly matters for emulators."),
    "mips64": (
        "safe to drop",
        "64-bit MIPS. Effectively no consumer device ever shipped with it. Removed from "
        "the Android NDK in r17 (2018)."),
    "mips": (
        "safe to drop",
        "MIPS. Never caught on in consumer Android -- a handful of budget tablets and "
        "set-top boxes. Removed from the Android NDK in r17 (2018)."),
}


def print_abi_notes(inv):
    """Explain what each architecture present in the APK actually is.

    Indentation matches the abi table above: 8 for the entry, 10 for its prose, and
    every line stays inside 78 columns.
    """
    print("\nnotes : what these are, and whether you need them")
    for abi in sorted(inv, key=lambda a: KNOWN_ABIS.index(a) if a in KNOWN_ABIS else 99):
        verdict, desc = ABI_NOTES.get(abi, ("unknown", "No information for this ABI."))
        print(f"\n        {abi}  --  {verdict}")
        print(textwrap.fill(desc, width=78,
                            initial_indent=" " * 10, subsequent_indent=" " * 10))


def device_abilist(serial=""):
    """Read ro.product.cpu.abilist off a connected device via adb.

    This is a local property read over the existing adb connection -- no lookup
    service, no model database. The values are the same strings Android uses for
    lib/<abi>/ directory names, so they need no translation.
    """
    adb = shutil.which("adb")
    if not adb and _ADB_INSTALL_OK[0]:
        offer_tool_install("adb", assume_yes=_ADB_INSTALL_OK[1])
        adb = shutil.which("adb")
    if not adb:
        die("adb not found on PATH -- needed for --from-device.\n"
            "       Re-run with --install-adb to install it, or select ABIs manually:\n"
            + ADB_HINTS)
    cmd = [adb] + (["-s", serial] if serial else []) + \
          ["shell", "getprop", "ro.product.cpu.abilist"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        die("could not read ABIs from the device.\n"
            f"       {(r.stderr or r.stdout).strip() or 'no response'}\n"
            "       Check `adb devices`, or pass a serial: --from-device <serial>")
    return [a.strip() for a in out.split(",") if a.strip()]


def abis_for_device(apk, serial=""):
    """Return the keep-list for a connected device, and report the reasoning.

    The device's list is what it *can* run, in preference order -- it is not what
    the APK contains. Android walks that order and loads the first ABI the APK
    actually ships, so the two have to be intersected rather than read literally.
    """
    present = set(abi_inventory(apk))
    if not present:
        return None
    dev = device_abilist(serial)
    usable = [a for a in dev if a in present]

    print(f"\ndevice: reports {','.join(dev)}")
    if not usable:
        die("this device cannot run any ABI in the APK.\n"
            f"       device supports : {', '.join(dev)}\n"
            f"       APK contains    : {', '.join(sorted(present))}")
    print(f"        will load {usable[0]}"
          + (f" (its preferred {dev[0]} is not in this APK)" if dev[0] != usable[0] else ""))
    return ",".join(usable)


def resolve_abis(apk, keep_arg, drop_arg):
    """Work out which lib/ entries to drop. Returns (drop_set, kept_list)."""
    inv = abi_inventory(apk)
    if not inv:
        return set(), []
    present = set(inv)

    if keep_arg and drop_arg:
        die("use either --abis or --drop-abis, not both")

    if keep_arg:
        keep = {a.strip() for a in keep_arg.split(",") if a.strip()}
        unknown = keep - present
        if unknown:
            die(f"--abis names ABI(s) not in this APK: {sorted(unknown)}\n"
                f"       present: {sorted(present)}")
    else:
        drop = {a.strip() for a in drop_arg.split(",") if a.strip()}
        unknown = drop - present
        if unknown:
            die(f"--drop-abis names ABI(s) not in this APK: {sorted(unknown)}\n"
                f"       present: {sorted(present)}")
        keep = present - drop

    if not keep:
        die("that would remove every ABI, leaving an APK with no native code.\n"
            f"       Keep at least one of: {sorted(present)}")

    drop_names = {n for n in zipfile.ZipFile(apk).namelist()
                  if n.startswith("lib/") and len(n.split("/")) >= 3
                  and n.split("/")[1] not in keep}
    return drop_names, sorted(keep)


def print_abi_table(apk, keep=None):
    """Show what native code the APK carries, and what each ABI costs."""
    inv = abi_inventory(apk)
    if not inv:
        return
    total = sum(b for _, b in inv.values())
    print(f"\nabis  : {len(inv)} architecture(s), {human(total)} of native code")
    for abi in sorted(inv, key=lambda a: -inv[a][1]):
        n, b = inv[abi]
        mark = "keep" if (keep is None or abi in keep) else "DROP"
        pct = 100 * b / total if total else 0
        print(f"        {abi:<14}{n:>2} file(s){human(b):>10}{pct:>7.1f}%   {mark}")
    """Find this platform's official package manager and how to get ffmpeg from it.

    Deliberately limited to real package managers -- they verify signatures and are
    what the user would type themselves. This tool never downloads a binary from a
    URL, and never bootstraps a package manager (no curl-pipe-shell for Homebrew).

    Returns (label, argv, note) or None.
    """
    p = sys.platform
    if p == "darwin":
        if shutil.which("brew"):
            return ("Homebrew", ["brew", "install", "ffmpeg"], None)
        return None
    if p == "win32":
        if shutil.which("winget"):
            return ("winget", ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                               "--accept-source-agreements", "--accept-package-agreements"],
                    "winget puts ffmpeg on PATH for NEW shells -- reopen your terminal "
                    "afterwards, then re-run this command.")
        if shutil.which("scoop"):
            return ("Scoop", ["scoop", "install", "ffmpeg"], None)
        if shutil.which("choco"):
            return ("Chocolatey", ["choco", "install", "-y", "ffmpeg"],
                    "Chocolatey usually needs an elevated (Administrator) shell.")
        return None
    # Linux and friends
    for mgr, argv, note in (
        ("apt",    ["sudo", "apt-get", "install", "-y", "ffmpeg"], None),
        ("dnf",    ["sudo", "dnf", "install", "-y", "ffmpeg"],
         "On Fedora the stock package may be 'ffmpeg-free' with reduced codecs; "
         "RPM Fusion provides the full build."),
        ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"], None),
        ("zypper", ["sudo", "zypper", "install", "-y", "ffmpeg"], None),
        ("apk",    ["sudo", "apk", "add", "ffmpeg"], None),
    ):
        if shutil.which(mgr):
            return (mgr, argv, note)
    return None


MANUAL_HINTS = """\
         macOS    brew install ffmpeg          (or: brew install vorbis-tools)
         Linux    sudo apt install ffmpeg      (or: vorbis-tools)
         Windows  winget install Gyan.FFmpeg   (or scoop/choco install ffmpeg)"""


ADB_PKG = {
    "Homebrew": ["brew", "install", "--cask", "android-platform-tools"],
    "winget": ["winget", "install", "--id", "Google.PlatformTools", "-e",
               "--accept-source-agreements", "--accept-package-agreements"],
    "Scoop": ["scoop", "install", "adb"],
    "Chocolatey": ["choco", "install", "-y", "adb"],
    "apt": ["sudo", "apt-get", "install", "-y", "android-tools-adb"],
    "dnf": ["sudo", "dnf", "install", "-y", "android-tools"],
    "pacman": ["sudo", "pacman", "-S", "--noconfirm", "android-tools"],
    "zypper": ["sudo", "zypper", "install", "-y", "android-tools"],
    "apk": ["sudo", "apk", "add", "android-tools"],
}

ADB_HINTS = """\
         macOS    brew install --cask android-platform-tools
         Linux    sudo apt install android-tools-adb   (or android-tools)
         Windows  winget install Google.PlatformTools
         any      https://developer.android.com/tools/releases/platform-tools"""


def detect_package_manager():
    """Find this platform's official package manager. Returns (label, ffmpeg_argv, note).

    Deliberately limited to real package managers -- they verify signatures and are
    what the user would type themselves. This tool never downloads a binary from a
    URL, and never bootstraps a package manager (no curl-pipe-shell for Homebrew).
    """
    p = sys.platform
    if p == "darwin":
        if shutil.which("brew"):
            return ("Homebrew", ["brew", "install", "ffmpeg"], None)
        return None
    if p == "win32":
        if shutil.which("winget"):
            return ("winget", ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                               "--accept-source-agreements", "--accept-package-agreements"],
                    "winget puts tools on PATH for NEW shells -- reopen your terminal "
                    "afterwards, then re-run this command.")
        if shutil.which("scoop"):
            return ("Scoop", ["scoop", "install", "ffmpeg"], None)
        if shutil.which("choco"):
            return ("Chocolatey", ["choco", "install", "-y", "ffmpeg"],
                    "Chocolatey usually needs an elevated (Administrator) shell.")
        return None
    for mgr, argv, note in (
        ("apt",    ["sudo", "apt-get", "install", "-y", "ffmpeg"], None),
        ("dnf",    ["sudo", "dnf", "install", "-y", "ffmpeg"],
         "On Fedora the stock package may be 'ffmpeg-free' with reduced codecs; "
         "RPM Fusion provides the full build."),
        ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"], None),
        ("zypper", ["sudo", "zypper", "install", "-y", "ffmpeg"], None),
        ("apk",    ["sudo", "apk", "add", "ffmpeg"], None),
    ):
        if shutil.which(mgr):
            return (mgr, argv, note)
    return None


def offer_tool_install(what, assume_yes=False):
    """Offer to install a missing optional tool via the platform package manager.

    `what` is "ffmpeg" or "adb". Same rules as everywhere else in this tool: official
    package managers only, the exact command is shown first, it asks and defaults to
    no, and it refuses to prompt when stdin is not a terminal.
    """
    found = detect_package_manager()
    hints = MANUAL_HINTS if what == "ffmpeg" else ADB_HINTS
    if not found:
        die(f"{what} is not installed, and no supported package manager was found.\n"
            "       Install it manually:\n" + hints)
    label, ff_argv, note = found
    argv = ff_argv if what == "ffmpeg" else ADB_PKG.get(label)
    if not argv:
        die(f"don't know how to install {what} with {label}.\n"
            "       Install it manually:\n" + hints)
    printable = " ".join(argv)

    print(f"\n  {what} was not found on this system.")
    print(f"  {label} is available and can install it:\n")
    print(f"      {printable}\n")
    if note and what == "ffmpeg":
        print(f"  Note: {note}\n")
    if what == "adb" and label == "winget":
        print("  Note: reopen your terminal afterwards so PATH picks it up.\n")

    if not assume_yes:
        if not sys.stdin.isatty():
            die(f"cannot prompt (not a terminal). Re-run with --yes to allow it,\n"
                f"       or install {what} yourself:\n" + hints)
        try:
            ans = input("  Run it now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            die(f"declined. Install {what} yourself and re-run:\n" + hints)

    print(f"\n  running: {printable}")
    r = subprocess.run(argv)
    if r.returncode != 0:
        die(f"{label} exited with status {r.returncode}. Install {what} manually:\n" + hints)
    print(f"  {what} installed.")
    return True


def offer_ffmpeg_install(assume_yes=False):
    """Offer to install ffmpeg via the platform package manager. Returns True if
    an encoder is available afterwards."""
    found = detect_package_manager()
    if not found:
        die("no Ogg Vorbis encoder available, and no supported package manager found.\n"
            "       Nothing ships one by default on macOS, Windows or most Linux.\n"
            "       Install one manually:\n" + MANUAL_HINTS)
    label, argv, note = found
    printable = " ".join(argv)

    print("\n  No Ogg Vorbis encoder found on this system.")
    print(f"  {label} is available and can install ffmpeg:\n")
    print(f"      {printable}\n")
    if note:
        print(f"  Note: {note}\n")

    if not assume_yes:
        if not sys.stdin.isatty():
            die("cannot prompt (not a terminal). Re-run with --yes to allow the install,\n"
                "       or install ffmpeg yourself:\n" + MANUAL_HINTS)
        try:
            ans = input("  Run it now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            die("declined. Install ffmpeg yourself and re-run:\n" + MANUAL_HINTS)

    print(f"\n  running: {printable}")
    r = subprocess.run(argv)
    if r.returncode != 0:
        die(f"{label} exited with status {r.returncode}. Install ffmpeg manually:\n"
            + MANUAL_HINTS)

    if find_vorbis_encoder():
        print("  ffmpeg installed and a Vorbis encoder is now available.")
        return True
    die("install finished but no Vorbis encoder is visible yet.\n"
        "       Open a new terminal so PATH refreshes, then re-run this command.")


def encode_ogg(kind, tool, src, dst, bitrate):
    """Re-encode one Ogg file. Returns True on success."""
    if kind == "ffmpeg-libvorbis":
        cmd = [tool, "-v", "error", "-y", "-i", src,
               "-c:a", "libvorbis", "-b:a", f"{bitrate}k", dst]
    elif kind == "ffmpeg-native":
        cmd = [tool, "-v", "error", "-y", "-i", src,
               "-c:a", "vorbis", "-strict", "-2", "-b:a", f"{bitrate}k", dst]
    else:  # oggtools: oggdec -> stdout wav -> oggenc
        dec = shutil.which("oggdec")
        p1 = subprocess.Popen([dec, "-Q", "-o", "-", src], stdout=subprocess.PIPE)
        p2 = subprocess.Popen([tool, "-Q", "-b", str(bitrate), "-o", dst, "-"],
                              stdin=p1.stdout, stderr=subprocess.DEVNULL)
        p1.stdout.close(); p2.communicate()
        return p2.returncode == 0 and os.path.exists(dst)
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(dst)


def shrink_audio(apk, bitrate, allow_install=False, assume_yes=False):
    """Re-encode every Ogg asset to `bitrate` kbps. Returns {name: (blob, STORED)}."""
    import concurrent.futures, tempfile
    found = find_vorbis_encoder()
    if not found:
        if allow_install:
            offer_ffmpeg_install(assume_yes)
            found = find_vorbis_encoder()
        else:
            die("no Ogg Vorbis encoder available.\n"
                "       Nothing ships one by default on macOS, Windows or most Linux.\n"
                "       Re-run with --install-ffmpeg to install it via your package\n"
                "       manager, or install it yourself:\n" + MANUAL_HINTS)
    kind, tool = found
    label = {"ffmpeg-libvorbis": "ffmpeg (libvorbis)",
             "ffmpeg-native": "ffmpeg (native vorbis encoder)",
             "oggtools": "oggdec | oggenc"}[kind]
    print(f"  encoder: {label}")

    zin = zipfile.ZipFile(apk)
    names = [n for n in zin.namelist() if n.lower().endswith(".ogg")]
    if not names:
        print("  no .ogg assets found -- nothing to shrink")
        return {}

    tmp = tempfile.mkdtemp(prefix="gmtv-audio-")
    out = {}
    try:
        srcs = {}
        for n in names:
            p = os.path.join(tmp, "in_" + os.path.basename(n))
            with open(p, "wb") as f:
                f.write(zin.read(n))
            srcs[n] = p

        def work(n):
            dst = os.path.join(tmp, "out_" + os.path.basename(n))
            ok = encode_ogg(kind, tool, srcs[n], dst, bitrate)
            return n, (open(dst, "rb").read() if ok else None)

        before = sum(os.path.getsize(p) for p in srcs.values())
        grew = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            for n, blob in ex.map(work, names):
                if blob is None:
                    print(f"  warning: failed to re-encode {n} -- keeping original")
                elif len(blob) >= os.path.getsize(srcs[n]):
                    # Some APKs already ship low-bitrate music; re-encoding upward is
                    # pure loss -- bigger file AND a second lossy generation. Keep the
                    # original for those tracks.
                    grew += 1
                else:
                    out[n] = (blob, zipfile.ZIP_STORED)  # Ogg is already compressed
        after = sum(len(b) for b, _ in out.values()) + \
                sum(os.path.getsize(srcs[n]) for n in names if n not in out)
        print(f"  {len(out)}/{len(names)} tracks re-encoded at ~{bitrate}k: "
              f"{human(before)} -> {human(after)}")
        if grew:
            print(f"  {grew} track(s) kept as-is: already at or below ~{bitrate}k, so "
                  f"re-encoding would have made them bigger")
        if not out:
            print("  this APK's music is already efficiently encoded -- "
                  "--shrink-audio has nothing to gain here")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def dos_time(info):
    """Convert ZipInfo.date_time back to the packed DOS (time, date) pair."""
    y, mo, d, h, mi, s = info.date_time
    return (h << 11) | (mi << 5) | (s // 2), ((max(y, 1980) - 1980) << 9) | (mo << 5) | d


def build(src, dst, patched, drop=(), signer=None):
    """Copy src -> dst, substituting entries in `patched` and dropping signatures.

    `patched` maps entry name -> (blob, compress_method). `drop` is a set of entry
    names to omit entirely (used by --abis).

    Untouched entries are copied as raw compressed bytes (no recompression), which
    keeps this fast and byte-faithful on a 300MB+ archive.
    """
    import gmtv_sign
    zin = zipfile.ZipFile(src)
    infos = [i for i in zin.infolist()
             if not i.filename.startswith("META-INF/") and i.filename not in drop]
    dropped = len(zin.infolist()) - len(infos)

    # Pass 1: digest each entry's UNCOMPRESSED content (what v1 signing covers),
    # streamed so a 300MB archive never lands in memory at once.
    sig_files = {}
    if signer:
        pairs = []
        for info in infos:
            if info.filename in patched:
                digest = gmtv_sign.sha256_b64(patched[info.filename][0])
            else:
                with zin.open(info.filename) as fp:
                    digest = gmtv_sign.sha256_b64_stream(fp)
            pairs.append((info.filename, digest))
        sig_files = gmtv_sign.sign_digests(pairs, signer[0], signer[1])

    central = []
    with open(src, "rb") as fin, open(dst, "wb") as out:
        # Signature files first, as jarsigner writes them.
        for signame, sigblob in sig_files.items():
            data = deflate(sigblob)
            crc = binascii.crc32(sigblob) & 0xFFFFFFFF
            nb = signame.encode()
            offset = out.tell()
            out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, zipfile.ZIP_DEFLATED,
                                  0, 0x21, crc, len(data), len(sigblob), len(nb), 0))
            out.write(nb); out.write(data)
            central.append((None, nb, zipfile.ZIP_DEFLATED, crc, len(data), len(sigblob), offset))

        for info in infos:
            if info.filename in patched:
                blob, meth = patched[info.filename]
                data = blob if meth == zipfile.ZIP_STORED else deflate(blob)
                crc = binascii.crc32(blob) & 0xFFFFFFFF
                method, csize, usize = meth, len(data), len(blob)
            else:
                data = raw_entry(fin, info)
                crc = info.CRC
                method, csize, usize = info.compress_type, info.compress_size, info.file_size

            name = info.filename.encode("utf-8")
            t, d = dos_time(info)
            # STORED entries must be aligned for Android to mmap them straight out of
            # the APK: 4 bytes in general, a full page for shared libraries. Modern
            # APKs use extractNativeLibs="false" and rely on this.
            extra = b""
            if method == zipfile.ZIP_STORED:
                align = 4096 if info.filename.endswith(".so") else 4
                head_end = out.tell() + 30 + len(name)
                extra = b"\x00" * ((align - (head_end % align)) % align)
            offset = out.tell()
            # flags forced to 0: we always write real sizes, never a data descriptor
            out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method,
                                  t, d, crc, csize, usize, len(name), len(extra)))
            out.write(name)
            out.write(extra)
            out.write(data)
            central.append((info, name, method, crc, csize, usize, offset))

        cd_start = out.tell()
        for info, name, method, crc, csize, usize, offset in central:
            # info is None for the signature entries synthesised above
            t, d = (0, 0x21) if info is None else dos_time(info)
            ext = 0 if info is None else info.external_attr
            out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, method,
                                  t, d, crc, csize, usize, len(name), 0, 0, 0, 0,
                                  ext, offset))
            out.write(name)
        cd_size = out.tell() - cd_start
        out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central),
                              cd_size, cd_start, 0))
    zin.close()
    return len(infos), dropped


NO_JRE = "Unable to locate a Java Runtime"


def ensure_key(path):
    """Load or create the signing key. Pure Python -- no JDK, no keystore.

    Returns (cert, key, reused). `reused` decides what the closing advice says:
    a reused key installs straight over the previous build and keeps save files,
    a fresh one is a different app identity and needs an uninstall first.
    """
    import gmtv_sign
    existed = os.path.exists(path)
    cert, key = gmtv_sign.load_or_create_key(path)
    print(f"  {'using existing' if existed else 'created'} signing key: {path}")
    return cert, key, existed


# Out-of-space shows up in more than one shape. The classic constant appears on
# older releases; Android 12+ fails earlier than that, when the install *session*
# is created, and surfaces a raw IOException instead -- which is what a 4GB Bravia
# actually prints:
#
#   android.os.ParcelableException: java.io.IOException:
#       Requested internal only, but not enough space
#
# Matching only on INSTALL_FAILED_INSUFFICIENT_STORAGE misses that entirely.
OUT_OF_SPACE_MARKERS = (
    "INSTALL_FAILED_INSUFFICIENT_STORAGE",
    "not enough space",
    "Requested internal only",
)


def is_out_of_space(text):
    return any(m in text for m in OUT_OF_SPACE_MARKERS)


def confirm(question, assume_yes=False):
    """Yes/no on the terminal. Refuses to assume consent when there is no tty."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def adb_devices():
    """[(serial, model)] for everything adb currently sees."""
    adb = shutil.which("adb")
    if not adb:
        return []
    out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True).stdout
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), "")
            devs.append((parts[0], model.replace("_", " ")))
    return devs


def install_to_device(apk, serial=None, assume_yes=False):
    """Install to a TV, handling the two failures these boxes actually hit.

    Mirrors what the GUI does, so the CLI is not a lesser tool:
      * Play Protect rejects re-signed sideloads silently. The adb-install
        verifier is turned off for the one install and turned straight back on.
      * A full TV is the normal failure on a 4GB box. `pm trim-caches` is offered
        -- with consent, never assumed -- and the install retried.
    """
    adb = shutil.which("adb")
    if not adb:
        die("adb not found on PATH -- needed to install.\n" + ADB_HINTS)
    pre = [adb] + (["-s", serial] if serial else [])

    def run(*args):
        return subprocess.run(pre + list(args), capture_output=True, text=True)

    print(f"\ninstalling to {serial or 'the connected device'}")
    r = run("install", "-r", apk)
    blob = r.stdout + r.stderr

    if "INSTALL_FAILED_VERIFICATION_FAILURE" in blob:
        print("  Play Protect blocked it; disabling the adb-install verifier, "
              "installing, then restoring it.")
        run("shell", "settings", "put", "global", "verifier_verify_adb_installs", "0")
        r = run("install", "-r", apk)
        blob += r.stdout + r.stderr
        run("shell", "settings", "put", "global", "verifier_verify_adb_installs", "1")

    if is_out_of_space(blob):
        free, cache = device_free_mb(serial), device_cache_mb(serial)
        print(f"\n  Not enough space: {free} MB free." if free is not None
              else "\n  Not enough space on the device.")
        if cache:
            print(f"  {cache} MB of that is cached files apps rebuild on demand.")
        print("  Clearing it touches no apps, saved games or settings.")
        if confirm("Clear the cache and retry?", assume_yes):
            freed = trim_device_caches(serial)
            if freed is None:
                print("  could not clear the cache.")
            else:
                print(f"  freed {freed} MB -- {device_free_mb(serial)} MB now free. "
                      "retrying…")
                r = run("install", "-r", apk)
                blob = r.stdout + r.stderr
        else:
            print("  skipped.")

    print(blob.strip())
    return "Success" in blob


def device_free_mb(serial=None):
    """Free space on the device's data partition, in MB (None if adb can't say)."""
    adb = shutil.which("adb")
    if not adb:
        return None
    cmd = [adb] + (["-s", serial] if serial else []) + ["shell", "df /data | tail -1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.split()
        return int(out[3]) // 1024                      # df reports 1K blocks
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return None


def device_cache_mb(serial=None):
    """How much of the device's storage is reclaimable cache, in MB."""
    adb = shutil.which("adb")
    if not adb:
        return None
    cmd = [adb] + (["-s", serial] if serial else []) + \
          ["shell", "dumpsys diskstats | grep -i 'App Cache Size'"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        return int(out.split(":")[1].strip()) // (1024 * 1024)
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return None


def trim_device_caches(serial=None):
    """Ask Android to drop cached files. Returns MB freed (None if it failed).

    `pm trim-caches` is Android's own reclaim path and is the only safe thing to
    offer here: it discards files apps registered as cache and rebuild on demand.
    It does not touch app *data*, which on a full TV is mostly save files. Nothing
    else on a stock device is safe to delete unattended -- an app's data directory
    looks like dead weight and is somebody's progress.
    """
    adb = shutil.which("adb")
    if not adb:
        return None
    before = device_free_mb(serial)
    pre = [adb] + (["-s", serial] if serial else [])
    try:
        subprocess.run(pre + ["shell", "pm trim-caches 8G"],
                       capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    after = device_free_mb(serial)
    if before is None or after is None:
        return None
    return max(0, after - before)


def target_sdk(apk):
    """Read targetSdkVersion from the binary manifest (0 if unknown)."""
    try:
        import gmtv_axml, struct as _st
        m = zipfile.ZipFile(apk).read("AndroidManifest.xml")
        pool = gmtv_axml._read_string_pool(m)
        idx = {v: k for k, v in pool.items()}
        want = idx.get("targetSdkVersion")
        if want is None:
            return 0
        for _n, ab, asize, acount in gmtv_axml._iter_elements(m):
            for i in range(acount):
                a = ab + i * asize
                _ns, name, _raw = _st.unpack_from("<III", m, a)
                _vs, _r0, dt, data = _st.unpack_from("<HBBI", m, a + 12)
                if name == want and dt == 0x10:
                    return data
    except Exception:
        pass
    return 0


def default_key_path():
    """Where to keep the signing key when --key is not given: beside this script.

    It used to be the bare name "gmtv-key.pem", which resolves against the *current
    directory* -- so running the tool from somewhere else silently minted a second
    key, and the next install refused to upgrade in place and took the player's
    save files with it.

    One absolute location, always. Deliberately not "use a key in the working
    directory if one is there", because that is the same directory-dependent
    behaviour wearing a hat: a stray file would quietly take over and break
    upgrades again. --key overrides this when you actually want a different one.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gmtv-key.pem")


def verify_signature(apk):
    """Re-derive every digest from the finished APK and check it against MANIFEST.MF.

    A genuine self-check, not a formality: it re-reads the written archive, recomputes
    each entry's SHA-256, and compares with what we claimed. A mistake in the manifest,
    the 72-byte wrap logic, or the zip writer surfaces here.
    """
    import gmtv_sign
    z = zipfile.ZipFile(apk)
    names = set(z.namelist())
    for req in ("META-INF/MANIFEST.MF", "META-INF/CERT.SF", "META-INF/CERT.RSA"):
        if req not in names:
            return False, f"missing {req}"
    man = z.read("META-INF/MANIFEST.MF").decode("utf-8")
    man = man.replace("\r\n ", "")          # unfold continuation lines
    claimed = {}
    for sec in man.split("\r\n\r\n"):
        n = d = None
        for line in sec.split("\r\n"):
            if line.startswith("Name: "): n = line[6:]
            elif line.startswith("SHA-256-Digest: "): d = line[16:]
        if n and d: claimed[n] = d
    payload = [n for n in z.namelist() if not n.startswith("META-INF/")]
    if len(claimed) != len(payload):
        return False, f"manifest lists {len(claimed)} entries, archive has {len(payload)}"
    for n in payload:
        with z.open(n) as fp:
            actual = gmtv_sign.sha256_b64_stream(fp)
        if claimed.get(n) != actual:
            return False, f"digest mismatch for {n}"
    return True, f"{len(payload)} entries verified"


def main():
    ap = argparse.ArgumentParser(
        description="Patch a GameMaker Studio 1.4 APK to run on Android TV.")
    ap.add_argument("apk", nargs="?", help="input APK (your own copy)")
    ap.add_argument("-o", "--output", help="output APK (default: <name>-tv.apk)")
    ap.add_argument("--key", "--keystore", dest="key", default=default_key_path(),
                    help="signing key (PEM, key+cert); created if missing. Defaults to "
                         "gmtv-key.pem beside this script, so re-patching upgrades in "
                         "place and keeps save files wherever you run from. Point it at "
                         "a path that does not exist to mint a fresh key instead, which "
                         "makes the result a separate app.")
    ap.add_argument("--install", nargs="?", const=True, metavar="SERIAL",
                    help="install the finished APK to a connected TV. Handles the "
                         "Play Protect verifier and offers to clear the TV's cache "
                         "if it runs out of space. Optionally pass an adb serial.")
    ap.add_argument("--new-key", action="store_true",
                    help="sign with a brand new key instead of reusing the usual one. "
                         "The result installs as a SEPARATE app: the old copy must be "
                         "uninstalled first, which deletes its save files. Without this "
                         "a re-patch upgrades in place and keeps them.")
    ap.add_argument("--list-devices", action="store_true",
                    help="list the TVs adb can currently see, then exit")
    ap.add_argument("--scan-network", action="store_true",
                    help="sweep the local network for Android TVs with wireless "
                         "debugging on, connect them, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--abis", metavar="LIST",
                    help="KEEP only these ABIs, comma-separated (e.g. armeabi-v7a,armeabi). "
                         "Needs no extra tools.")
    ap.add_argument("--drop-abis", metavar="LIST",
                    help="REMOVE these ABIs instead (e.g. mips,x86). Mutually exclusive "
                         "with --abis. Needs no extra tools.")
    ap.add_argument("--from-device", nargs="?", const="", metavar="SERIAL",
                    help="ask a connected device (adb) which ABIs it can load, keep only "
                         "those, drop the rest. Optionally pass an adb serial.")
    ap.add_argument("--list-abis", action="store_true",
                    help="show the architectures in the APK and what each costs, then exit")
    ap.add_argument("--shrink-audio", nargs="?", type=int, const=128, metavar="KBPS",
                    help="re-encode Ogg music to KBPS (default 128). Needs ffmpeg or "
                         "vorbis-tools. AM2R ships ~500kbps music that is ~76%% of the APK.")
    ap.add_argument("--orientation", choices=("landscape", "portrait", "both"),
                    help="rewrite the game's screen orientation. A portrait phone game "
                         "pillarboxes on a 16:9 TV; 'landscape' makes GameMaker rebuild "
                         "the view for the wider aspect (it re-lays out, it does not "
                         "stretch). No effect on games already in that orientation.")
    ap.add_argument("--install-adb", action="store_true",
                    help="if adb is needed but missing, offer to install it via this "
                         "platform's package manager. Always asks first.")
    ap.add_argument("--install-ffmpeg", action="store_true",
                    help="if no Vorbis encoder is found, offer to install ffmpeg via this "
                         "platform's package manager (brew / winget / apt / dnf / pacman). "
                         "Always shows the exact command and asks first.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="answer yes to the --install-ffmpeg prompt (for scripts/CI)")
    args = ap.parse_args()

    if not (args.list_devices or args.scan_network) and not args.apk:
        ap.error("an APK is required (or use --list-devices / --scan-network)")

    if args.list_devices:
        devs = adb_devices()
        if not devs:
            print("no devices. Is the TV on, and `adb connect <ip>:5555` done?")
        for serial, model in devs:
            print(f"  {serial:28s} {model}")
        return
    if args.scan_network:
        import gmtv_scan
        print("sweeping the local network for Android TVs…")
        found = gmtv_scan.discover()
        if not found:
            print("  nothing found. The TV needs wireless debugging enabled.")
        for d in found:
            print(f"  {d['target']:24s} {d.get('model','?')}  "
                  f"Android {d.get('release','?')}  {d.get('abilist','')}")
        return

    if not os.path.exists(args.apk):
        die(f"no such file: {args.apk}")
    out = args.output or os.path.splitext(args.apk)[0] + "-tv.apk"

    try:
        zin = zipfile.ZipFile(args.apk)
    except zipfile.BadZipFile:
        die("not a valid APK/zip")
    names = zin.namelist()

    if not any(any(m in n for m in GM_MARKERS) for n in names):
        die("this does not look like a GameMaker Android package "
            "(no game.droid / libyoyo.so found)")

    targets = [n for n in names if n.endswith(TARGET_ENTRY)]
    if not targets:
        die("no libyoyo.so in the package -- nothing to patch")

    print(f"input : {args.apk}  ({human(os.path.getsize(args.apk))}, {len(names)} entries)")
    if args.list_abis:
        print_abi_table(args.apk)
        inv = abi_inventory(args.apk)
        if inv:
            print_abi_notes(inv)
            spare = ','.join(sorted(set(inv) - {'arm64-v8a', 'armeabi-v7a', 'armeabi'}))
            print("\nnext  : let a connected device decide for you --")
            print("        python3 gmtv-patch.py <apk> --from-device")
            print("\n        or check by hand and choose yourself:")
            print("        adb shell getprop ro.product.cpu.abilist")
            if spare:
                print(f"        python3 gmtv-patch.py <apk> --drop-abis {spare}")
        return
    print(f"found : {len(targets)} runner libraries")

    patched, total = {}, 0
    for name in targets:
        blob = zin.read(name)
        new, counts = patch_blob(blob)
        hits = sum(counts.values())
        abi = name.split("/")[1] if "/" in name else name
        if hits:
            # Preserve the original storage method. Modern APKs ship .so files STORED
            # (extractNativeLibs="false") so Android can mmap them straight from the
            # archive; re-deflating them fails the install with
            # INSTALL_FAILED_INVALID_APK "Failed to extract native libraries".
            patched[name] = (new, zin.getinfo(name).compress_type)
            total += hits
            detail = ", ".join(f"{n.decode()}x{c}" for n, c in counts.items() if c)
            print(f"        {abi:<14} {hits} constant(s) -> {detail}")
        else:
            print(f"        {abi:<14} no feature constant found (left unchanged)")
    zin.close()

    if not total:
        die("the leanback feature constant was not found in any runner.\n"
            "       This APK may not use the affected GameMaker version.")

    print(f"total : {total} constant(s) replaced")

    # Resolve ABI selection BEFORE the dry-run exit, so --dry-run validates the flags
    # and reports the saving. Catching a typo'd ABI name is exactly what a dry run is for.
    _ADB_INSTALL_OK[0] = args.install_adb
    _ADB_INSTALL_OK[1] = args.yes

    drop = set()
    keep_arg = args.abis
    if args.from_device is not None:
        if args.abis or args.drop_abis:
            die("use --from-device on its own, not with --abis/--drop-abis")
        keep_arg = abis_for_device(args.apk, args.from_device)
    if keep_arg or args.drop_abis:
        drop, keep = resolve_abis(args.apk, keep_arg, args.drop_abis)
        print_abi_table(args.apk, keep)
        if drop:
            saved = sum(i.compress_size for i in zipfile.ZipFile(args.apk).infolist()
                        if i.filename in drop)
            print(f"        -> removing {len(drop)} file(s), saving ~{human(saved)}")

    if args.orientation:
        import gmtv_axml
        try:
            man = zipfile.ZipFile(args.apk).read("AndroidManifest.xml")
            cur, _vals = gmtv_axml.describe(man)
            if cur is None:
                print("\norient: no GameMaker orientation meta-data found -- skipping")
            else:
                new_man, changes = gmtv_axml.set_orientation(man, args.orientation)
                print(f"\norient: {cur} -> {args.orientation}")
                if changes:
                    for k, o, n in changes:
                        print(f"        {k:<24} {o} -> {n}")
                    patched["AndroidManifest.xml"] = (new_man, zipfile.ZIP_DEFLATED)
                else:
                    print("        already set that way -- nothing to change")
        except Exception as e:
            die(f"could not rewrite orientation: {e}")

    if args.dry_run:
        if args.shrink_audio:
            enc = find_vorbis_encoder()
            print(f"\naudio : would re-encode Ogg music to ~{args.shrink_audio}k "
                  f"({'encoder: ' + enc[0] if enc else 'NO ENCODER FOUND'})")
        print("\ndry run -- nothing written")
        return

    if args.shrink_audio:
        print(f"\naudio : re-encoding to ~{args.shrink_audio}k")
        patched.update(shrink_audio(args.apk, args.shrink_audio,
                                    allow_install=args.install_ffmpeg,
                                    assume_yes=args.yes))

    print("\nsigning")
    if args.new_key:
        # A path that cannot exist yet, so a fresh key is minted and the result
        # is a different app to Android.
        import tempfile
        args.key = os.path.join(tempfile.mkdtemp(prefix="gmtv-newkey-"), "gmtv-key.pem")
    *signer, key_reused = ensure_key(args.key)

    print(f"\nwriting {out}")
    kept, dropped = build(args.apk, out, patched, drop, signer=signer)
    print(f"  {kept} entries copied, {dropped} entr(y/ies) removed")
    ok, detail = verify_signature(out)
    if not ok:
        die(f"self-check failed: {detail}")

    # Android REQUIRES v2+ for targetSdk >= 30; a v1-only APK is rejected with
    # INSTALL_PARSE_FAILED_NO_CERTIFICATES. Add v2 when the app needs it (or when
    # the original had one), so modern APKs install too.
    tsdk = target_sdk(args.apk)
    import gmtv_sign_v2
    needs_v2 = tsdk >= 30 or gmtv_sign_v2.has_v2(args.apk)
    if needs_v2:
        try:
            n = gmtv_sign_v2.sign(out, signer[0], signer[1])
            print(f"  signed v1 + v2 (targetSdk {tsdk}) -- self-check: {detail}, "
                  f"v2 block {n} bytes")
        except Exception as e:
            die(f"v2 signing failed: {e}")
    else:
        print(f"  signed (v1/JAR, SHA-256 RSA) -- self-check: {detail}")

    print(f"\ndone: {out}  ({human(os.path.getsize(out))})")
    if args.install:
        serial = args.install if isinstance(args.install, str) else None
        if install_to_device(out, serial, args.yes):
            print("\ninstalled." + ("" if key_reused else
                  "\nSigned with a new key, so this is a separate app from any earlier copy."))
        else:
            die("install failed -- see the output above")
        return

    if key_reused:
        print("\nSigned with the same key as last time, so this installs straight over"
              "\nthe previous build and keeps its save files:")
        print("  adb install -r " + out)
    else:
        print("\nThis is a brand new signing key, so Android sees a different app."
              "\nUninstall the old copy first -- that deletes its save files:")
        print("  adb uninstall <package>")
        print("  adb install " + out)
        print("\nKeep " + args.key + " and future patches will upgrade in place instead.")
    print("\nIf install fails with INSTALL_FAILED_VERIFICATION_FAILURE, Play Protect")
    print("is rejecting it. Disable it on the device, install, then re-enable.")


if __name__ == "__main__":
    main()
