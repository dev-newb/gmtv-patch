"""
Pure-Python APK v1 (JAR) signing — no JDK required.

GameMaker 1.4 packages target old SDKs, so Android accepts a v1-only signature
(proven this session: a v1-signed APK installs and runs on Android 14). This module
produces that signature with `cryptography` alone, replacing the jarsigner dependency.

The scheme (Android "APK Signature Scheme v1", a.k.a. JAR signing):
  META-INF/MANIFEST.MF  — per-entry SHA-256 of the *uncompressed* content
  META-INF/CERT.SF      — SHA-256 of the whole manifest, plus of each manifest section
  META-INF/CERT.RSA     — detached PKCS#7 signature over CERT.SF

Manifest text rules that bite if you get them wrong, all handled below:
  * CRLF line endings, blank line terminates every section.
  * No line may exceed 72 bytes UTF-8; longer lines wrap with a leading space.
  * A section's SF digest is over that section's EXACT bytes including its blank line.
"""

import base64
import datetime
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

CREATED_BY = "gmtv-patch (pure-python)"


def load_or_create_key(pem_path):
    """Return (cert, private_key), creating a self-signed pair on first use.

    Cached as one PEM file (key + cert) beside the tool — the analogue of the old
    keystore. Reusing it lets a re-patched APK install as an in-place upgrade.
    """
    import os
    if os.path.exists(pem_path):
        data = open(pem_path, "rb").read()
        key = serialization.load_pem_private_key(data, password=None)
        cert = x509.load_pem_x509_certificate(data)
        return cert, key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "gmtv-patch"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "gmtv-patch"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    ])
    # Fixed validity window; Date.now() is fine here (not a workflow script).
    not_before = datetime.datetime(2020, 1, 1)
    not_after = datetime.datetime(2099, 1, 1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before).not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    pem = (
        key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    with open(pem_path, "wb") as f:
        f.write(pem)
    try:
        os.chmod(pem_path, 0o600)
    except OSError:
        pass
    return cert, key


def _wrap(line):
    """Wrap one header line to <=72 bytes UTF-8, continuation lines lead with space."""
    raw = line.encode("utf-8")
    if len(raw) <= 72:
        return line
    out, first, rest = [], raw[:72], raw[72:]
    out.append(first)
    while rest:
        chunk, rest = rest[:71], rest[71:]  # 71 + leading space = 72
        out.append(b" " + chunk)
    return b"\r\n".join(out).decode("utf-8")


def _section(pairs):
    """Build one manifest/SF section (list of (key,value)) ending in a blank line."""
    lines = [_wrap(f"{k}: {v}") for k, v in pairs]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def build_signature_files(digest_pairs):
    """digest_pairs: ordered list of (name, base64_sha256_of_uncompressed_content).

    Takes digests rather than bytes so the caller can hash a 300MB archive
    incrementally instead of holding every uncompressed entry in memory.

    Returns (manifest_bytes, sections) where sections[name] = that entry's exact
    manifest section bytes (needed verbatim for the per-entry SF digests).
    """
    main = _section([("Manifest-Version", "1.0"), ("Created-By", CREATED_BY)])
    manifest = bytearray(main)
    sections = {}
    for name, digest in digest_pairs:
        sec = _section([("Name", name), ("SHA-256-Digest", digest)])
        sections[name] = bytes(sec)
        manifest += sec
    return bytes(manifest), sections


def sha256_b64(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def sha256_b64_stream(fp, chunk=1 << 20):
    """Digest a file-like object without loading it all at once."""
    h = hashlib.sha256()
    while True:
        b = fp.read(chunk)
        if not b:
            break
        h.update(b)
    return base64.b64encode(h.digest()).decode("ascii")


def build_sf(manifest_bytes, sections):
    man_digest = base64.b64encode(hashlib.sha256(manifest_bytes).digest()).decode("ascii")
    main = _section([
        ("Signature-Version", "1.0"),
        ("SHA-256-Digest-Manifest", man_digest),
        ("Created-By", CREATED_BY),
    ])
    sf = bytearray(main)
    for name, sec_bytes in sections.items():
        d = base64.b64encode(hashlib.sha256(sec_bytes).digest()).decode("ascii")
        sf += _section([("Name", name), ("SHA-256-Digest", d)])
    return bytes(sf)


def build_pkcs7(sf_bytes, cert, key):
    """Detached PKCS#7 over the SF file — no signed attributes, DER, binary."""
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(sf_bytes)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [
            pkcs7.PKCS7Options.DetachedSignature,
            pkcs7.PKCS7Options.NoAttributes,
            pkcs7.PKCS7Options.Binary,
        ])
    )


def sign_digests(digest_pairs, cert, key):
    """High level: [(name, b64sha256)] -> {META-INF path: bytes} for the 3 sig files."""
    manifest, sections = build_signature_files(digest_pairs)
    sf = build_sf(manifest, sections)
    rsa_blob = build_pkcs7(sf, cert, key)
    return {
        "META-INF/MANIFEST.MF": manifest,
        "META-INF/CERT.SF": sf,
        "META-INF/CERT.RSA": rsa_blob,
    }


def sign_entries(entries, cert, key):
    """Convenience for small inputs/tests: entries = [(name, uncompressed_bytes)]."""
    return sign_digests([(n, sha256_b64(d)) for n, d in entries], cert, key)
