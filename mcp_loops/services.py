"""services.py — track the long-lived processes a Loopyard box runs (Round-3 A6).

Round-3 A6 (pilot finding): after setup the server + worker + venv "vanished ~20
min later" and a returning user had nothing to talk to, and `yard status` showed
no pid/port/health. The processes were being started *inside a session* and dying
with it, with nothing recording them.

This module is the persistence backbone: the detached processes `yard up` starts
(and the server that `loopyard-quickstart.sh` starts) are recorded in a single
``services.json`` under ``<data>/../_run/`` so a RETURNING user can run
``yard status`` and see what is alive, and ``yard up`` is idempotent (it
re-attaches to a live pid instead of double-spawning). No secrets — just pids,
ports, command lines, and log paths, keyed by a stable service name.

State vocabulary (what ``describe`` reports per service):

    running   — the recorded pid is alive
    stopped   — pid is dead AND it was stopped on purpose (``yard down`` / mark_stopped)
    crashed   — pid is dead but we never stopped it (the "it vanished" case)
"""
from __future__ import annotations

import json
import os
import signal
from typing import Optional

from mcp_loops import paths

REGISTRY = "services.json"

RUNNING = "running"
STOPPED = "stopped"
CRASHED = "crashed"


def run_dir(data_dir: Optional[str] = None) -> str:
    """``<install>/data/_run`` — a sibling of the ``_loops`` data root, the same
    dir ``loopyard-quickstart.sh`` writes its pidfiles/logs under. Resolved through
    the shared data-dir precedence so yard + quickstart + status all agree."""
    return os.path.join(os.path.dirname(paths.resolve_data_dir(data_dir)), "_run")


def _registry_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(run_dir(data_dir), REGISTRY)


def read_all(data_dir: Optional[str] = None) -> dict:
    """The service registry (name → entry), or ``{}`` if none/unreadable. Never
    raises — a corrupt registry reads as empty."""
    try:
        with open(_registry_path(data_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(services: dict, data_dir: Optional[str] = None) -> str:
    path = _registry_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(services, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def record(name: str, *, pid: int, now: float, port: Optional[int] = None,
           cmd: Optional[list] = None, log: Optional[str] = None,
           data_dir: Optional[str] = None) -> dict:
    """Upsert a service entry as RUNNING. Overwrites the prior record for
    ``name`` (a fresh spawn supersedes the old pid). Returns the entry."""
    services = read_all(data_dir)
    entry = {"name": name, "pid": int(pid), "port": port,
             "cmd": list(cmd) if cmd else None, "log": log,
             "startedAt": now, "intent": RUNNING}
    services[name] = entry
    _write_all(services, data_dir)
    return entry


def forget(name: str, data_dir: Optional[str] = None) -> bool:
    """Drop a service from the registry. Returns True if it was present."""
    services = read_all(data_dir)
    if name in services:
        del services[name]
        _write_all(services, data_dir)
        return True
    return False


def mark_stopped(name: str, data_dir: Optional[str] = None) -> None:
    """Record that ``name`` was stopped ON PURPOSE, so a later dead-pid reads as
    ``stopped`` rather than ``crashed``. Best-effort (no-op if absent)."""
    services = read_all(data_dir)
    if name in services:
        services[name]["intent"] = STOPPED
        _write_all(services, data_dir)


def is_alive(pid: Optional[int]) -> bool:
    """True if ``pid`` names a live process. ``os.kill(pid, 0)`` probes without
    signalling: PermissionError means it exists but isn't ours (still alive)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _state(entry: dict) -> str:
    if is_alive(entry.get("pid")):
        return RUNNING
    return STOPPED if entry.get("intent") == STOPPED else CRASHED


def describe(data_dir: Optional[str] = None, *, now: Optional[float] = None) -> list:
    """A liveness snapshot of every recorded service, sorted by name:
    ``[{name, pid, port, state, uptime, cmd, log}]``. ``uptime`` is seconds since
    ``startedAt`` (only meaningful while running); ``now`` is injectable for tests."""
    out = []
    for name, entry in sorted(read_all(data_dir).items()):
        state = _state(entry)
        started = entry.get("startedAt")
        uptime = None
        if state == RUNNING and isinstance(started, (int, float)) and now is not None:
            uptime = max(0.0, now - started)
        out.append({"name": name, "pid": entry.get("pid"),
                    "port": entry.get("port"), "state": state,
                    "uptime": uptime, "cmd": entry.get("cmd"),
                    "log": entry.get("log")})
    return out


def stop(name: str, data_dir: Optional[str] = None,
         *, sig: int = signal.SIGTERM) -> str:
    """Stop a tracked service by signalling ITS recorded pid (never a broad
    pattern — isolation rule 3). Marks intent=stopped so it reads ``stopped``.
    Returns the resulting state: ``stopped`` (signalled or already dead) or the
    original state if there was nothing to signal."""
    services = read_all(data_dir)
    entry = services.get(name)
    if not entry:
        return "absent"
    pid = entry.get("pid")
    if is_alive(pid):
        try:
            os.kill(int(pid), sig)
        except OSError:
            pass
    mark_stopped(name, data_dir)
    return STOPPED
