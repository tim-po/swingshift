"""Deterministic sync ids — spec §0 D5 / D14 / D21, backlog S-1 (b)(c)(d).

* ``originId`` — UUIDv4 minted ONCE per device (``local_origin_id``), persisted
  at ``<state_dir>/origin_id``; never derived from the key or hostname, so a
  key rotation / re-enrollment keeps it (S-1 test 7).
* ``loopId``  — the ``.loopid`` sidecar ``{"loopId","originId","dirName"}``
  when it is ours (D14), else ``uuid5(NS_LOOP, originId + "/" + dirName)``.
  Losing the sidecar re-derives the SAME id (B4). Generation n>0 (D21, a
  recreated dir after ``loop.gone``) appends ``"#" + n``; S-1 always uses 0.
* ``runId``   — ``uuid5(NS_RUN, loopId + "/" + repr(float(started)))``,
  stateless so re-ingest is idempotent. Stored in run.json as ``.hex`` (the
  form H7's ``turn_identity`` rows already carry); ``run_uuid`` gives the
  UUID object.

String forms: loopId/originId are canonical ``str(uuid)``; runId is 32-char
hex. This module is pure file I/O — no server imports.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

# Fixed forever (spec §0 D5). Changing either re-keys every loop/run everywhere.
NS_LOOP = uuid.UUID("3299da6c-3231-4175-8413-c8cc217f72d8")
NS_RUN = uuid.UUID("f9494d60-d0c6-46ec-86b3-1134a83e4db6")

SIDECAR = ".loopid"
ORIGIN_ID_FILE = "origin_id"


def _canon_uuid(value) -> Optional[str]:
    """Canonical ``str(uuid)`` for a well-formed UUID string, else None."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _atomic_write(path: str, text: str, mode: int = 0o666) -> None:
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- originId

def local_origin_id(state_dir: str) -> str:
    """This device's logical originId: minted once (UUIDv4, 0600), then re-read.

    Creation is create-if-absent via ``os.link`` of a fully written temp file,
    so two racing first calls agree on one id. A present but unreadable /
    malformed file RAISES rather than re-minting — silently minting a new id
    would fork the device's identity (every loopId re-keys)."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    path = os.path.join(state_dir, ORIGIN_ID_FILE)
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(uuid.uuid4()) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass                      # lost the race — adopt the winner's
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    with open(path, encoding="utf-8") as fh:
        oid = _canon_uuid(fh.read().strip())
    if oid is None:
        raise ValueError(f"malformed originId file: {path}")
    return oid


# ---------------------------------------------------------------- loopId / runId

def derive_loop_id(origin_id: str, dir_name: str, generation: int = 0) -> str:
    """``uuid5(NS_LOOP, originId/dirName[#n])`` — D5, D21 generation hook."""
    name = f"{origin_id}/{dir_name}"
    if generation:
        name += f"#{int(generation)}"
    return str(uuid.uuid5(NS_LOOP, name))


def run_uuid(loop_id: str, started) -> uuid.UUID:
    """D5 runId as a UUID. ``started`` is run.json's ``started`` epoch; it is
    coerced to float so an int-valued ``started`` keys the same as its float."""
    return uuid.uuid5(NS_RUN, f"{loop_id}/{float(started)!r}")


def run_id(loop_id: str, started) -> str:
    """D5 runId in its stored form (32-char hex, as H7 writes ``runId``)."""
    return run_uuid(loop_id, started).hex


# ---------------------------------------------------------------- sidecar

def read_sidecar(loop_dir: str) -> Optional[dict]:
    """The ``.loopid`` sidecar as ``{"loopId","originId","dirName"?}``, or None
    when absent or malformed (malformed == absent: the id re-derives)."""
    try:
        with open(os.path.join(loop_dir, SIDECAR), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    lid, oid = _canon_uuid(data.get("loopId")), _canon_uuid(data.get("originId"))
    if lid is None or oid is None:
        return None
    out = {"loopId": lid, "originId": oid}
    if isinstance(data.get("dirName"), str):
        out["dirName"] = data["dirName"]
    return out


def write_sidecar(loop_dir: str, loop_id: str, origin_id: str, dir_name: str) -> None:
    """Atomically (tmp + os.replace) write ``<loop_dir>/.loopid``."""
    body = {"loopId": loop_id, "originId": origin_id, "dirName": dir_name}
    _atomic_write(os.path.join(loop_dir, SIDECAR),
                  json.dumps(body, sort_keys=True) + "\n")


def split_log_path(state_dir: str) -> str:
    """Where ``loop.split`` notices go until S-2's store exists."""
    return os.path.join(state_dir, "_sync", "split.jsonl")


def _log_split(split_log: Optional[str], rec: dict) -> None:
    if not split_log:
        return
    os.makedirs(os.path.dirname(split_log), mode=0o700, exist_ok=True)
    line = json.dumps(rec, sort_keys=True) + "\n"
    fd = os.open(split_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))  # one O_APPEND write per record
    finally:
        os.close(fd)


def _rightful_claimant(data_dir: str, dir_name: str, loop_id: str) -> Optional[str]:
    """Another dir under ``data_dir`` whose sidecar carries ``loop_id`` AND
    names that dir as its ``dirName`` — i.e. the original of a copy."""
    try:
        entries = os.listdir(data_dir)
    except OSError:
        return None
    for other in sorted(entries):
        if other == dir_name:
            continue
        odir = os.path.join(data_dir, other)
        if not os.path.isdir(odir):
            continue
        sc = read_sidecar(odir)
        if sc and sc["loopId"] == loop_id and sc.get("dirName") == other:
            return other
    return None


def resolve_loop_id(data_dir: str, dir_name: str, origin_id: str, *,
                    split_log: Optional[str] = None,
                    persist: bool = True) -> str:
    """The loopId for ``<data_dir>/<dir_name>`` (D5 + D14), (re)writing the
    sidecar when ``persist`` and logging ``loop.split`` on a re-mint.

    Rules, in order:
    1. No/malformed sidecar → derived id (B4 re-adoption; no split).
    2. Sidecar ``originId`` ≠ local → re-mint to the local derived id (D14,
       rsync/scp/Time-Machine to another machine); split ``foreign_origin``.
    3. Sidecar ``dirName`` ≠ this dir AND another dir on this origin rightfully
       claims the same loopId → this dir is the copy: re-mint; split ``copy``.
    4. Otherwise honour the sidecar. A ``dirName`` mismatch with no rightful
       claimant is a rename (or a legacy sidecar without dirName): the id is
       kept and the sidecar's dirName is updated (spec D5 re-mints only when
       TWO dirs carry the same id).
    """
    origin_id = _canon_uuid(origin_id) or origin_id
    loop_dir = os.path.join(data_dir, dir_name)
    derived = derive_loop_id(origin_id, dir_name)
    sc = read_sidecar(loop_dir)

    reason = None
    if sc is None:
        loop_id = derived
    elif sc["originId"] != origin_id:
        loop_id, reason = derived, "foreign_origin"
    elif sc.get("dirName") != dir_name and _rightful_claimant(
            data_dir, dir_name, sc["loopId"]) is not None:
        loop_id, reason = derived, "copy"
    else:
        loop_id = sc["loopId"]

    if reason and sc["loopId"] == loop_id:
        reason = None   # re-mint landed on the same id (e.g. dir re-derives equal)
    if reason:
        _log_split(split_log, {
            "kind": "loop.split", "from": sc["loopId"], "to": loop_id,
            "dirName": dir_name, "fromOrigin": sc["originId"],
            "fromDirName": sc.get("dirName"), "reason": reason, "ts": time.time()})

    want = {"loopId": loop_id, "originId": origin_id, "dirName": dir_name}
    if persist and sc != want and os.path.isdir(loop_dir):
        write_sidecar(loop_dir, loop_id, origin_id, dir_name)
    return loop_id
