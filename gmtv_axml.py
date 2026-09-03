"""
Minimal binary AndroidManifest.xml (AXML) editing — pure Python, no apktool/JDK.

Only one operation is supported, deliberately: flipping GameMaker's orientation
meta-data. Those values are 4-byte ints, so rewriting them is a same-size edit —
no string-pool surgery, no chunk resizing, nothing downstream shifts. That keeps
this a safe, surgical change rather than a manifest rewriter.

GameMaker 1.4 stores orientation as four <meta-data> entries:

    OrientLandscape / OrientLandscapeFlipped / OrientPortrait / OrientPortraitFlipped

with -1 meaning "allowed" and 0 meaning "not allowed". A phone game shipped as
portrait-only pillarboxes on a 16:9 TV; flipping it to landscape makes GameMaker
rebuild the view for the new aspect (it re-lays out, it does not stretch).

AXML layout, for reference:
    header        type(2) headerSize(2) size(4)
    string pool   type=0x0001, then offsets[], then string data
    resource map  type=0x0180
    XML nodes     start-element = 0x0102
      node        header(8) lineNumber(4) comment(4)            = 16 bytes
      attrExt     ns(4) name(4) attrStart(2) attrSize(2) attrCount(2) id(2) class(2) style(2)
      attributes  begin at node+16+attrStart, each attrSize bytes:
                  ns(4) name(4) rawValue(4) size(2) res0(1) dataType(1) data(4)
"""

import struct

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10

ORIENT_KEYS = ("OrientLandscape", "OrientLandscapeFlipped",
               "OrientPortrait", "OrientPortraitFlipped")

# mode -> (landscape, landscapeFlipped, portrait, portraitFlipped); -1 = allowed
MODES = {
    "landscape": (-1, -1, 0, 0),
    "portrait": (0, 0, -1, -1),
    "both": (-1, -1, -1, -1),
}


def _read_string_pool(m):
    """Return {index: str} for the AXML string pool."""
    typ, hdr, size = struct.unpack_from("<HHI", m, 8)
    if typ != 0x0001:
        raise ValueError("second chunk is not a string pool")
    count, style_count, flags, strings_start, _ = struct.unpack_from("<5I", m, 16)
    utf8 = bool(flags & (1 << 8))
    offsets = struct.unpack_from(f"<{count}I", m, 8 + hdr)
    base = 8 + strings_start
    out = {}
    for i, off in enumerate(offsets):
        p = base + off
        if utf8:
            def varint(q):
                x = m[q]
                return (((x & 0x7F) << 8) | m[q + 1], q + 2) if x & 0x80 else (x, q + 1)
            _, p = varint(p)          # char count
            n, p = varint(p)          # byte count
            out[i] = m[p:p + n].decode("utf-8", "replace")
        else:
            n = struct.unpack_from("<H", m, p)[0]
            out[i] = m[p + 2:p + 2 + n * 2].decode("utf-16le", "replace")
    return out


def _iter_elements(m):
    """Yield (node_offset, attr_offset, attr_size, attr_count) for start-elements."""
    pos = 8
    total = len(m)
    while pos + 8 <= total:
        typ, hdr, size = struct.unpack_from("<HHI", m, pos)
        if size <= 0:
            break
        if typ == 0x0102:                                  # RES_XML_START_ELEMENT
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", m, pos + 24)
            yield pos, pos + 16 + attr_start, attr_size, attr_count
        pos += size


def find_orientation(m):
    """Return {key: (data_offset, current_value)} for the four Orient meta-data entries."""
    pool = _read_string_pool(m)
    want = {i: s for i, s in pool.items() if s in ORIENT_KEYS}
    found = {}
    for _node, ab, asize, acount in _iter_elements(m):
        attrs = []
        for i in range(acount):
            a = ab + i * asize
            ns, name, raw = struct.unpack_from("<III", m, a)
            vsize, res0, dtype, data = struct.unpack_from("<HBBI", m, a + 12)
            attrs.append({"name": name, "dtype": dtype, "data": data,
                          "data_off": a + 12 + 4})
        # a GameMaker orientation entry is <meta-data android:name="Orient…" android:value="-1"/>
        key = None
        for at in attrs:
            if at["dtype"] == TYPE_STRING and at["data"] in want:
                key = want[at["data"]]
                break
        if not key:
            continue
        for at in attrs:
            if at["dtype"] == TYPE_INT_DEC:
                signed = struct.unpack("<i", struct.pack("<I", at["data"]))[0]
                found[key] = (at["data_off"], signed)
                break
    return found


def set_orientation(m, mode):
    """Rewrite the four orientation ints. Returns (new_bytes, [(key, old, new)])."""
    if mode not in MODES:
        raise ValueError(f"unknown orientation {mode!r}; use one of {sorted(MODES)}")
    land, land_f, port, port_f = MODES[mode]
    targets = {"OrientLandscape": land, "OrientLandscapeFlipped": land_f,
               "OrientPortrait": port, "OrientPortraitFlipped": port_f}
    found = find_orientation(m)
    if not found:
        return bytes(m), []
    out = bytearray(m)
    changes = []
    for key, (off, old) in found.items():
        new = targets[key]
        if old != new:
            struct.pack_into("<i", out, off, new)
            changes.append((key, old, new))
    return bytes(out), changes


def describe(m):
    """Human-readable current orientation, for reporting."""
    f = find_orientation(m)
    if not f:
        return None, {}
    vals = {k: v[1] for k, v in f.items()}
    land = vals.get("OrientLandscape", 0) != 0
    port = vals.get("OrientPortrait", 0) != 0
    name = ("landscape" if land and not port else
            "portrait" if port and not land else
            "both" if land and port else "none")
    return name, vals


# --------------------------------------------------------------------------
# Adding a TV launcher entry.
#
# Everything above is a same-size edit. This is not: Android TV only lists apps
# whose launcher activity declares android.intent.category.LEANBACK_LAUNCHER,
# that string is not in a phone game's manifest, and adding it means growing the
# string pool and inserting nodes -- so the manifest gets rebuilt rather than
# patched. The game's code and assets are untouched; this is one 5KB file.
#
# Two things keep it tractable:
#   * The new string is appended to the END of the pool, so no existing index
#     moves and nothing else in the file has to be renumbered. (Inserting an
#     *attribute* name would not be so lucky -- those occupy the first N indices
#     in lockstep with the RESOURCE_MAP chunk, and adding one shifts everything.)
#   * The new <category> is a byte-for-byte clone of the <category> already in
#     the launcher intent-filter, with one field changed: the string index its
#     name points at. Cloning inherits the correct namespace, attribute layout
#     and sizes instead of inventing them.

CHUNK_START_TAG = 0x0102
CHUNK_END_TAG = 0x0103
LEANBACK = "android.intent.category.LEANBACK_LAUNCHER"
LAUNCHER = "android.intent.category.LAUNCHER"


def _pool_parts(m):
    """(strings, flags, header_size, chunk_size) for the string pool."""
    typ, hdr, size = struct.unpack_from("<HHI", m, 8)
    if typ != 0x0001:
        raise ValueError("second chunk is not a string pool")
    count, styles, flags, strings_start, _ = struct.unpack_from("<5I", m, 16)
    if styles:
        raise ValueError("styled string pool -- not handled")
    return _read_string_pool(m), flags, hdr, size, count


def _build_pool(strings, flags):
    """Serialise a string pool chunk. Keeps the original UTF-8/UTF-16 encoding."""
    utf8 = bool(flags & (1 << 8))
    blob, offsets = bytearray(), []
    for s in strings:
        offsets.append(len(blob))
        if utf8:
            raw = s.encode("utf-8")
            if len(s) > 0x7F or len(raw) > 0x7F:
                raise ValueError("long UTF-8 string needs 2-byte varints")
            blob += bytes([len(s), len(raw)]) + raw + b"\x00"
        else:
            raw = s.encode("utf-16-le")
            if len(s) > 0x7FFF:
                raise ValueError("string too long")
            blob += struct.pack("<H", len(s)) + raw + b"\x00\x00"
    while len(blob) % 4:
        blob += b"\x00"
    hdr = 28
    strings_start = hdr + 4 * len(strings)
    size = strings_start + len(blob)
    out = struct.pack("<HHI", 0x0001, hdr, size)
    out += struct.pack("<5I", len(strings), 0, flags, strings_start, 0)
    out += b"".join(struct.pack("<I", o) for o in offsets)
    return out + bytes(blob)


def _chunks(m):
    """[(type, start, size)] for every chunk after the string pool."""
    _, _, pool_size = struct.unpack_from("<HHI", m, 8)
    off, out = 8 + pool_size, []
    while off < len(m) - 8:
        t, _hs, sz = struct.unpack_from("<HHI", m, off)
        if sz <= 0:
            break
        out.append((t, off, sz))
        off += sz
    return out


def _tag_name_index(m, start):
    """String index of a START_TAG's element name."""
    return struct.unpack_from("<I", m, start + 20)[0]


def _attr_string_indices(m, start):
    """[(offset_of_rawValue, offset_of_data)] for each attribute of a START_TAG."""
    # attrExt sits right after the 16-byte node header: ns(4) name(4) then
    # attributeStart(2) attributeSize(2) attributeCount(2) -- so +24, not +28.
    attr_start, attr_size, attr_count = struct.unpack_from("<HHH", m, start + 24)
    base = start + 16 + attr_start
    return [(base + i * attr_size + 8, base + i * attr_size + 16)
            for i in range(attr_count)]


def add_leanback_launcher(data):
    """Return (new_manifest, note). Idempotent; returns the input if already TV-ready."""
    m = bytes(data)
    strings, flags, _hdr, pool_size, _count = _pool_parts(m)
    by_index = strings
    if LEANBACK in by_index.values():
        return m, "already declares LEANBACK_LAUNCHER"
    launcher_idx = next((i for i, s in by_index.items() if s == LAUNCHER), None)
    if launcher_idx is None:
        return m, "no LAUNCHER category to copy -- manifest left alone"

    chunks = _chunks(m)
    # find the <category android:name="...LAUNCHER"> start tag and its end tag
    target = None
    for n, (t, start, size) in enumerate(chunks):
        if t != CHUNK_START_TAG:
            continue
        if by_index.get(_tag_name_index(m, start)) != "category":
            continue
        if any(struct.unpack_from("<I", m, raw)[0] == launcher_idx
               for raw, _d in _attr_string_indices(m, start)):
            target = n
            break
    if target is None:
        return m, "launcher <category> not found -- manifest left alone"
    if chunks[target + 1][0] != CHUNK_END_TAG:
        return m, "unexpected manifest shape -- manifest left alone"

    new_idx = len(by_index)
    ordered = [by_index[i] for i in range(len(by_index))] + [LEANBACK]

    # clone the category start tag, repoint its name attribute at the new string
    _t, s_off, s_size = chunks[target]
    clone = bytearray(m[s_off:s_off + s_size])
    for raw, dat in _attr_string_indices(m, s_off):
        if struct.unpack_from("<I", m, raw)[0] == launcher_idx:
            struct.pack_into("<I", clone, raw - s_off, new_idx)
            struct.pack_into("<I", clone, dat - s_off, new_idx)
    _t2, e_off, e_size = chunks[target + 1]
    end_clone = m[e_off:e_off + e_size]

    body = bytearray()
    for n, (_t3, off, size) in enumerate(chunks):
        body += m[off:off + size]
        if n == target + 1:                       # after the existing </category>
            body += clone + end_clone

    pool = _build_pool(ordered, flags)
    out = bytearray(struct.pack("<HHI", 0x0003, 8, 8 + len(pool) + len(body)))
    out += pool + body
    return bytes(out), f"added LEANBACK_LAUNCHER (string index {new_idx})"


def verify_manifest(data):
    """Re-read a rebuilt manifest. Raises if anything is inconsistent.

    A manifest Android cannot parse does not misbehave -- the APK simply refuses
    to install, which is a far worse failure than the same-size edits elsewhere
    in this tool can produce. So the rebuild is read back before it is accepted:
    the header must describe the real length, every chunk must walk cleanly to
    the end, and the launcher categories must both be present.
    """
    typ, hdr, size = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0003 or size != len(data):
        raise ValueError(f"bad root header: type=0x{typ:04x} size={size} actual={len(data)}")
    strings, _flags, _h, pool_size, _c = _pool_parts(data)
    off = 8 + pool_size
    while off < len(data) - 8:
        _t, _hs, sz = struct.unpack_from("<HHI", data, off)
        if sz <= 0 or off + sz > len(data):
            raise ValueError(f"chunk at {off} has bad size {sz}")
        off += sz
    if off != len(data):
        raise ValueError(f"chunks end at {off}, file is {len(data)}")
    names = set(strings.values())
    for needed in (LAUNCHER, LEANBACK):
        if needed not in names:
            raise ValueError(f"{needed} missing after rebuild")
    return True
