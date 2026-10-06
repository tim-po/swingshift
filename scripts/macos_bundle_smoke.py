#!/usr/bin/env python3
"""Static smoke of a cross-assembled macOS origin bundle — on Linux (P2.5 (3)).

A Mac bundle cannot execute here, so this proves everything that CAN be proven
without a Mac, and says so:

  1. the tarball matches its .sha256 and is a Loopyard bundle for the target;
  2. the runtime python3 and EVERY native extension (.so/.dylib) is Mach-O for the
     target arch (or a fat binary containing it) — zero ELF leaked from the host;
  3. ``bin/yard`` is the portable shim and ``mcp_loops/yard.py`` inside is
     byte-identical to the repo at the bundle's gitSha, carrying the P2.5 verbs
     (headless ``origin up``, ``origin status|down``, ``hub pair-code``) — the SAME
     CLI path the Linux acceptance runs;
  4. ``install.sh`` on a (faked) ``uname`` Darwin/<arch> picks exactly this
     artifact from a release dir, verifies it and installs ``~/loopyard/bin/yard``.

NOT proven here (the owner's Mac run, see bundle/MACOS.md): that the Mach-O
binaries load under dyld / Gatekeeper, and a live ``yard origin up`` from a Mac.

    python3 scripts/macos_bundle_smoke.py /tmp/out/loopyard-origin-macos-arm64.tar.gz
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# mach-o magic (both byte orders) + fat; cputype for the two targets
MH_MAGIC_64 = (0xFEEDFACF, 0xCFFAEDFE)
FAT_MAGIC = (0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA)
CPU = {"arm64": 0x0100000C, "x86_64": 0x01000007}
P25_MARKERS = ("def _origin_up_headless", "def _cmd_origin_status", "def _cmd_origin_down",
               "def cmd_hub", "_PAIR_CODE_ON_ARGV")


def macho_arches(head: bytes) -> list[str] | None:
    """Arches of a Mach-O / fat binary from its first bytes; None if not Mach-O."""
    if len(head) < 8:
        return None
    (magic_be,) = struct.unpack(">I", head[:4])
    if magic_be in MH_MAGIC_64:
        fmt = "<I" if magic_be == 0xCFFAEDFE else ">I"
        (cpu,) = struct.unpack(fmt, head[4:8])
        return [k for k, v in CPU.items() if v == cpu] or [hex(cpu)]
    if magic_be in FAT_MAGIC:
        (n,) = struct.unpack(">I", head[4:8])
        out = []
        for i in range(min(n, 8)):
            off = 8 + i * 20
            if len(head) < off + 4:
                break
            (cpu,) = struct.unpack(">I", head[off:off + 4])
            out += [k for k, v in CPU.items() if v == cpu] or [hex(cpu)]
        return out
    return None


class Smoke:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, ok: bool, what: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ok   {what}")
        else:
            self.failed += 1
            print(f"  FAIL {what}{(' — ' + detail) if detail else ''}")


def fake_uname_dir(tmp: str, s: str, m: str) -> str:
    d = os.path.join(tmp, "fakebin")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "uname")
    with open(p, "w") as fh:
        fh.write(f'#!/bin/sh\ncase "$1" in -s) echo "{s}";; -m) echo "{m}";; '
                 f'*) exec /usr/bin/env -u PATH /bin/uname "$@";; esac\n')
    os.chmod(p, 0o755)
    return d


def run_install(tmp: str, uname_s: str, uname_m: str, release_dir: str, dest: str):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPYARD_")}
    env["PATH"] = fake_uname_dir(tmp, uname_s, uname_m) + os.pathsep + env.get("PATH", "")
    env["HOME"] = tmp
    # install.sh is https-only; loopback http needs LOOPYARD_ALLOW_INSECURE=1.
    # A bundle that carries a .sig is still only checksum-verified here unless
    # install.sh has a pinned key (then the signature is enforced regardless).
    env["LOOPYARD_ALLOW_INSECURE"] = env["LOOPYARD_ALLOW_UNSIGNED"] = "1"
    import functools
    import http.server
    import threading

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=release_dir))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        env["LOOPYARD_RELEASE_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        return subprocess.run(["sh", os.path.join(REPO, "install.sh"), "--dir", dest],
                              env=env, capture_output=True, text=True, timeout=300)
    finally:
        httpd.shutdown()
        httpd.server_close()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    tarball = os.path.abspath(argv[0])
    sm = Smoke()
    print(f"== macOS bundle static smoke: {os.path.basename(tarball)}")

    # 1. checksum + identity
    with open(tarball, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    want = open(tarball + ".sha256").read().split()[0]
    sm.check(digest == want, f"sha256 matches .sha256 ({digest[:16]}…)")
    tf = tarfile.open(tarball, "r:gz")
    names = tf.getnames()
    meta = json.load(tf.extractfile("loopyard/BUNDLE.json"))
    target = meta.get("target", "")
    arch = target.rsplit("-", 1)[-1]
    sm.check(target in ("macos-arm64", "macos-x86_64"), f"BUNDLE.json target={target} version={meta.get('version')}")
    sm.check(os.path.basename(tarball) == f"loopyard-origin-{target}.tar.gz",
             "artifact name is the one install.sh fetches for this target")

    # 2. native code is Mach-O for the target arch — no host ELF
    natives, bad, elf = 0, [], []
    for m in tf.getmembers():
        if not m.isfile():
            continue
        is_py = m.name.endswith(("/bin/python3", "/bin/python3.13"))
        if not (is_py or m.name.endswith((".so", ".dylib"))):
            continue
        head = tf.extractfile(m).read(8 + 20 * 8)
        if head[:4] == b"\x7fELF":
            elf.append(m.name)
            continue
        arches = macho_arches(head)
        natives += 1
        if not arches or arch not in arches:
            bad.append(f"{m.name}:{arches}")
    py = [n for n in names if n.endswith(("runtime/bin/python3", "runtime/bin/python3.13"))]
    sm.check(bool(py), f"runtime python present ({', '.join(py)})")
    sm.check(natives > 0 and not bad and not elf,
             f"{natives} native binaries are Mach-O {arch}, 0 ELF",
             f"bad={bad[:5]} elf={elf[:5]}")

    # 3. the SAME CLI: shim + yard.py byte-identical to git at gitSha, P2.5 verbs present
    sys.path.insert(0, REPO)
    from mcp_loops import origin_bundle
    shim = tf.getmember("loopyard/bin/yard")
    sm.check(tf.extractfile(shim).read().decode() == origin_bundle.BIN_YARD and shim.mode & 0o111,
             "bin/yard is the portable -I shim, executable")
    sha = meta.get("gitSha", "")
    for rel in ("mcp_loops/yard.py", "mcp_loops/origin_client.py", "mcp_loops/origin_onboard.py"):
        inside = tf.extractfile(f"loopyard/{rel}").read()
        git = subprocess.run(["git", "-C", REPO, "show", f"{sha}:{rel}"], capture_output=True)
        sm.check(git.returncode == 0 and git.stdout == inside, f"{rel} == git {sha[:12]}:{rel}")
    yard_src = tf.extractfile("loopyard/mcp_loops/yard.py").read().decode()
    missing = [mk for mk in P25_MARKERS if mk not in yard_src]
    sm.check(not missing, "yard.py carries headless origin up / status / down / hub pair-code",
             f"missing {missing}")

    # 4. install.sh on Darwin/<arch> fetches + installs exactly this artifact
    tmp = tempfile.mkdtemp(prefix="ob-mac-smoke-")
    try:
        rel = os.path.join(tmp, "release", "latest")
        os.makedirs(rel)
        for suf in ("", ".sha256", ".sig"):
            if suf == ".sig" and not os.path.exists(tarball + suf):
                continue
            shutil.copy(tarball + suf, os.path.join(rel, os.path.basename(tarball) + suf))
        uname_m = "arm64" if arch == "arm64" else "x86_64"
        dest = os.path.join(tmp, "loopyard")
        r = run_install(tmp, "Darwin", uname_m, os.path.join(tmp, "release"), dest)
        out = r.stdout + r.stderr
        sm.check(r.returncode == 0 and f"installed Loopyard {meta.get('version')}" in out,
                 f"install.sh (uname Darwin/{uname_m}) fetched + verified + installed", out[-400:])
        sm.check(os.access(os.path.join(dest, "bin", "yard"), os.X_OK)
                 and json.load(open(os.path.join(dest, "BUNDLE.json")))["target"] == target,
                 "installed tree: <dir>/bin/yard + BUNDLE.json for the target")
        # the other mac arch must NOT silently take this artifact
        other = "x86_64" if uname_m == "arm64" else "arm64"
        r2 = run_install(tmp, "Darwin", other, os.path.join(tmp, "release"), os.path.join(tmp, "l2"))
        sm.check(r2.returncode == 3 and "download failed" in r2.stderr,
                 f"uname Darwin/{other} asks for its own artifact (rc 3 here), never this one")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"== {sm.passed} passed, {sm.failed} failed")
    print("NOT PROVEN HERE (owner, on a Mac): dyld/Gatekeeper load of the Mach-O runtime, "
          "a live `yard origin up` — see bundle/MACOS.md")
    if sm.failed == 0:
        print("MAC_BUNDLE_SMOKE_OK")
    return 0 if sm.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
