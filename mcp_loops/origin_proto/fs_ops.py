"""fs_ops — ``fs.pull`` / ``fs.push`` as COMPOSITIONS over ``origin.run`` (§2.1).

These are NOT new wire methods and add NO new capability surface. Per the hardened
spec (§2.1): "fs.pull / fs.push are compositions, not new methods. A pull is
``origin.run{argv:["cat", path]}`` streamed back; a push is
``origin.run{argv:["tee", path], stdin:<b64>}``." Richer file semantics
(resumable, chunked, checksum-first) stay a *client-side convention over
origin.run* to be promoted to a dedicated method only if measurement demands it —
emergent, not built up front.

Everything here rides the ONE exec primitive through :meth:`DispatchRouter.run`,
so it inherits every P1 property for free: keypair-authed channel, the §5.1(4)
bidirectional-mTLS hard gate for non-loopback origins, the origin's fail-closed
``RunAllowlist`` (the origin must opt ``cat``/``tee`` in — an un-permitted verb is
refused at the origin, surfaced here as a typed error), dispatch-id idempotency,
and real-argv audit. Execution stays on the origin's own box; only bytes cross.

Integrity: a push uses ``tee``, which writes stdin to the file AND echoes it to
stdout. We compare that echo against the bytes we sent, so a truncated or
corrupted transfer is CAUGHT here rather than silently trusted — a free integrity
check with no extra binary on the origin's allowlist. A pull returns the file
bytes plus their sha256 so the caller can persist + verify.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, Optional, Union

from mcp_loops import audit as _daudit

# A conservative default ceiling so a buffered pull/push can't OOM the Hub with a
# giant file over the one-shot channel. The compositions are for config/artifact
# files, not disk images; a bigger transfer is the "promote to a chunked method"
# signal the spec calls out, not something to silently buffer.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB
_STDERR_SLACK = 64 * 1024


class FsError(RuntimeError):
    """A pull/push that reached the origin but failed there (non-zero exit,
    missing path, permission denied) — carries the origin's stderr."""

    def __init__(self, message: str, *, exit_code: Optional[int] = None,
                 stderr: str = "", path: str = "", verb: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.path = path
        self.verb = verb


class FsIntegrityError(FsError):
    """A push whose origin-echoed bytes did not match what we sent — the transfer
    landed corrupt/truncated. Fail LOUD; never report a bad push as ok."""


def _b64d(s: Optional[str]) -> bytes:
    return base64.b64decode(s) if s else b""


def _check_path(remote_path: str, verb: str) -> None:
    """Refuse a path cat/tee would parse as an OPTION (P4b). Paired with the
    ``--`` terminator in the argv this is belt-and-braces: e.g. ``tee -a`` writes
    no file yet echoes stdin, which the integrity check would call ``verified``.
    A file whose name starts with ``-`` is still reachable as ``./-x`` or by an
    absolute path."""
    if not isinstance(remote_path, str) or not remote_path:
        raise FsError(f"{verb}: remote_path must be a non-empty string",
                      path=str(remote_path), verb=verb)
    if remote_path.startswith("-"):
        raise FsError(f"{verb}: refusing {remote_path!r} — a leading '-' is an "
                      f"option, not a path (use ./{remote_path} or an absolute path)",
                      path=remote_path, verb=verb)


def _stderr_text(res: dict) -> str:
    try:
        return _b64d(res.get("stderr")).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 — stderr decode must never mask the real error
        return ""


async def fs_pull(router: Any, device_id: str, remote_path: str, *,
                  cap_token: Optional[str] = None,
                  dispatch_id: Optional[str] = None,
                  timeout: Optional[float] = None,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Pull ``remote_path`` FROM the origin ``device_id`` back to the caller (B→A).

    Composition: ``origin.run{argv:["cat", "--", remote_path]}``; the origin streams the
    file's bytes back as stdout, which we base64-decode. Returns
    ``{ok, verb, path, bytes, data, sha256, dispatchId}``. Raises :class:`FsError`
    if the origin couldn't read the path (surfacing its stderr + exit code) or if
    the file exceeds ``max_bytes`` (the "promote to chunked" signal, not a silent
    truncation).
    """
    _check_path(remote_path, "fs.pull")
    with _daudit.dispatch_context(verb=_daudit.V_FS_READ):
        res = await router.run(device_id, ["cat", "--", remote_path],
                               verb=_daudit.V_FS_READ,
                               cap_token=cap_token, dispatch_id=dispatch_id,
                               timeout=timeout, max_bytes=max_bytes)
    exit_code = res.get("exitCode")
    if res.get("outputLimitExceeded"):
        # P4c: the ORIGIN stopped capturing at the ceiling and killed ``cat`` —
        # the file never got buffered whole on either side.
        raise FsError(
            f"fs.pull {remote_path!r} is over the {max_bytes}-byte ceiling — "
            f"promote to a chunked transfer",
            exit_code=exit_code, path=remote_path, verb="fs.pull")
    if exit_code != 0:
        raise FsError(
            f"fs.pull {remote_path!r} failed on origin (exit {exit_code})",
            exit_code=exit_code, stderr=_stderr_text(res),
            path=remote_path, verb="fs.pull")
    data = _b64d(res.get("stdout"))
    if len(data) > max_bytes:
        raise FsError(
            f"fs.pull {remote_path!r} is {len(data)} bytes, over the "
            f"{max_bytes}-byte ceiling — promote to a chunked transfer",
            path=remote_path, verb="fs.pull")
    return {
        "ok": True,
        "verb": "fs.pull",
        "path": remote_path,
        "bytes": len(data),
        "data": data,
        "sha256": hashlib.sha256(data).hexdigest(),
        "dispatchId": res.get("dispatchId"),
    }


async def fs_push(router: Any, device_id: str, remote_path: str,
                  data: Union[bytes, str], *,
                  cap_token: Optional[str] = None,
                  dispatch_id: Optional[str] = None,
                  timeout: Optional[float] = None,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Push ``data`` TO ``remote_path`` on the origin ``device_id`` (A→B).

    Composition: ``origin.run{argv:["tee", "--", remote_path], stdin:<b64(data)>}``.
    ``tee`` writes stdin to the file AND echoes it to stdout, so we compare that
    echo against what we sent and raise :class:`FsIntegrityError` on any mismatch —
    a truncated/corrupt push is caught, never reported ok. Returns
    ``{ok, verb, path, bytes, sha256, verified, dispatchId}``.
    """
    _check_path(remote_path, "fs.push")
    payload = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    payload = bytes(payload)
    if len(payload) > max_bytes:
        raise FsError(
            f"fs.push to {remote_path!r} is {len(payload)} bytes, over the "
            f"{max_bytes}-byte ceiling — promote to a chunked transfer",
            path=remote_path, verb="fs.push")
    with _daudit.dispatch_context(verb=_daudit.V_FS_WRITE):
        res = await router.run(device_id, ["tee", "--", remote_path],
                               verb=_daudit.V_FS_WRITE,
                               stdin=base64.b64encode(payload).decode("ascii"),
                               cap_token=cap_token, dispatch_id=dispatch_id,
                               timeout=timeout,
                               # tee echoes exactly the payload; the slack keeps
                               # its stderr on a failure (P4c capture cap).
                               max_bytes=len(payload) + _STDERR_SLACK)
    exit_code = res.get("exitCode")
    if exit_code != 0:
        raise FsError(
            f"fs.push to {remote_path!r} failed on origin (exit {exit_code})",
            exit_code=exit_code, stderr=_stderr_text(res),
            path=remote_path, verb="fs.push")
    echoed = _b64d(res.get("stdout"))
    if echoed != payload:
        raise FsIntegrityError(
            f"fs.push to {remote_path!r} corrupt: origin echoed {len(echoed)} of "
            f"{len(payload)} bytes",
            exit_code=exit_code, path=remote_path, verb="fs.push")
    return {
        "ok": True,
        "verb": "fs.push",
        "path": remote_path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "verified": True,
        "dispatchId": res.get("dispatchId"),
    }
