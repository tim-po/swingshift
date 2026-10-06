"""Code-enforced authorization for multi-user Telegram direction of the swarm.

LOAD-BEARING SECURITY PRINCIPLE (design §"Core security principle"): the TG
conversational brain runs ``claude -p --dangerously-skip-permissions`` on text
from group members (attacker-controllable). Therefore authorization is NEVER
delegated to the LLM. It is enforced here, in deterministic code, at a broker
boundary in front of ``actions.dispatch()`` — and the ``Principal`` is derived
from the *message envelope* (``from.id``), not from any LLM/user-typed text. An
observer cannot escalate by claiming "I'm admin" in chat.

This module is PURE and unit-testable: ``resolve_identity`` and ``authorize``
take plain inputs (cfg, message dict, project, params) and return dataclasses,
with the only side-effect being the file-based rate limiter, which takes an
explicit ``data_dir`` so it is injectable in tests.

Scope: this layer is engaged ONLY for projects with ``tg_authz=True``.
``needs_confirm`` is returned for OUTWARD_ACTIONS; Phase 2 wires the confirm-first
flow over Telegram (see ``tg_pending.py`` + ``tg_listener._handle_slash_authz`` /
the awaiting-confirm branch in ``handle_update``), with ``/deploy`` as the first
outward action. ``Principal.can_confirm_gated`` gates WHO may confirm.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Principal — WHO is asking, re-derived fresh every turn from the envelope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The authority for one request, derived from the message envelope.

    - ``from_id``: TG user id of the sender (``message.from.id``).
    - ``role``: observer | member | director | admin | denied.
    - ``projects``: project slugs this principal may act within; ``("*",)`` =
      all. For unknown group senders this is scoped to the single project whose
      bot received the message.
    - ``source``: how the identity was resolved — "dm_record", "dm_unknown",
      "group_record", "group_unknown" — kept for audit/logging.
    """
    from_id: int
    role: str
    projects: tuple[str, ...]
    source: str

    def covers_project(self, slug: str) -> bool:
        return "*" in self.projects or slug in self.projects

    @property
    def can_confirm_gated(self) -> bool:
        """Whether this principal is allowed to CONFIRM an OUTWARD/gated action.

        Confirming a confirm-first action is itself an authority-bearing act, so
        it requires the same tier that could raise one: a principal able to
        ``dispatch`` (director/admin). observer/member can never confirm a gated
        action — they could not have raised it in the first place. The
        cross-user binding (confirm must come from the raiser, or an admin) is
        enforced separately at the call site against the pending record.
        """
        caps = caps_for(self.role)
        return "dispatch" in caps or "all" in caps

    @property
    def is_admin(self) -> bool:
        return "all" in caps_for(self.role)


# ---------------------------------------------------------------------------
# Decision — the result of an authorize() call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """allow | deny(reason) | needs_confirm. Use the constructors below."""
    outcome: str                       # "allow" | "deny" | "needs_confirm"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    @property
    def denied(self) -> bool:
        return self.outcome == "deny"

    @property
    def needs_confirm(self) -> bool:
        return self.outcome == "needs_confirm"


def allow() -> Decision:
    return Decision("allow")


def deny(reason: str) -> Decision:
    return Decision("deny", reason)


def needs_confirm(reason: str = "") -> Decision:
    return Decision("needs_confirm", reason)


# ---------------------------------------------------------------------------
# Capability tiers (design §1.2). Derived from role by ONE function.
# ---------------------------------------------------------------------------

# Capability tokens used by authorize():
#   read           list/status (scoped state)
#   inject_own     /say (inject_input) only into sessions the principal owns
#   inject_any     inject into any session in scope
#   dispatch       spawn/dispatch within projects[]
#   all            everything (admin)
_TIER_CAPS: dict[str, frozenset[str]] = {
    "denied": frozenset(),
    "observer": frozenset({"read"}),
    "member": frozenset({"read", "inject_own"}),
    "director": frozenset({"read", "inject_own", "inject_any", "dispatch"}),
    "admin": frozenset({"read", "inject_own", "inject_any", "dispatch", "all"}),
}


def caps_for(role: str) -> frozenset[str]:
    """Return the capability set for ``role`` (empty for unknown roles)."""
    return _TIER_CAPS.get(role, frozenset())


# ---------------------------------------------------------------------------
# Actions → required capability, and the outward (confirm-first) set.
# ---------------------------------------------------------------------------

# Outward-facing actions touch the world beyond the swarm; per the locked rule
# they are confirm-first even for admin. A "money/*" naming convention is also
# treated as outward (see _is_outward).
OUTWARD_ACTIONS: frozenset[str] = frozenset(
    {"deploy", "gmail_send", "tg_send_as_user", "calendar_write"}
)

# Map the logical actions this layer fronts to the capability they require.
# "inject" is special: it needs inject_own AND, unless the principal owns the
# target session, inject_any (ownership check below).
_ACTION_CAP: dict[str, str] = {
    "status": "read",
    "sessions": "read",
    "inject": "inject_own",
    "spawn": "dispatch",
    "dispatch": "dispatch",
    # OUTWARD actions require the dispatch capability to RAISE (and the same tier
    # to confirm — see Principal.can_confirm_gated). They are then gated
    # confirm-first below regardless of tier. director/admin can raise; observer/
    # member cannot. (Phase 2 wires "deploy"; the rest are pre-mapped so adding
    # their slash commands later needs no authz change.)
    "deploy": "dispatch",
    "gmail_send": "dispatch",
    "tg_send_as_user": "dispatch",
    "calendar_write": "dispatch",
}

# Dispatch-class actions are additionally rate-limited at a stricter rate.
_DISPATCH_ACTIONS: frozenset[str] = frozenset({"spawn", "dispatch"})


def _is_outward(action: str) -> bool:
    return action in OUTWARD_ACTIONS or action.startswith("money/")


# ---------------------------------------------------------------------------
# Identity resolution (design §1.3). DERIVED FROM THE ENVELOPE.
# ---------------------------------------------------------------------------


def _is_dm(msg: dict) -> bool:
    return (msg.get("chat", {}) or {}).get("type") == "private"


def resolve_identity(cfg: Any, msg: dict, proj: Any) -> Principal:
    """Derive the Principal for ``msg`` received by ``proj``'s bot.

    DM (chat.type=='private', strong identity):
      - known sender → their record (role + projects from tg_users.toml).
      - unknown sender → ``defaults.dm_unknown_role`` (default "denied"),
        scoped to this project only.
    Group (weaker identity):
      - known sender → their record.
      - unknown sender → ``defaults.group_member_role`` (default "observer"),
        scoped to THIS project's slug only (never "*").
    """
    frm = msg.get("from") or {}
    try:
        from_id = int(frm.get("id"))
    except (TypeError, ValueError):
        from_id = 0
    slug = getattr(proj, "slug", "") or ""

    record = (getattr(cfg, "tg_users", {}) or {}).get(from_id)
    is_dm = _is_dm(msg)

    if record is not None:
        return Principal(
            from_id=from_id,
            role=record.role,
            projects=tuple(record.projects),
            source="dm_record" if is_dm else "group_record",
        )

    defaults = getattr(cfg, "tg_authz_defaults", None)
    if is_dm:
        role = getattr(defaults, "dm_unknown_role", "denied") if defaults else "denied"
        source = "dm_unknown"
    else:
        role = getattr(defaults, "group_member_role", "observer") if defaults else "observer"
        source = "group_unknown"
    # Unknown senders are scoped to the single project whose bot saw them.
    return Principal(from_id=from_id, role=role, projects=(slug,), source=source)


# ---------------------------------------------------------------------------
# Per-from.id rate limiter (file/marker based; injectable data_dir).
# ---------------------------------------------------------------------------

# Defaults (design §"Defaults" #5): 60 brain-turns/hr/user; 10 dispatch/hr/user.
TURN_LIMIT_PER_HOUR = 60
DISPATCH_LIMIT_PER_HOUR = 10
_WINDOW_SECONDS = 3600


def _bucket_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "_worker" / "tg_ratelimit"


def _check_and_consume(data_dir: Path, key: str, limit: int,
                       now: Optional[float] = None) -> bool:
    """Token-bucket-ish: append a timestamp for ``key``, prune the >1h-old ones,
    and return True if the count within the trailing hour is <= ``limit``.

    State is one file per key under ``data/_worker/tg_ratelimit/``. Cheap enough
    to run BEFORE the expensive brain spawn. Returns True = under limit (allow),
    False = over limit (reject).
    """
    now = time.time() if now is None else now
    d = _bucket_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Sanitize key for use as a filename.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    f = d / f"{safe}.log"
    cutoff = now - _WINDOW_SECONDS
    stamps: list[float] = []
    if f.exists():
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    t = float(line)
                except ValueError:
                    continue
                if t >= cutoff:
                    stamps.append(t)
        except OSError:
            stamps = []
    if len(stamps) >= limit:
        # Over limit — rewrite the pruned window (do NOT add this attempt) and
        # reject. We still prune so the file doesn't grow unbounded.
        f.write_text("\n".join(f"{t:.3f}" for t in stamps) + "\n")
        return False
    stamps.append(now)
    f.write_text("\n".join(f"{t:.3f}" for t in stamps) + "\n")
    return True


def check_turn_rate(data_dir: Path, from_id: int,
                    now: Optional[float] = None) -> bool:
    """Per-from.id brain-turn rate gate. True = allowed, False = over limit.

    Call BEFORE spawning the expensive brain so unknown/abusive senders never
    trigger an LLM turn (the biggest cost lever).
    """
    return _check_and_consume(data_dir, f"turn-{from_id}", TURN_LIMIT_PER_HOUR, now)


def check_dispatch_rate(data_dir: Path, from_id: int,
                        now: Optional[float] = None) -> bool:
    """Per-from.id dispatch rate gate (stricter). True = allowed."""
    return _check_and_consume(
        data_dir, f"dispatch-{from_id}", DISPATCH_LIMIT_PER_HOUR, now
    )


# ---------------------------------------------------------------------------
# authorize() — the broker boundary. Pure given (principal, action, params).
# ---------------------------------------------------------------------------


def authorize(principal: Principal, action: str, params: Optional[dict] = None,
              *, data_dir: Optional[Path] = None) -> Decision:
    """Decide whether ``principal`` may perform ``action`` with ``params``.

    Order (design §2):
      1. project-scope check (action's target project in principal.projects)
      2. capability check (role tier grants the action's capability)
      3. ownership check (member 'inject' only into sessions they own)
      4. gating (action in OUTWARD_ACTIONS / money/* → needs_confirm)
      5. rate-limit hook (dispatch-class actions: stricter per-from.id limit)

    Returns allow() / deny(reason) / needs_confirm(). Pure except the optional
    rate-limit hook, which only touches the filesystem when ``data_dir`` is
    given and the action is dispatch-class.
    """
    params = params or {}
    caps = caps_for(principal.role)

    # 0. A denied/no-cap principal can do nothing.
    if not caps:
        return deny(f"role '{principal.role}' has no capabilities")

    # 1. Project scope. The action's project comes from params['slug'] when
    #    present (spawn/status/dispatch); inject targets a session whose project
    #    we don't re-derive here, so scope for inject is enforced via the
    #    principal's record + ownership (below) rather than a slug match.
    target_slug = params.get("slug")
    if target_slug is not None and not principal.covers_project(target_slug):
        return deny(f"not in scope for project '{target_slug}'")

    # 2. Capability.
    required = _ACTION_CAP.get(action)
    if required is None:
        # Unknown/unmapped action: deny by default (fail closed).
        if "all" not in caps:
            return deny(f"unknown action '{action}'")
        required = "all"
    if "all" not in caps and required not in caps:
        return deny(f"role '{principal.role}' cannot '{action}'")

    # 3. Ownership — a 'member' may inject ONLY into a session they own. A
    #    principal with inject_any (director/admin) skips this.
    if action == "inject" and "inject_any" not in caps and "all" not in caps:
        owner = params.get("owner")
        if owner is not None and owner != principal.from_id and \
                str(owner) != str(principal.from_id):
            return deny("can only inject into your own sessions")

    # 4. Gating: outward actions are confirm-first, even for admin.
    if _is_outward(action):
        return needs_confirm(f"'{action}' is an outward action (confirm required)")

    # 5. Rate-limit hook for dispatch-class actions.
    if action in _DISPATCH_ACTIONS and data_dir is not None:
        if not check_dispatch_rate(Path(data_dir), principal.from_id):
            return deny(
                f"dispatch rate limit ({DISPATCH_LIMIT_PER_HOUR}/hr) exceeded"
            )

    return allow()
