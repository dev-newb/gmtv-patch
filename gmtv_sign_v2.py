"""
APK Signature Scheme v2 — pure Python.

Android *requires* v2 (or newer) for apps targeting SDK 30+; a v1/JAR signature alone
is rejected with:

    INSTALL_PARSE_FAILED_NO_CERTIFICATES: No signature found in package of
    version 2 or newer

v1 signs each zip entry's uncompressed content. v2 instead signs the *file itself* —
three contiguous regions, digested in 1 MB chunks — and stores the result in an "APK
Signing Block" inserted between the last entry and the central directory.

    [ entries ][ APK Signing Block ][ central directory ][ EOCD ]
      region 1                        region 2            region 3

Because the block sits between regions 1 and 2, inserting it shifts the central
directory, so the EOCD's "offset of central directory" must be rewritten to match.
That, plus the chunked digest, is the whole trick.

Digest per Android's spec:
  chunk digest = SHA256( 0xa5 || uint32le(chunk_len) || chunk_bytes )
  final digest = SHA256( 0x5a || uint32le(chunk_count) || all chunk digests )

The signature covers `signed data` = the sequence of digests, certificates and
additional attributes, and is verified against the public key in the block.
"""

import hashlib
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
APK_SIGNATURE_SCHEME_V2_ID = 0x7109871A
# RSASSA-PKCS1-v1_5 with SHA2-256 — the widely supported choice
SIG_ALGO_RSA_PKCS1_SHA256 = 0x0103
CHUNK = 1024 * 1024


def _u32(v):
    return struct.pack("<I", v)


def _u64(v):
    return struct.pack("<Q", v)


def _len_prefixed(b):
    return _u32(len(b)) + b


def _find_eocd(data):
    """Return (eocd_offset, cd_offset, cd_size)."""
    # EOCD is at the end; scan back over the (usually empty) comment
    for i in range(len(data) - 22, max(-1, len(data) - 22 - 65536), -1):
        if data[i:i + 4] == b"PK\x05\x06":
            cd_size, cd_off = struct.unpack_from("<II", data, i + 12)
            return i, cd_off, cd_size
    raise ValueError("EOCD not found — not a zip?")


def _chunked_digest(regions):
    """Android's two-level chunked SHA-256 over the three APK regions."""
    digests = []
    count = 0
    for buf in regions:
        for off in range(0, len(buf), CHUNK):
            chunk = buf[off:off + CHUNK]
            h = hashlib.sha256()
            h.update(b"\xa5")
            h.update(_u32(len(chunk)))
            h.update(chunk)
            digests.append(h.digest())
            count += 1
    top = hashlib.sha256()
    top.update(b"\x5a")
    top.update(_u32(count))
    for d in digests:
        top.update(d)
    return top.digest()


def _build_v2_block(digest, cert_der, key):
    """Assemble the length-prefixed v2 structure and sign it."""
    # Every one of these is a "length-prefixed sequence of length-prefixed records",
    # i.e. TWO levels: outer sequence length, then each record's own length. Getting
    # this wrong makes Android read past the single record and complain about a
    # truncated "record #2".
    dig_record = _u32(SIG_ALGO_RSA_PKCS1_SHA256) + _len_prefixed(digest)
    digests = _len_prefixed(_len_prefixed(dig_record))
    certs = _len_prefixed(_len_prefixed(cert_der))
    attrs = _len_prefixed(b"")                       # empty sequence, no attributes
    signed_data = digests + certs + attrs

    signature = key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    sig_record = _u32(SIG_ALGO_RSA_PKCS1_SHA256) + _len_prefixed(signature)
    signatures = _len_prefixed(_len_prefixed(sig_record))
    public_key = _len_prefixed(
        key.public_key().public_bytes(serialization.Encoding.DER,
                                      serialization.PublicFormat.SubjectPublicKeyInfo))

    signer = _len_prefixed(signed_data) + signatures + public_key
    signers = _len_prefixed(_len_prefixed(signer))    # sequence containing one signer
    return signers


def sign(apk_path, cert, key):
    """Add a v2 signature to an existing (already v1-signed) APK, in place."""
    data = bytearray(open(apk_path, "rb").read())
    eocd_off, cd_off, cd_size = _find_eocd(data)

    contents = bytes(data[:cd_off])                  # region 1
    central = bytes(data[cd_off:eocd_off])           # region 2
    eocd = bytearray(data[eocd_off:])                # region 3

    # The digest is computed with the EOCD's cd offset pointing at where the central
    # directory *will* be once the block is inserted -- but per spec it is treated as
    # if it pointed at the start of the signing block, i.e. the original cd_off.
    struct.pack_into("<I", eocd, 16, cd_off)

    digest = _chunked_digest([contents, central, bytes(eocd)])
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    v2_value = _build_v2_block(digest, cert_der, key)

    # id-value pair: uint64 length | uint32 id | value
    pair = _u64(len(v2_value) + 4) + _u32(APK_SIGNATURE_SCHEME_V2_ID) + v2_value

    # block = uint64 size | pairs | (padding) | uint64 size | magic
    body = pair
    total = len(body) + 8 + 16                       # + trailing size + magic
    pad = (4096 - ((len(contents) + 8 + total) % 4096)) % 4096
    if pad:
        # pad with a dummy id-value pair so the block stays well-formed
        if pad < 12:
            pad += 4096
        dummy = _u64(pad - 8) + _u32(0x42726577) + b"\x00" * (pad - 12)
        body += dummy
        total = len(body) + 8 + 16
    block = _u64(total) + body + _u64(total) + APK_SIG_BLOCK_MAGIC

    new_cd_off = len(contents) + len(block)
    struct.pack_into("<I", eocd, 16, new_cd_off)

    with open(apk_path, "wb") as f:
        f.write(contents)
        f.write(block)
        f.write(central)
        f.write(eocd)
    return len(block)


def has_v2(path):
    with open(path, "rb") as f:
        return f.read().find(APK_SIG_BLOCK_MAGIC) >= 0
