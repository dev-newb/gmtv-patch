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
