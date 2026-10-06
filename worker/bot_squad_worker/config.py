"""Config loader — parses bot-squad config/projects.toml + config/secrets.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ``<root>/worker/bot_squad_worker/config.py`` → ``parents[2]`` is the install root.
_INSTALL_ROOT = Path(__file__).resolve().parents[2]


def default_config_dir() -> Path:
    """The worker's default ``--config`` (R1/R21/R24): the same ladder as
    ``mcp_loops.paths.config_dir()``, re-derived here so the worker never
    imports ``mcp_loops``:

        $LOOPYARD_CONFIG_DIR > $LOOPYARD_HOME/config > $LOOPYARD_INSTALL/config
                             > <install root>/config
    """
    for env, suffix in (("LOOPYARD_CONFIG_DIR", None), ("LOOPYARD_HOME", "config"),
                        ("LOOPYARD_INSTALL", "config")):
        val = os.environ.get(env)
        if val:
            base = Path(val).expanduser()
            return base / suffix if suffix else base
    return _INSTALL_ROOT / "config"


class ConfigError(Exception):
    """Raised when projects.toml/secrets.toml is missing, malformed, or
    incomplete. Carries a human-readable, multi-field detail so the worker
    can surface *which* file and *which* keys are wrong in degraded mode
    instead of crash-looping on the first KeyError."""


# Org-identity defaults (self-install Phase 0: "de-Tim the core"). These are
# TODAY's swarmdev hardcoded values, moved out of core.lifecycle into config so
# the core is org-agnostic. A missing config/org.toml keeps these defaults, so
# swarmdev behaves byte-identically. Per-org installs override via org.toml.
_DEFAULT_ORG_OWNER_CHANNEL = "200974761"      # fallback approval-ping target
# NO DEFAULT FOR THE EGRESS PROXY, AND THE EMPTY STRING IS THE POINT.
# This used to read "http://172.17.0.4:1080". The comment two blocks up justifies
# every default here as "TODAY's swarmdev values (so behavior is identical)" — a
# claim that holds for org_name and org_owner_channel, which do not move, and is
# FALSE BY CONSTRUCTION for this one: xray_resolve exists precisely because Docker
# reassigns that container's IP on every boot and rewrites org.toml to match. So
# the single value guaranteed to rot was the single value with a constant behind
# it, and the equivalence decayed with nobody touching either file — measured
# 2026-07-24 (coord-computation): the default said .4, live org.toml said .2.
# A default that silently substitutes a stale address for a missing key is worse
# than none: the send fails at a container that is not there, and the caller
# cannot tell that from a proxy that was never configured. Empty means NOT
# CONFIGURED, every consumer already spells it `proxy or None` (tg.TgClient._post,
# tg_pipelines._tg_bot_send, lifecycle._send_tg_message), and the failure now says
# which of the two it was.
_DEFAULT_TG_EGRESS_PROXY = ""


# Required keys every project entry must define. Anything not listed here is
# optional (prod_url/staging_url/dev_url default to ""; repo_master to None).
_PROJECT_REQUIRED = (
    "slug",
    "display_name",
    "repo_path",
    "deploy_branch",
    "master_branch",
    "deploy_targets",
    "tg_chat",
)


def _norm_track_entries(value: object) -> tuple[int | str, ...]:
    """Normalize a tg_tracking chats/realtime list, preserving entry *type*.

    Entries are stored AS GIVEN: an int chat_id stays an int, anything else
    becomes a stripped string (chat title or ``@username``). ``bool`` is coerced
    to str rather than silently treated as 0/1 (TOML can't produce it here, but
    a programmatic ``from_toml`` caller might). A falsy/None value yields ().
    """
    if not value:
        return ()
    out: list[int | str] = []
    for e in value:
        if isinstance(e, bool):
            out.append(str(e))
        elif isinstance(e, int):
            out.append(e)
        else:
            out.append(str(e).strip())
    return tuple(out)


@dataclass(frozen=True)
class Project:
    slug: str
    display_name: str
    repo_path: Path           # alias for repo_dev — the dev clone
    deploy_branch: str
    master_branch: str
    prod_url: str
    staging_url: str
    dev_url: str
    deploy_targets: tuple[str, ...]
    tg_chat: str
    repo_master: Path | None = None  # master clone for prod deploys + hotfixes
    # Per-project Telegram bot toggle. When False the multi-bot listener does
    # NOT poll this project's bot (its getUpdates loop is skipped entirely), so
    # a newly-registered project stays dark until the coordinator flips it on.
    # Sourced from a ``[projects.<slug>.tg]`` sub-table (``enabled = true``) or a
    # top-level ``tg_enabled`` key in the project entry; see ``from_toml``.
    tg_enabled: bool = False
    # Per-project per-user authorization opt-in. Default False so existing
    # projects (notably the LIVE 'computation' bot) keep their byte-for-byte
    # historical chat-level-only behavior: no identity resolution, no per-user
    # rate limit, no new gating, no new slash commands. ONLY when a project sets
    # ``tg_authz = true`` (e.g. swarmdev) does tg_authz.py engage in the
    # listener. Parsed exactly like ``tg_enabled`` (sub-table or flat key).
    tg_authz: bool = False
    # Chat-tracking scope (chat-tracking system, increment 1). Sourced from a
    # ``[projects.<slug>.tg_tracking]`` sub-table with keys mode/chats/realtime
    # (flat ``tg_track_*`` keys also tolerated). Scope resolution lives in
    # tg_tracking.py; these are pure config, consumed by the (later) pipelines.
    #
    # ``tg_track_mode``: "include" (track ONLY listed chats) or "exclude"
    # (track ALL chats except the listed ones).
    # ``tg_track_chats``: the include-or-exclude list. Entries are stored AS
    # GIVEN — an int chat_id, or a string matching a chat title / @username.
    # ``tg_track_realtime``: subset processed on message arrival; everything
    # else in scope is processed periodically.
    #
    # DEFAULTS (mode="exclude", empty lists) mean: ALL chats tracked, NONE
    # real-time — a safe no-op that does not change current behavior.
    tg_track_mode: str = "exclude"
    tg_track_chats: tuple[int | str, ...] = ()
    tg_track_realtime: tuple[int | str, ...] = ()
    # COST GUARD (chat-tracking system, increment 2). Master kill-switch for the
    # LLM extraction pipelines. Default False so NO project incurs LLM cost until
    # an operator explicitly opts in. The periodic ``tg_tracking_tick`` job is a
    # pure no-op for any project with this False. Sourced from a
    # ``[projects.<slug>.tg_tracking]`` sub-table (``enabled = true``) or a flat
    # ``tg_track_enabled`` key — same convention as ``tg_enabled``.
    tg_track_enabled: bool = False

    def repo_for_target(self, target: str) -> Path:
        """Pick the right clone for a deploy target.

        - ``prod``: master clone (separate dir so dev work continues
          uninterrupted while a prod deploy is in flight). Falls back to
          ``repo_path`` if ``repo_master`` is not configured.
        - everything else (``staging``, ``dev``, …): dev clone = ``repo_path``.
        """
        if target == "prod" and self.repo_master is not None:
            return self.repo_master
        return self.repo_path

    @classmethod
    def from_toml(cls, raw: dict, slug_hint: str | None = None) -> "Project":
        """Build a Project from a raw projects.toml table.

        Collects *all* missing required keys into one ConfigError rather than
        bailing on the first KeyError, so a degraded worker can report e.g.
        "project 'swarmdev': missing required keys: deploy_branch, tg_chat".
        ``slug_hint`` is the table key (used in the message when the entry
        omits its own ``slug``).
        """
        missing = [k for k in _PROJECT_REQUIRED if k not in raw]
        if missing:
            name = raw.get("slug") or slug_hint or "<unknown>"
            raise ConfigError(
                f"project {name!r}: missing required keys: " + ", ".join(missing)
            )
        # tg_enabled: accept either a [projects.<slug>.tg] sub-table with
        # ``enabled = true`` (preferred, groups future per-project TG settings)
        # or a flat ``tg_enabled`` key. Default False — projects stay dark until
        # explicitly enabled.
        tg_block = raw.get("tg") or {}
        tg_enabled = bool(tg_block.get("enabled", raw.get("tg_enabled", False)))
        # tg_authz: same parsing convention as tg_enabled. Default False.
        tg_authz = bool(tg_block.get("authz", raw.get("tg_authz", False)))
        # tg_tracking: prefer a [projects.<slug>.tg_tracking] sub-table with
        # mode/chats/realtime; tolerate flat tg_track_* keys. Defaults below are
        # a safe no-op (exclude nothing => track all; no realtime).
        track_block = raw.get("tg_tracking") or {}
        mode = str(
            track_block.get("mode", raw.get("tg_track_mode", "exclude"))
        ).strip().lower()
        if mode not in ("include", "exclude"):
            mode = "exclude"
        tg_track_chats = _norm_track_entries(
            track_block.get("chats", raw.get("tg_track_chats", ()))
        )
        tg_track_realtime = _norm_track_entries(
            track_block.get("realtime", raw.get("tg_track_realtime", ()))
        )
        # tg_track_enabled (COST GUARD): same convention as tg_enabled. Default
        # False — the LLM extraction pipelines stay dark until explicitly opted in.
        tg_track_enabled = bool(
            track_block.get("enabled", raw.get("tg_track_enabled", False))
        )
        return cls(
            slug=raw["slug"],
            display_name=raw["display_name"],
            repo_path=Path(raw["repo_path"]),
            deploy_branch=raw["deploy_branch"],
            master_branch=raw["master_branch"],
            prod_url=raw.get("prod_url", ""),
            staging_url=raw.get("staging_url", ""),
            dev_url=raw.get("dev_url", ""),
            deploy_targets=tuple(raw["deploy_targets"]),
            tg_chat=str(raw["tg_chat"]),
            repo_master=Path(raw["repo_master"]) if raw.get("repo_master") else None,
            tg_enabled=tg_enabled,
            tg_authz=tg_authz,
            tg_track_mode=mode,
            tg_track_chats=tg_track_chats,
            tg_track_realtime=tg_track_realtime,
            tg_track_enabled=tg_track_enabled,
        )


@dataclass(frozen=True)
class TgUser:
    """A known Telegram human, keyed on TG ``from.id``.

    Loaded from config/tg_users.toml (NOT auth.toml, which is keyed on web-UI
    login). ``from_id`` is the integer TG user id. ``role`` is one of
    observer/member/director/admin (capability tiers derived in tg_authz.py).
    ``projects`` is the list of project slugs this user may act within; ``["*"]``
    means all projects (admin). ``display`` is a human label for audit/replies.
    """
    from_id: int
    role: str
    projects: tuple[str, ...]
    display: str = ""

    def covers_project(self, slug: str) -> bool:
        """True if this user's project scope includes ``slug`` (``*`` = all)."""
        return "*" in self.projects or slug in self.projects


@dataclass(frozen=True)
class TgAuthzDefaults:
    """Fallback roles for senders with no tg_users.toml record.

    From the ``[defaults]`` block of tg_users.toml. ``group_member_role`` is the
    role granted to an *unknown* sender seen in a project's group chat (scoped to
    that project only). ``dm_unknown_role`` is the role for an unknown sender in
    a DM — default ``denied`` means no capabilities at all.
    """
    group_member_role: str = "observer"
    dm_unknown_role: str = "denied"


@dataclass(frozen=True)
class Config:
    config_dir: Path
    projects: dict[str, Project] = field(default_factory=dict)
    tg_bot_token: str = ""
    # Per-project bot tokens from secrets.toml [telegram_bots] (slug -> token).
    # A project with no entry here falls back to the global tg_bot_token via
    # ``bot_token_for`` — that's how 'computation' keeps using the global bot.
    telegram_bots: dict[str, str] = field(default_factory=dict)
    # Per-user TG identity store from config/tg_users.toml, keyed on int from.id.
    # Loaded like ``telegram_bots``; a missing file tolerates → empty map. Only
    # consulted for projects with ``tg_authz=True``.
    tg_users: dict[int, TgUser] = field(default_factory=dict)
    tg_authz_defaults: "TgAuthzDefaults" = field(default_factory=TgAuthzDefaults)
    tg_auth_age_max: int = 86400
    # Quiet hours during which non-urgent TG sends are dropped. UTC. Loaded
    # from <config_dir>/system_settings.toml; defaults match historical
    # 17→05 UTC sleep window for the Tashkent stakeholder.
    tg_quiet_hours_start_utc: int = 17
    tg_quiet_hours_end_utc: int = 5
    # Personal-account MTProto creds for the tg_user ingest worker. 0/"" =
    # disabled; the worker exits cleanly on startup.
    tg_user_api_id: int = 0
    tg_user_api_hash: str = ""
    # Per-org identity (self-install: org-agnostic core, specifics in org.toml).
    # Loaded tolerantly from <config_dir>/org.toml; a missing file keeps these
    # defaults. They are TODAY's swarmdev values for the two fields that DO NOT
    # MOVE — and deliberately absent for the one that does (see
    # _DEFAULT_TG_EGRESS_PROXY): a default is a standing claim of equivalence, and
    # it is only honest for a value nothing rewrites behind your back.
    #   - org_name:          human label for the org (defaults to the install slug).
    #   - org_owner_channel: fallback approval-ping target (the owner's TG chat).
    #   - tg_egress_proxy:   HTTP proxy for TG bot sends (past the DPI block).
    #                        "" = NOT CONFIGURED → direct egress, which on a
    #                        DPI-blocked host fails; the send logs which it was.
    org_name: str = "swarmdev"
    org_owner_channel: str = _DEFAULT_ORG_OWNER_CHANNEL
    tg_egress_proxy: str = _DEFAULT_TG_EGRESS_PROXY

    @property
    def data_dir(self) -> Path:
        # R2: ``$LOOPYARD_WORKER_DATA_DIR`` is a migration-only override; unset ⇒
        # the historical ``<config>/../data``.
        env = os.environ.get("LOOPYARD_WORKER_DATA_DIR")
        if env:
            return Path(env)
        return self.config_dir.parent / "data"

    @property
    def sock_path(self) -> Path:
        return self.data_dir / "_sock" / "worker.sock"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "_worker" / "heartbeat"

    @property
    def tg_user_dir(self) -> Path:
        return self.data_dir / "_tg"

    @property
    def tg_user_db_path(self) -> Path:
        return self.tg_user_dir / "messages.db"

    @property
    def tg_tracking_db_path(self) -> Path:
        # Chat-tracking state (todos/meetings/suggested_replies/watermarks).
        # Deliberately a SEPARATE db from messages.db so the ingest worker and
        # the tracking pipelines never contend on the same file/WAL.
        return self.tg_user_dir / "tracking.db"

    @property
    def tg_user_session_path(self) -> Path:
        # Telethon appends ".session"; pass the stem.
        return self.tg_user_dir / "telethon"

    def project_data_dir(self, slug: str) -> Path:
        return self.data_dir / slug

    def bot_token_for(self, slug: str) -> str:
        """Resolve a project's Telegram bot token.

        Per-project token from secrets.toml ``[telegram_bots]`` wins; otherwise
        fall back to the global ``[telegram].bot_token``. 'computation' has no
        per-project entry, so it keeps using the global token (its live bot).
        """
        return self.telegram_bots.get(slug) or self.tg_bot_token

    def enabled_tg_projects(self) -> list[Project]:
        """Projects whose per-project bot the listener should poll.

        A project is polled iff ``tg_enabled`` is True AND it has a resolvable
        bot token. Disabled projects are skipped entirely (never polled).
        """
        return [
            p for p in self.projects.values()
            if p.tg_enabled and self.bot_token_for(p.slug)
        ]

    @classmethod
    def load(cls, config_dir: Path) -> "Config":
        projects_toml = config_dir / "projects.toml"
        if not projects_toml.exists():
            raise FileNotFoundError(f"projects.toml not found: {projects_toml}")
        # R3: secrets.toml is optional — every key in it is optional, so a
        # fresh box / bundle needs no stub file.
        secrets_toml = config_dir / "secrets.toml"
        try:
            raw = tomllib.loads(projects_toml.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{projects_toml}: malformed TOML: {e}") from e

        # Validate every project entry, collecting all problems into one
        # message instead of dying on the first bad table.
        projects: dict[str, Project] = {}
        project_errors: list[str] = []
        for slug, p in raw.get("projects", {}).items():
            try:
                projects[slug] = Project.from_toml(p, slug_hint=slug)
            except ConfigError as e:
                project_errors.append(str(e))
        if project_errors:
            raise ConfigError(
                f"{projects_toml}: invalid project config:\n  - "
                + "\n  - ".join(project_errors)
            )

        try:
            sec = (tomllib.loads(secrets_toml.read_text())
                   if secrets_toml.exists() else {})
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{secrets_toml}: malformed TOML: {e}") from e

        # system_settings.toml is optional; defaults match the historical hardcoded
        # values so existing deploys behave identically until the admin writes it.
        quiet_start = 17
        quiet_end = 5
        sys_settings = config_dir / "system_settings.toml"
        if sys_settings.exists():
            sys_raw = tomllib.loads(sys_settings.read_text())
            tg_block = sys_raw.get("tg", {}) or {}
            quiet_start = int(tg_block.get("quiet_hours_start_utc", quiet_start))
            quiet_end = int(tg_block.get("quiet_hours_end_utc", quiet_end))

        tg_user = sec.get("telegram_user", {}) or {}
        telegram_bots = {
            str(k): str(v)
            for k, v in (sec.get("telegram_bots", {}) or {}).items()
            if v
        }

        # tg_users.toml — per-user identity store. Optional: a missing file
        # yields an empty map (no user is known → every sender falls through to
        # the defaults). Keyed on TG from.id (string of int in the file).
        tg_users: dict[int, TgUser] = {}
        defaults = TgAuthzDefaults()
        tg_users_toml = config_dir / "tg_users.toml"
        if tg_users_toml.exists():
            try:
                u_raw = tomllib.loads(tg_users_toml.read_text())
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{tg_users_toml}: malformed TOML: {e}") from e
            d = u_raw.get("defaults", {}) or {}
            defaults = TgAuthzDefaults(
                group_member_role=str(d.get("group_member_role", "observer")),
                dm_unknown_role=str(d.get("dm_unknown_role", "denied")),
            )
            users_block = u_raw.get("users", {}) or {}
            for key, rec in users_block.items():
                try:
                    fid = int(rec.get("from_id", key))
                except (TypeError, ValueError):
                    raise ConfigError(
                        f"{tg_users_toml}: user {key!r} has non-integer from.id"
                    )
                tg_users[fid] = TgUser(
                    from_id=fid,
                    role=str(rec.get("role", "observer")),
                    projects=tuple(rec.get("projects", []) or []),
                    display=str(rec.get("display", "")),
                )

        # org.toml — per-org identity (self-install). Optional: a missing file
        # keeps TODAY's swarmdev defaults so behavior is byte-identical. The org
        # name falls back to the install-dir slug (org-agnostic), the owner
        # channel + egress proxy to the hardcoded-today defaults.
        org_name = config_dir.parent.name or "swarmdev"
        org_owner_channel = _DEFAULT_ORG_OWNER_CHANNEL
        tg_egress_proxy = _DEFAULT_TG_EGRESS_PROXY
        org_toml = config_dir / "org.toml"
        if org_toml.exists():
            try:
                org_raw = tomllib.loads(org_toml.read_text())
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{org_toml}: malformed TOML: {e}") from e
            org_block = org_raw.get("org", {}) or {}
            net_block = org_raw.get("network", {}) or {}
            org_name = str(org_block.get("name", org_name))
            org_owner_channel = str(
                org_block.get("owner_channel", org_owner_channel))
            tg_egress_proxy = str(
                net_block.get("tg_egress_proxy", tg_egress_proxy))

        return cls(
            config_dir=config_dir,
            projects=projects,
            tg_bot_token=sec.get("telegram", {}).get("bot_token", ""),
            telegram_bots=telegram_bots,
            tg_users=tg_users,
            tg_authz_defaults=defaults,
            tg_auth_age_max=int(sec.get("telegram", {}).get("auth_age_max", 86400)),
            tg_quiet_hours_start_utc=quiet_start,
            tg_quiet_hours_end_utc=quiet_end,
            tg_user_api_id=int(tg_user.get("api_id", 0) or 0),
            tg_user_api_hash=str(tg_user.get("api_hash", "") or ""),
            org_name=org_name,
            org_owner_channel=org_owner_channel,
            tg_egress_proxy=tg_egress_proxy,
        )
