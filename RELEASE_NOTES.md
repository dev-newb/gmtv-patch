# gmtv-patch v1.0

Makes GameMaker Android games run on Android TV, Fire TV, NVIDIA Shield and Google TV.

Ships no game data and no GameMaker runtime — it patches a copy you already own.

---

## The problem this solves

GameMaker games built for Android refuse to start on TV devices, dying at boot with:

```
FATAL ERROR in action number 1 of <Unknown Event> for object oTestKeys:
Incorrect Android target... this executable targets Android TV devices.
This build is for Android
```

People have been stuck on this since at least 2017:

- [XDA, Nov 2018](https://xdaforums.com/t/help-request-am2r-game-dont-work-on-android-tv-how-to-fool-it.3872253/) —
  someone asks how to run AM2R on a Shield. Three posts, all their own, **zero replies**,
  ending *"So no solution? :/ It's really impossible?"*
- [GameMaker Community, 2017](https://forum.gamemaker.io/index.php?threads/apk-does-not-work-on-nvidia-shield-solved.26994/) —
  marked `[solved]`, but the fix is *rebuild the project* on an older GameMaker. Russell
  Kay of YoYo Games confirms in-thread: *"1.x does not support Android TV but 2.x does."*
- [Spelunky Classic HD issue #37](https://github.com/yancharkin/SpelunkyClassicHD/issues/37) —
  reported against a Shield, still open; the developer replied he had *"no idea how to fix
  it"* without the hardware to test on.

Every documented fix needs the original project. None help if you only have an APK.

## Why it was hard to find

The check is **not in `AndroidManifest.xml`**. That is where everyone looked — hunting for
leanback intent filters and `uses-feature` entries — which is also why generic
"Android-TV-ify an APK" repackagers don't help: they add a launcher banner, the app then
appears on the home screen, and it dies exactly as before.

The gate is compiled into the native runner, `lib/<abi>/libyoyo.so`, and it is **two**
conditions:

```c
isTV = hasSystemFeature("android.software.leanback") || Build.MANUFACTURER == "AMAZON"
```

## The fix

Corrupt both constants so neither can ever match:

```
android.software.leanback  ->  android.software.leanbacz
AMAZON                     ->  AMAZOZ
```

Same length, so nothing in the binary shifts — no offsets, no relocations, no code
changes. **Two bytes per ABI.** Only whole NUL-delimited strings are touched, which
matters twice: the log format string `"android.software.leanback = %d"` is deliberately
left intact so you can confirm the patch took (`= 0` means live), and the unrelated
GameMaker constant `os_amazon` is never disturbed.

Patching only the first condition works on Shield and Bravia but leaves **Amazon Fire TV
still blocked** — Fire devices report leanback inconsistently, which is presumably why
YoYo added the vendor check.

---

## Confirmed working

| Game | Engine era | Result |
|---|---|---|
| **AM2R 1.5.2** | GMS 1.4, targetSdk 23 | runs at a locked 59.8 fps; controller support built in |
| **Grid Run 1.1.0** | GMS 1.4, targetSdk 26 | runs; `--orientation landscape` gives true widescreen; touch-only, wants a mouse |
| **Spelunky Classic HD** | targetSdk 35, arm64, v2-signed | runs; **has a full GAMEPAD CONFIGURATION menu** |

| Device | Android | |
|---|---|---|
| NVIDIA SHIELD Android TV (2017) | 11 | ✅ |
| Sony BRAVIA 4K VH2 | 12 | ✅ |
| Chromecast with Google TV 4K | 14 | ✅ |
| Google TV Streamer | 14 | ✅ |
| Amazon Fire TV | — | untested; the `AMAZON` fix is derived from the binary, not confirmed on hardware |

**The most interesting result: two of the three games already had working controller
support.** The TV gate was the only thing stopping anyone from ever reaching it. This tool
doesn't add features — it unlocks work the original developers had already done.

---

## The other half: signing it back up

Changing one byte inside an APK invalidates its signature, and Android will not
install an unsigned package. The usual answer is to shell out to `jarsigner`, which
means anyone running the tool needs a JDK installed — a few hundred megabytes of
Java to change two bytes of a `.so`. So both signature schemes are implemented here
directly, in Python, with `cryptography` doing nothing but RSA and SHA-256.

### v1 — JAR signing

Three files, and it is a *text* format, which is where the traps are:

| file | contents |
|---|---|
| `META-INF/MANIFEST.MF` | SHA-256 of each entry's **uncompressed** content |
| `META-INF/CERT.SF` | SHA-256 of the whole manifest, plus one per manifest section |
| `META-INF/CERT.RSA` | detached PKCS#7 over CERT.SF — DER, binary, no signed attributes |

- CRLF endings throughout, and a blank line terminates every section.
- No line may exceed **72 bytes of UTF-8**. Longer ones wrap onto a continuation
  line beginning with a space — so a continuation carries 71 bytes, not 72. Entry
  names in a GameMaker APK routinely run past this.
- CERT.SF's per-entry digest is over that manifest section's **exact bytes**,
  trailing blank line included. You cannot rebuild the section later to hash it;
  the emitted bytes have to be kept as they were written.

Digests are streamed a megabyte at a time — AM2R is a 333 MB archive, and holding
every uncompressed entry in memory to hash it is not an option.

### v2 — APK Signature Scheme v2

Anything targeting SDK 30 or newer is rejected outright with a v1-only signature:

```
INSTALL_PARSE_FAILED_NO_CERTIFICATES: No signature found in package of
version 2 or newer
```

v2 is written whenever the app targets SDK 30+, and also whenever the original
already carried one, so a v2 APK never comes back downgraded. It does not sign
entries. It signs **the file itself**, as three regions, digested in 1 MB chunks:

```
[ entries ][ APK Signing Block ][ central directory ][ EOCD ]
  region 1                        region 2            region 3

chunk digest = SHA256( 0xa5 || uint32le(chunk_len) || chunk_bytes )
final digest = SHA256( 0x5a || uint32le(chunk_count) || all chunk digests )
```

The signing block is inserted *between* regions 1 and 2, which shifts the central
directory — so the EOCD's "offset of central directory" has to be rewritten. The
circularity is resolved by a rule in the spec: digest the EOCD with that field
still pointing at the original offset, then write the real one afterwards.

The bug that cost the most time was a missing level of nesting. Every field in
the block is a *length-prefixed sequence of length-prefixed records* — **two**
prefixes, not one. With a single prefix Android reads past the only record present
and reports a second one that was never there:

```
Failed to parse signature record #2: Remaining buffer too short
```

### And the zip has to survive it

Signing only holds if the archive is rebuilt byte-faithfully. Two failures came
from getting that wrong:

- Re-deflating entries the original had **stored** breaks `extractNativeLibs="false"`
  packages — `INSTALL_FAILED_INVALID_APK: Failed to extract native libraries`. The
  original compression method of every entry is preserved.
- Stored `.so` entries must start on a **4096-byte page boundary**, so they can be
  mapped straight out of the APK. Alignment padding goes in each local header's
  extra field.

### How it was checked

Every run ends by verifying its own output. The finished APK is reopened, each
entry's SHA-256 is recomputed from what was actually written, continuation lines
are unfolded, and the result is compared against `MANIFEST.MF`. A mistake in the
wrap logic or the zip writer surfaces here rather than on the TV:

```
$ gmtv-patch.py AM2R-1.5.2.apk
signing
  using existing signing key: gmtv-key.pem
writing AM2R-1.5.2-tv.apk
  132 entries copied, 3 entr(y/ies) removed
  signed v1 + v2 (targetSdk 23) -- self-check: 132 entries verified, v2 block 3662 bytes

done: AM2R-1.5.2-tv.apk  (317.3MB)
```

That is 333 MB rewritten, digested twice and signed twice, in about eleven seconds.

Not checked against `jarsigner`: the entire point was to stop needing a JDK, and
there is no Java runtime on the machine this was built on. The verification that
counts is Android's own installer — these APKs install and run on four devices
spanning Android 11 to 14, including a targetSdk 35 package the platform refuses
outright unless the v2 block parses exactly right.

---

## Features

**No JDK required.** Both signature schemes are implemented in pure Python — see
[the section above](#the-other-half-signing-it-back-up). The tool needs an
interpreter and `cryptography`, nothing else.

**Works across both GameMaker eras.** Old APKs (deflated libraries, v1 signatures) and
modern ones (stored + page-aligned libraries, v2 signatures, targetSdk 35) both work.

**Fits tight TVs.** A Sony BRAVIA had 382 MB free on a 4 GB partition and Android needs
roughly twice the APK size during install:

| Flag | Effect |
|---|---|
| `--from-device` | reads the TV's ABIs over adb and keeps only what it can load |
| `--abis` / `--drop-abis` / `--list-abis` | pick architectures by hand, with plain-language notes on each |
| `--shrink-audio [kbps]` | re-encode music; skips tracks that wouldn't get smaller |
| `--orientation landscape` | widescreen a portrait phone game |

**Widescreen.** GameMaker stores orientation as four manifest ints, so flipping them is a
same-size edit. The result is better than expected: GameMaker **rebuilds the view for the
new aspect ratio** rather than stretching, and games re-lay out their own UI. Grid Run
went from a pillarboxed strip to genuine full-screen widescreen.

**Optional helpers, never silent.** `adb` and `ffmpeg` are only needed for optional
features. `--install-adb` / `--install-ffmpeg` offer to fetch them through your platform's
package manager — official managers only, never a URL download, the exact command shown
first, defaults to no, and it refuses to prompt when stdin isn't a terminal.

**Desktop app.** `gmtv_gui.py` is a Tkinter front end covering the same options, designed
to ship as a single PyInstaller file so end users install nothing at all.

---

## Usage

```bash
python3 gmtv-patch.py YourGame.apk                        # patch, signed, ready to install
python3 gmtv-patch.py YourGame.apk --from-device          # trim to a connected TV
python3 gmtv-patch.py YourGame.apk --orientation landscape
python3 gmtv-patch.py YourGame.apk --list-abis            # what's inside, and what it costs
python3 gmtv_gui.py                                       # desktop app
```

Requires Python 3.8+ and `cryptography`. Nothing else.

**Installing:** re-signing changes the key, so uninstall any existing copy first. Keep the
generated `gmtv-key.pem` — re-patching with the same key upgrades in place and **preserves
save files**. Play Protect silently rejects re-signed sideloads with
`INSTALL_FAILED_VERIFICATION_FAILURE`; turn the adb-install verifier off, install, then
turn it back on.

---

## What this does *not* do

- **It doesn't add controller support.** Whether a game is *playable* with a gamepad
  depends on what the original developer implemented. AM2R and Spelunky have it; Grid Run
  is touch-only and wants a Bluetooth mouse. The patch makes games **run** — it can't
  write input handling that was never there.
- **It doesn't add a TV home-screen banner.** Launch from the sideloaded-apps area.
- **It can't make 32-bit games 64-bit.** GameMaker 1.4 never shipped an arm64 runner.

## Legal

This tool contains no copyrighted game content and no GameMaker runtime; it modifies a
file you supply. Whether you may *redistribute* a patched APK is a separate question and
for most games the answer is no — the runtime inside is proprietary to YoYo Games, and the
game is its author's. Patch your own copy.
