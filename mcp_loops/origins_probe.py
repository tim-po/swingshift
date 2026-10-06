"""Origins capability probing — the ONLY module that touches PATH / $HOME
to answer "which authed subscription CLIs can this origin actually run?".

The pure ``origins`` module deliberately knows nothing about CLIs; this module
adds an honest, machine-local probe used by the server's origin-capability
tool. Two design rules govern every line here:

  1. **Applied means honored at spawn.** ``applied`` says whether a loop
     agent that picks this CLI (+ model) actually gets it at spawn — true only
     when the CLI is on the spawn allowlist (``headless.SPAWN_RUNTIMES``), the
     passthrough gate is on (``headless.model_passthrough_enabled``: default
     on, ``LOOPS_MODEL_PASSTHROUGH=0`` turns it off) AND the binary is on this
     origin's PATH. A CLI that is not installed (``authed: False``) can never
     be applied — its spawn would fail. A present binary whose login we cannot
     confirm (``authed: "unknown"``, e.g. macOS keychain creds) still counts:
     spawn does launch it.

  2. **Never guess ``authed: True``.** The probe only reports ``True`` when it
     has *positive* evidence (binary on PATH AND a non-empty, readable auth
     file). Otherwise the answer is ``False`` (e.g. binary missing) or
     ``"unknown"`` (e.g. binary present but the credential store is elsewhere
     — on macOS the Claude Code CLI can persist creds to the login keychain,
     which this probe deliberately does not read).

For a REMOTE origin the Hub does not look at its own disk: it asks the origin
over the authenticated origin channel (``capabilities.probe`` — a read-only,
manifest-gated RPC the origin answers by running :func:`probe_local_clis` on
ITS box). :func:`probe_origin` takes that call as ``remote_probe`` and
normalises the answer (:func:`normalize_remote`) so a remote record can never
claim more than the local shape allows. With no live channel to the origin the
answer stays an honest ``{ok: False, reason}`` — never a guess.

Record shape (per CLI)::

    {
      "cli": "claude",
      "authed": True | False | "unknown",
      "applied": True | False,
      "note": "one-line honesty about what the probe saw",
      "lastProbed": 1789347501.4,
    }
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Callable, Optional


# CLIs we know how to reason about today. Each entry names the binary the CLI
# ships as, plus the *possible* auth-file locations we'll accept as positive
# evidence. Order matters — first non-empty file wins.
_KNOWN_CLIS: list[dict] = [
    {"cli": "cursor", "binary": "cursor-agent", "auth_paths": ("~/.config/cursor/auth.json",),
     "keychain_note": "run cursor-agent status to check subscription login"},
    {
        "cli": "claude",
        "binary": "claude",
        "auth_paths": (
            "~/.claude/.credentials.json",
            "~/.claude/auth.json",
            "~/.config/claude/auth.json",
        ),
        "keychain_note": (
            "on macOS claude credentials may live in the login keychain and "
            "are not probed by this tool"
        ),
    },
    {
        "cli": "codex",
        "binary": "codex",
        "auth_paths": (
            "~/.codex/auth.json",
            "~/.config/codex/auth.json",
        ),
        "keychain_note": None,
    },
]


def _binary_on_path(binary: str, *, path: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of ``binary`` on ``PATH`` (or the injected
    ``path`` for tests), or ``None`` if not present."""
    if path is None:
        return shutil.which(binary)
    return shutil.which(binary, path=path)


def _auth_file_signal(auth_paths: tuple[str, ...], *,
                      home: Optional[str] = None) -> tuple[Optional[str], str]:
    """Look up the first candidate auth file that exists. Returns
    ``(path_or_None, signal)`` where signal is one of:

      "positive"  — file exists and is readable AND non-empty
      "empty"     — file exists but is zero bytes
      "unreadable"— file exists but we can't read it (permissions, IO error)
      "absent"    — no candidate file found

    ``home`` (test seam) overrides ``$HOME``.
    """
    for raw in auth_paths:
        expanded = _expand(raw, home=home)
        if not os.path.exists(expanded):
            continue
        try:
            size = os.path.getsize(expanded)
        except OSError:
            return expanded, "unreadable"
        if size <= 0:
            return expanded, "empty"
        # Prove readable: an unreadable non-empty file is still "unknown".
        try:
            with open(expanded, "rb") as fh:
                fh.read(1)
        except OSError:
            return expanded, "unreadable"
        return expanded, "positive"
    return None, "absent"


def _expand(path: str, *, home: Optional[str] = None) -> str:
    if home is None:
        return os.path.expanduser(path)
    if path.startswith("~"):
        return os.path.join(home, path[2:] if path.startswith("~/") else path[1:])
    return path


def _spawn_applies(cli: str, passthrough: Optional[bool]) -> bool:
    """True iff a spawn honors a pick of ``cli`` (see rule 1 above)."""
    from mcp_loops import headless   # lazy: keep this module import-light
    on = headless.model_passthrough_enabled() if passthrough is None else passthrough
    return bool(on) and cli in headless.SPAWN_RUNTIMES


def _applied_note(applied: bool) -> str:
    return ("applied at spawn (runtime + --model passed)" if applied else
            "recorded, not applied at spawn (LOOPS_MODEL_PASSTHROUGH=0)")


_NOT_INSTALLED_NOTE = "not applied at spawn (CLI not installed on this origin)"


def probe_local_clis(now: float, *, path: Optional[str] = None,
                     home: Optional[str] = None,
                     passthrough: Optional[bool] = None) -> list[dict]:
    """Probe every known CLI on THIS box and return the honest capability
    list. ``path``, ``home`` and ``passthrough`` are test seams; they default
    to the process environment.

    Guarantees:

      * Never returns ``authed: True`` without positive evidence.
      * ``applied`` is true only when spawn honors the pick (rule 1).
      * Stamps ``lastProbed`` with ``now`` so the surface can show freshness.
    """
    out: list[dict] = []
    for spec in _KNOWN_CLIS:
        applied = _spawn_applies(spec["cli"], passthrough)
        tail = _applied_note(applied)
        from mcp_loops import runtimes
        effective, _ = runtimes.selection(spec["cli"])
        if effective != spec["cli"]:
            applied = False
            tail = f"host routes this runtime to {effective}; saved choice is not applied"
        binary_path = _binary_on_path(spec["binary"], path=path)
        if binary_path is None:
            # Rule 1: a missing binary is never applied, whatever the gate says.
            out.append({
                "cli": spec["cli"],
                "authed": False,
                "applied": False,
                "note": f"not on PATH — {_NOT_INSTALLED_NOTE}",
                "lastProbed": now,
            })
            continue
        auth_path, signal = _auth_file_signal(spec["auth_paths"], home=home)
        if signal == "positive":
            authed: bool | str = True
            note = (
                f"binary at {binary_path}; auth file {auth_path} — "
                f"{tail}"
            )
        elif signal == "empty":
            authed = "unknown"
            note = (
                f"binary at {binary_path}; auth file {auth_path} is empty — "
                f"{tail}"
            )
        elif signal == "unreadable":
            authed = "unknown"
            note = (
                f"binary at {binary_path}; auth file {auth_path} is not "
                f"readable — {tail}"
            )
        else:  # absent
            authed = "unknown"
            keychain = spec.get("keychain_note")
            keychain_txt = f"; {keychain}" if keychain else ""
            note = (
                f"binary at {binary_path}; no auth file in "
                f"{','.join(spec['auth_paths'])}{keychain_txt} — "
                f"{tail}"
            )
        out.append({
            "cli": spec["cli"],
            "authed": authed,
            "applied": applied,
            "note": note,
            "lastProbed": now,
        })
    return out


# Reason we return for a remote origin the Hub cannot reach over the origin
# channel (not enrolled / not connected / no origin fabric on this box).
REMOTE_NOT_WIRED_REASON = (
    "origin is not connected to this hub's origin channel — its CLIs can only "
    "be probed while it is online (yard connect)"
)

_AUTHED_VALUES = (True, False, "unknown")


def normalize_remote(payload: Any, now: float) -> dict:
    """Validate + normalise an origin's ``capabilities.probe`` answer into the
    :func:`probe_origin` result shape. Defensive by design — the payload came
    over the wire, so a record can never claim more than the local shape:

      * only dict records with a non-empty string ``cli`` survive;
      * ``authed`` must be exactly ``True``/``False``/``"unknown"`` — anything
        else (``"yes"``, ``1``) degrades to ``"unknown"``, never to ``True``;
      * ``applied`` is taken only when it is a real bool, else ``False`` —
        and is forced ``False`` when ``authed`` is ``False`` (rule 1: a CLI
        the origin does not have is never applied, even if an older origin
        says so);
      * every record is tagged ``remote: True`` so the surface can say where the
        probe ran.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "lastProbed": now,
                "reason": "origin answered capabilities.probe with a non-object"}
    if payload.get("ok") is False:
        return {"ok": False, "lastProbed": now,
                "reason": str(payload.get("reason")
                              or "origin reported its probe failed")}
    raw = payload.get("cliCapabilities")
    if not isinstance(raw, list):
        return {"ok": False, "lastProbed": now,
                "reason": "origin answered capabilities.probe without cliCapabilities"}
    probed = payload.get("lastProbed")
    probed = probed if isinstance(probed, (int, float)) and not isinstance(
        probed, bool) else now
    caps: list[dict] = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        cli = rec.get("cli")
        if not isinstance(cli, str) or not cli:
            continue
        authed = rec.get("authed")
        if not any(authed is v if isinstance(v, bool) else authed == v
                   for v in _AUTHED_VALUES):
            authed = "unknown"
        applied = rec.get("applied")
        note = rec.get("note")
        last = rec.get("lastProbed")
        caps.append({
            "cli": cli,
            "authed": authed,
            "applied": (applied if isinstance(applied, bool) else False)
            and authed is not False,
            "note": note if isinstance(note, str) else "",
            "lastProbed": last if isinstance(last, (int, float))
            and not isinstance(last, bool) else probed,
            "remote": True,
        })
    return {"ok": True, "cliCapabilities": caps, "lastProbed": probed,
            "via": "origin-channel"}


def probe_origin(kind: str, now: float, *, path: Optional[str] = None,
                 home: Optional[str] = None,
                 remote_probe: Optional[Callable[[], Any]] = None) -> dict:
    """Dispatch by origin ``kind``. For ``local`` we run :func:`probe_local_clis`
    and return ``{ok, cliCapabilities, lastProbed}``. For anything else we call
    ``remote_probe()`` — the Hub's ``capabilities.probe`` over the origin
    channel — and normalise its answer; with no ``remote_probe`` (origin not
    connected) or on a channel failure/refusal we return ``{ok: False, reason,
    lastProbed}`` — an HONEST signal the surface / launcher can show without
    lying."""
    if kind == "local":
        caps = probe_local_clis(now, path=path, home=home)
        return {"ok": True, "cliCapabilities": caps, "lastProbed": now}
    if remote_probe is None:
        return {"ok": False, "reason": REMOTE_NOT_WIRED_REASON, "lastProbed": now}
    try:
        payload = remote_probe()
    except Exception as exc:  # noqa: BLE001 — surfaced as the honest reason
        msg = str(exc) or type(exc).__name__
        return {"ok": False, "lastProbed": now,
                "reason": f"remote capability probe failed: {msg}"}
    return normalize_remote(payload, now)


def model_cli(model: Optional[str]) -> Optional[str]:
    """Map a model id to the CLI that would run it. ``claude-*`` → ``claude``,
    ``codex-*`` → ``codex``. Unknown prefixes return ``None`` — the caller
    treats that as "no gate we can honestly enforce" (better than inventing a
    denial we can't justify)."""
    if not isinstance(model, str) or not model:
        return None
    s = model.lower()
    if s.startswith("claude"):
        return "claude"
    if s.startswith("codex"):
        return "codex"
    return None


def can_run(capabilities_result: dict, model: Optional[str]) -> dict:
    """Honest predicate the surface AND the server share so a run can be
    accepted or refused by the SAME rule everywhere.

    Semantics — deliberately conservative on failure, permissive on ignorance:

      * ``model is None / ""``  →  always allowed. Inherit-mode carries no
        CLI claim, so there is nothing to gate.
      * ``model`` maps to a CLI we don't recognise  →  allowed. We do not
        invent a gate for a CLI we can't reason about.
      * capability probe was ``ok: False`` (remote not-wired, or probe error)
        →  allowed, but with ``reason`` explaining the honest gap. Refusing
        here would silently break every remote origin the moment a real
        cross-origin dispatch lands.
      * CLI record present with ``authed: True``  →  allowed.
      * CLI record present with ``authed: "unknown"``  →  allowed, but the
        caller gets an honest ``reason`` so it can warn the human. We do NOT
        promote "unknown" to "denied" — the login keychain case on macOS is
        the canonical example: we can't see the credential from disk, but
        the CLI can still run. Denying here would lock out real logins.
      * CLI record present with ``authed: False``  →  REFUSED with reason.
        The probe positively proved the CLI isn't authed on this origin.
      * CLI record absent from capabilities list  →  REFUSED with reason
        ("the origin didn't report a probe for this CLI").

    Returns ``{canRun: bool, reason: str, cli: str | None}``. ``reason`` is
    ALWAYS non-empty on refusal and MAY be non-empty on allow (e.g. an
    "unknown" caveat) — the caller decides whether to surface the caveat.
    """
    cli = model_cli(model)
    if cli is None:
        return {"canRun": True, "reason": "", "cli": None}
    if not isinstance(capabilities_result, dict):
        return {"canRun": True,
                "reason": "no capability record — model gate skipped",
                "cli": cli}
    if capabilities_result.get("ok", True) is False:
        return {"canRun": True,
                "reason": capabilities_result.get("reason") or REMOTE_NOT_WIRED_REASON,
                "cli": cli}
    caps = capabilities_result.get("cliCapabilities") or []
    entry = next((c for c in caps if isinstance(c, dict) and c.get("cli") == cli), None)
    if entry is None:
        return {"canRun": False,
                "reason": f"origin did not report a probe for {cli!r}",
                "cli": cli}
    authed = entry.get("authed")
    if authed is True:
        return {"canRun": True, "reason": "", "cli": cli}
    if authed == "unknown":
        return {"canRun": True,
                "reason": f"authed for {cli!r} is unknown on this origin "
                          f"(see capability note) — allowing, launcher may warn",
                "cli": cli}
    # authed is False (or any other falsy) → refuse.
    return {"canRun": False,
            "reason": f"origin is not authed for {cli!r}: "
                      f"{entry.get('note') or 'no note'}",
            "cli": cli}


# ── claude login state (LAUNCH-CHECKLIST 3.3) ────────────────────────────────
# The auth-file probe above can only say "unknown" for a present binary with
# no creds file (macOS keychain, API-key env, …). Before a loop's first turn
# the engine asks the CLI itself: ``claude auth status`` reads every store the
# CLI uses and prints ``{"loggedIn": bool, …}``. Same honesty rule: only an
# explicit ``"loggedIn": false`` is ``False``; a CLI without the subcommand, a
# timeout or unparsable output stays ``"unknown"`` and never blocks a start.
CLAUDE_NOT_LOGGED_IN = (
    "claude is installed but not logged in — run `claude` once in a terminal "
    "and log in (or `claude auth login`), then start the loop again")

_LOGIN_OK_TTL_S = 600.0
_LOGIN_OK_CACHE: dict[str, float] = {}


def probe_claude_login(binary: str, *, timeout: float = 20.0,
                       run: Optional[Callable[..., Any]] = None) -> bool | str:
    """``True`` / ``False`` / ``"unknown"`` from ``<binary> auth status
    --json`` (stdin closed, bounded by ``timeout`` — never a hang)."""
    import json
    import subprocess
    run = run or subprocess.run
    try:
        out = run([binary, "auth", "status", "--json"], stdin=subprocess.DEVNULL,
                  capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    try:
        logged_in = json.loads(out.stdout or "").get("loggedIn")
    except (ValueError, AttributeError):
        return "unknown"
    return logged_in if isinstance(logged_in, bool) else "unknown"


def claude_login_state(binary: str, *, now: Optional[float] = None) -> bool | str:
    """:func:`probe_claude_login`, with a positive answer cached per binary for
    ``_LOGIN_OK_TTL_S`` so back-to-back starts don't each spawn the CLI. A
    ``False`` is never cached: the next start after ``claude`` login proceeds."""
    import time
    now = time.time() if now is None else now
    if now - _LOGIN_OK_CACHE.get(binary, float("-inf")) < _LOGIN_OK_TTL_S:
        return True
    state = probe_claude_login(binary)
    if state is True:
        _LOGIN_OK_CACHE[binary] = now
    return state
