"""Auto-resolve the xray-vpn egress-proxy IP in config/org.toml (task 28d5a0e7).

The Telegram egress proxy points at the xray-vpn docker container
(``[network].tg_egress_proxy`` in org.toml). Docker reassigns that container a
fresh 172.17.0.x address on every host reboot, so the hardcoded IP goes stale
and TG egress breaks until someone edits org.toml by hand.

This module resolves the container's CURRENT ip via ``docker inspect`` and
rewrites just the host portion of the proxy line — preserving the scheme, port,
and surrounding comments — only when it actually changed. It's idempotent and
boot-safe: if docker or the container isn't up yet it logs a warning and exits 0
(a stale IP is no worse than before; never fail the boot). Wire it as a
oneshot that runs before the bot-swarm services (see
systemd/bot-swarm-xray-resolve.service).

Run:  python -m bot_squad_worker.xray_resolve [--container xray-vpn] [--org PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONTAINER = "xray-vpn"
DEFAULT_ORG = Path(__file__).resolve().parents[2] / "config" / "org.toml"

# Matches:  tg_egress_proxy = "http://172.17.0.2:1080"   # trailing comment ok
# Groups:   1=prefix through scheme://   2=host   3=:port(optional)   4=tail
_PROXY_RE = re.compile(
    r'(?m)^(\s*tg_egress_proxy\s*=\s*"[a-zA-Z0-9+.\-]+://)'  # 1: key + scheme://
    r'([^:/"\s]+)'                                            # 2: host (ip/name)
    r'(:\d+)?'                                                # 3: optional :port
    r'("[^\n]*)$'                                             # 4: closing quote + rest
)


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path``'s CONTENT in one indivisible step — never truncate in place.

    THIS FILE IS READ FRESH BY EVERYTHING, AND IT IS REWRITTEN AT BOOT. That pair is
    the whole hazard. ``Path.write_text`` opens "w", which TRUNCATES first: between the
    truncate and the write landing there is a real window in which org.toml on disk is
    EMPTY or HALF A FILE, and every concurrent reader in the fleet samples it —
    mcp_telegram.helpers.get_proxy() on every Bot API request in every runner,
    ops/fleet_watchdog._alert_config() on every tick, ba's owner_chat() on every
    Telegram update, coord-computation's spend-guard alert path per call. What they get
    back is not a stale value (which everyone has defended against) but a TOMLDecodeError
    or a missing key — a config fault, arriving in the paging path, at boot, when the
    fleet is at its least settled. Nobody's fresh read is wrong; the file was.

    The fleet spent 2026-07-24 converting HELD config reads to FRESH ones — correctly,
    the held value rots because docker reassigns this container's IP on reboot. But every
    one of those conversions multiplies how often somebody samples this window, and all
    of the day's work was on the reader side. The window is one line on the writer side,
    and closing it here closes it for all of them at once — which is better than each
    reader growing its own retry, because a reader-side retry has to KNOW that a parse
    error means "torn, try again" rather than "misconfigured, page a human", and those
    two want opposite handling.

    tmp-in-the-same-directory + ``os.replace`` is atomic on POSIX (same filesystem, so
    no cross-device fallback): a reader sees either every old byte or every new one, and
    a reader holding an open fd keeps reading the old inode to its end. mtime caches are
    unaffected — a replaced file is a new mtime, which is exactly the "picked up on the
    next call" the readers already rely on.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)          # atomic; also removes tmp
    except BaseException:
        # A CRASH MUST NOT LEAVE THE HAZARD BEHIND IN A NEW COSTUME: a stranded
        # .org.toml.tmp is a half-written config sitting next to the real one, and the
        # next run's tmp write would be the only thing that ever cleaned it up.
        tmp.unlink(missing_ok=True)
        raise


def rewrite_proxy_host(text: str, new_host: str) -> tuple[str, str | None]:
    """Pure: return (new_text, old_host). old_host is None if the line is absent
    or the host already equals new_host (i.e. nothing to change)."""
    m = _PROXY_RE.search(text)
    if not m:
        return text, None
    old_host = m.group(2)
    if old_host == new_host:
        return text, None
    new_line = m.group(1) + new_host + (m.group(3) or "") + m.group(4)
    return text[:m.start()] + new_line + text[m.end():], old_host


def resolve_container_ip(container: str, retries: int = 1,
                         delay: float = 2.0) -> str | None:
    """Current bridge IP of `container` via docker inspect, or None if docker or
    the container isn't available. Retries to tolerate a not-yet-up container at
    boot."""
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    for attempt in range(max(1, retries)):
        try:
            out = subprocess.run(
                ["docker", "inspect", "-f", fmt, container],
                capture_output=True, text=True, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        ip = out.stdout.strip()
        if out.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return ip
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve xray-vpn proxy IP into org.toml")
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--org", type=Path, default=DEFAULT_ORG)
    ap.add_argument("--retries", type=int, default=15,
                    help="docker-inspect attempts (boot: container may lag)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"xray_resolve: {msg}", flush=True)

    if not args.org.exists():
        log(f"org.toml not found at {args.org} — nothing to do")
        return 0

    ip = resolve_container_ip(args.container, retries=args.retries)
    if ip is None:
        log(f"could not resolve container {args.container!r} (docker down or "
            f"container not up) — leaving org.toml unchanged")
        return 0

    text = args.org.read_text()
    new_text, old_host = rewrite_proxy_host(text, ip)
    if old_host is None:
        log(f"proxy host already {ip} (or line absent) — no change")
        return 0

    log(f"{'[dry-run] would update' if args.dry_run else 'updating'} "
        f"tg_egress_proxy host {old_host} -> {ip}")
    if not args.dry_run:
        write_atomic(args.org, new_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
