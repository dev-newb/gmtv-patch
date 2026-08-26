"""Find Android devices on the local network.

Two mechanisms, because they find different things:

  * mDNS  -- Android 11+ "wireless debugging" advertises _adb-tls-connect._tcp.
             Zero-config and instant, but only for devices with it switched on,
             and each pairing is per-host.
  * port  -- Classic `adb tcpip 5555` / always-on adb (NVIDIA Shield ships this
             way). Never advertised, so it has to be probed for.

The port sweep is a plain TCP connect across the /24, run in parallel. It is not
a security scan: it touches one port with a normal connect, exactly what
`adb connect` would do.
"""

import concurrent.futures
import ipaddress
import socket
import subprocess
import shutil

ADB_PORT = 5555


def _local_subnets():
    """Best-effort list of IPv4 /24s this machine is on."""
    nets = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                ip = line.split()[1]
                try:
                    nets.append(ipaddress.ip_network(ip + "/24", strict=False))
                except ValueError:
                    pass
    except Exception:
        pass
    return nets


def _probe(ip, port=ADB_PORT, timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((str(ip), port))
        return str(ip)
    except Exception:
        return None
    finally:
        s.close()


def scan_ports(subnets=None, port=ADB_PORT, workers=256, timeout=1.5, progress=None):
    """Return [ip] with `port` open.

    The timeout matters more than it looks: on Wi-Fi a live device can take ~0.8s
    to complete a TCP handshake, so an aggressive timeout silently misses exactly
    the devices you are looking for. 1.5s with 256 workers sweeps a /24 in a few
    seconds and does not drop slow responders.
    """
    subnets = subnets or _local_subnets()
    hosts = [h for n in subnets for h in n.hosts()]
    found, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_probe, h, port, timeout): h for h in hosts}
        for f in concurrent.futures.as_completed(futs):
            done += 1
            if progress and done % 32 == 0:
                progress(done, len(hosts))
            r = f.result()
            if r:
                found.append(r)
    return sorted(found, key=lambda x: tuple(int(p) for p in x.split(".")))


def scan_mdns():
    """Devices advertising adb over mDNS (Android 11+ wireless debugging)."""
    adb = shutil.which("adb")
    if not adb:
        return []
    out = []
    try:
        r = subprocess.run([adb, "mdns", "services"], capture_output=True,
                           text=True, timeout=12).stdout
        for line in r.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "_adb" in parts[1]:
                out.append(parts[0])
    except Exception:
        pass
    return out


def identify(target):
    """Connect and read a few props. Returns dict or None."""
    adb = shutil.which("adb")
    if not adb:
        return None
    subprocess.run([adb, "connect", target], capture_output=True, text=True, timeout=15)
    def prop(k):
        return subprocess.run([adb, "-s", target, "shell", "getprop", k],
                              capture_output=True, text=True, timeout=12).stdout.strip()
    model = prop("ro.product.model")
    if not model:
        return None
    return {"target": target, "model": model,
            "release": prop("ro.build.version.release"),
            "sdk": prop("ro.build.version.sdk"),
            "abilist": prop("ro.product.cpu.abilist"),
            "leanback": "yes" if prop("ro.build.characteristics").find("tv") >= 0 else "?"}


def discover(progress=None):
    """Everything we can find, identified. Ports first (fast), then mDNS names."""
    seen, results = set(), []
    for ip in scan_ports(progress=progress):
        t = f"{ip}:{ADB_PORT}"
        if t not in seen:
            seen.add(t)
            info = identify(t)
            if info:
                results.append(info)
    for name in scan_mdns():
        if name not in seen:
            seen.add(name)
            info = identify(name)
            if info:
                results.append(info)
    return results
