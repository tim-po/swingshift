#!/usr/bin/env python3
"""loops_dashboard.py — the standalone, auth-free Loopyard dashboard for a
SELF-HOSTED install.

`gate.py` is the *coordinator* dashboard (Telegram-login auth + chat + cloud
settings + mac-origin bootstrap) and depends on the coordinator's shared package,
so it is deliberately NOT part of a clean Loopyard release. Without a shipped
entrypoint a fresh install could only serve the bare `/panel/` loops *table* off
the MCP server — the rich SPA (the +Loop creator with rule-checked team
**suggestions**, Loops / Runs / Agents / Origins / Projects / Terminal) was
unreachable.

This module serves that SAME SPA and the SAME `/api/loops/*` surface with **no
auth** — it is bound to localhost on the user's own box, so safety is the user's
own perimeter (single-tenant, own compute). It is a thin router over
`tracking_ui.loops_panel` (which is coordinator-free and talks to the MCP loops
server via `MCP_LOOPS_URL`), and it also serves what the SPA loads same-origin:
the vendored xterm assets + PWA manifest/service-worker/icons, and the
PTY-over-WebSocket terminal (`terminal.terminal_ws`) so the Terminal view and the
+Loop creator's live terminal pane work. Because there is no gate in front, the
WebSocket's trust boundary IS the localhost bind — do not expose the dash port
publicly. The coordinator-only routes (gate login, chat, project cloud config,
mac-origin) are intentionally omitted.

Run:
    python -m tracking_ui.loops_dashboard
    # or:  python tracking_ui/loops_dashboard.py
Env:
    LOOPYARD_DASH_HOST   bind host   (default 127.0.0.1)
    LOOPYARD_DASH_PORT   bind port   (default 8811)
    MCP_LOOPS_URL        loops server (default http://127.0.0.1:8771/mcp)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `tracking_ui` importable when this file is run directly (python path/to/file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from starlette.routing import Mount, Route, WebSocketRoute  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from tracking_ui import loops_panel  # noqa: E402  (coordinator-free loops monitor + APIs)
from tracking_ui import terminal     # noqa: E402  (PTY-over-WebSocket; stdlib + starlette only)
from tracking_ui import chat_api     # noqa: E402  (agent-chat SSE / long-poll over the thread)
from tracking_ui import observability_api as OBS  # noqa: E402  (goodness / thought-log)
from tracking_ui import audit_api as AUD  # noqa: E402  (P3 dispatch audit, read-only)
from tracking_ui import owner_api as OWN  # noqa: E402  (Phase 5 owner-scoped reads)
from tracking_ui import origin_connect as OCX  # noqa: E402  (one-time origin-connect link)
from tracking_ui import session_attach_api as SAA  # noqa: E402  (one-time session-attach link, owner side)
from tracking_ui import city_delivered as CITY  # noqa: E402  (City: git-delivered file kinds, read-only)
from tracking_ui import session_attach_route as SAR  # noqa: E402  (the link + scoped /__attach, AI side)
from tracking_ui import onboarding_api as ONB  # noqa: E402  (getting-started checklist, read-only)
from mcp_loops import paths  # noqa: E402
from mcp_loops import _version  # noqa: E402

_STATIC = Path(__file__).resolve().parent / "static"
_PWA = _STATIC / "pwa"

# The new modular Vite frontend (frontend/apps/web), built to dist/ and served
# under /app/ ALONGSIDE the legacy SPA — a soft, reversible cutover:
#   LOOPYARD_WEB_APP=0          hide /app entirely (rollback)
#   LOOPYARD_WEB_APP_DEFAULT=0  opt out of making "/" land on the new app (legacy stays at /loops etc.)
# $LOOPYARD_WEB_DIST, else the checkout's frontend/apps/web/dist, else a tarball
# install's <root>/web (B-3: where `origin_bundle build --web-dist` puts it).
_WEB_DIST = paths.web_dist()


def _web_app_enabled() -> bool:
    return os.environ.get("LOOPYARD_WEB_APP", "1").strip().lower() not in ("0", "false", "no") \
        and (_WEB_DIST / "index.html").is_file()


async def _web_app(request):
    """Serve a built asset, or index.html for any client-side route (SPA fallback)."""
    rel = request.path_params.get("path", "")
    f = (_WEB_DIST / rel).resolve()
    if rel and f.is_file() and _WEB_DIST.resolve() in f.parents:
        # Hashed assets are immutable; everything else revalidates.
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        return FileResponse(str(f), headers={"Cache-Control": cache})
    return FileResponse(str(_WEB_DIST / "index.html"), headers={"Cache-Control": "no-cache"})


async def _version_api(request):
    """GET /api/version (B-3): the engine's stamp — the web skew banner compares
    it with its own VITE_LOOPYARD_VERSION; `yard start` compares the roots (I-5)."""
    return JSONResponse(_version.version_info(), headers={"Cache-Control": "no-cache"})


async def _manifest(request):
    f = _PWA / "manifest.webmanifest"
    if f.exists():
        return FileResponse(str(f), media_type="application/manifest+json")
    return JSONResponse({"error": "manifest missing"}, status_code=404)


async def _service_worker(request):
    f = _PWA / "sw.js"
    if f.exists():
        return FileResponse(
            str(f), media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})
    return JSONResponse({"error": "sw missing"}, status_code=404)


# ── Retired /download channel (B-15) ─────────────────────────────────────────
# The ad-hoc closed-beta .dmg page is gone: installs are portal-gated now (sign in
# → "Set up Loopyard on this machine" → the personalized one-liner). Old /download
# links still resolve, but only where LOOPYARD_DOWNLOAD_PAGE=1 mounts them, and
# they 302 to $LOOPYARD_PORTAL_URL — nothing is ever served from disk here.
_PORTAL_TARGETS = {"/download": "/", "/download/mac": "/#/setup"}


def _download_page_enabled() -> bool:
    return os.environ.get("LOOPYARD_DOWNLOAD_PAGE", "").strip().lower() in ("1", "true", "yes")


async def _download_to_portal(request):
    """302 an old /download link to the portal; honest 503 if no portal is set."""
    portal = os.environ.get("LOOPYARD_PORTAL_URL", "").strip().rstrip("/")
    if not portal.startswith(("https://", "http://")):
        return JSONResponse({"error": "downloads moved to the Loopyard portal; "
                                      "LOOPYARD_PORTAL_URL is not configured"}, status_code=503)
    return RedirectResponse(portal + _PORTAL_TARGETS[request.url.path], status_code=302,
                            headers={"Cache-Control": "no-store"})


def build_app() -> Starlette:
    """The clean subset of gate.py's route table — SPA shell + /api/loops/*.

    Route ORDER matters: literal/prefixed API routes MUST precede the
    `/api/loops/{name}` catch-all, or "issues" / "creator" / "loopyard" /
    "capability" get eaten as a loop name. This mirrors gate.py exactly.
    """
    P = loops_panel
    routes = []
    if _web_app_enabled():
        routes += [
            Route("/app", lambda r: RedirectResponse("/app/"), methods=["GET"]),
            Route("/app/{path:path}", _web_app, methods=["GET"]),
        ]
        if os.environ.get("LOOPYARD_WEB_APP_DEFAULT", "1").strip().lower() in ("1", "true", "yes"):
            routes.append(Route("/", lambda r: RedirectResponse("/app/"), methods=["GET"]))
    if _download_page_enabled():
        # Retired public download (gate-EXEMPT via gate.py): old links 302 to the portal.
        routes += [Route(path, _download_to_portal, methods=["GET"]) for path in _PORTAL_TARGETS]
    routes += [
        # One-time origin-connect link: token-gated (the minted single-use token
        # IS the bearer), so it is gate-EXEMPT like /mac-origin/ in gate.py.
        Route("/origin-connect/{token}", OCX.serve_connect, methods=["GET"]),
    ]
    # One-time SESSION-ATTACH link (docs/SESSION-ATTACH.md): GET /session-attach/<token>
    # serves the attach instructions once; /__attach/{claim,mcp} is the scoped,
    # credential-gated connector surface (never raw :8771). Each route authenticates
    # its own secret, so on the public gate these prefixes are gate-EXEMPT.
    routes += SAR.build_routes(SAA._root)   # root resolved lazily per request
    routes += [
        # ── SPA shell: every nav path serves the same page; the client router
        # reads location.pathname, so a hard refresh / pasted deep link resolves.
        Route("/", P.loops_page, methods=["GET"]),
        Route("/loops", P.loops_page, methods=["GET"]),
        Route("/runs", P.loops_page, methods=["GET"]),
        Route("/agents", P.loops_page, methods=["GET"]),
        Route("/origins", P.loops_page, methods=["GET"]),
        Route("/projects", P.loops_page, methods=["GET"]),
        Route("/products", P.loops_page, methods=["GET"]),   # legacy alias
        Route("/terminal", P.loops_page, methods=["GET"]),
        Route("/newloop", P.loops_page, methods=["GET"]),
        Route("/issues", P.loops_page, methods=["GET"]),
        Route("/issues/{id:path}", P.loops_page, methods=["GET"]),
        Route("/hub", P.loops_page, methods=["GET"]),   # objectives queue + issue log
        # REDESIGN-SPEC §2/§8 — the §7 surfaces: the per-project Overview and the
        # first-class Sessions roster (the SPA reads location.pathname on refresh).
        Route("/overview", P.loops_page, methods=["GET"]),
        Route("/sessions", P.loops_page, methods=["GET"]),
        Route("/roles", P.loops_page, methods=["GET"]),   # Agents → Roles (rd-sessions rename)
        Route("/loops/{name:path}", P.loops_page, methods=["GET"]),
        Route("/agents/{id:path}", P.loops_page, methods=["GET"]),
        Route("/api/version", _version_api, methods=["GET"]),   # B-3 version stamp
        # ── Issues API (must precede /api/loops/{name}) — REDESIGN-SPEC §3
        # (rd-issues): the reactive inbox stream + first-class filing + inline
        # triage. The /file and /triage POSTs are literal prefixes, so they must
        # precede the /issue/{id} drill-in AND the /{name} catch-all.
        Route("/api/loops/issues", P.issues_list_api, methods=["GET"]),
        Route("/api/loops/issues/file", P.issue_file_api, methods=["POST"]),
        Route("/api/loops/issues/triage", P.issue_triage_api, methods=["POST"]),
        Route("/api/loops/issue/{id:path}", P.issue_detail_api, methods=["GET"]),
        Route("/api/loops/audit", AUD.origin_audit_api, methods=["GET"]),  # P3 dispatch audit (precede /{name})
        # ── §6 targets (a): the Idea Hub two-pane living-document workspace.
        # New living-doc model (loop_docs_* / point-a-loop promotes). The legacy
        # zero-setup objectives/issues store routes below stay for back-compat but
        # the Hub view now reads /docs. Literal prefixes — MUST precede /{name}.
        Route("/api/loops/docs", P.docs_list_api, methods=["GET"]),
        Route("/api/loops/docs/create", P.doc_create_api, methods=["POST"]),
        Route("/api/loops/docs/update", P.doc_update_api, methods=["POST"]),
        Route("/api/loops/docs/point", P.doc_build_loop_api, methods=["POST"]),
        Route("/api/loops/docs/move", P.doc_move_api, methods=["POST"]),  # a plan is the loops' workstream
        Route("/api/loops/doc/{id:path}", P.doc_get_api, methods=["GET"]),
        # ── §8 (Q2): the objective-manager THREAD docked to a Hub doc. The two
        # POST verbs are literal prefixes and MUST precede /thread/{id:path}.
        # agent-chat live delivery (tracking_ui/chat_api.py): SSE + long-poll over
        # the same thread view. Literal /chat/ prefix — MUST precede /api/loops/{name}.
        Route("/api/loops/chat/events/{id:path}", chat_api.thread_events_api, methods=["GET"]),
        Route("/api/loops/chat/wait/{id:path}", chat_api.thread_wait_api, methods=["GET"]),
        Route("/api/loops/chat/threads", chat_api.thread_list_api, methods=["GET"]),
        Route("/api/loops/thread/attach", P.thread_attach_api, methods=["POST"]),
        Route("/api/loops/thread/post", P.thread_post_api, methods=["POST"]),
        Route("/api/loops/thread/{id:path}", OWN.thread_get_api, methods=["GET"]),
        # ── §7 (rd-sessions): a capability-honest "Send" — a real dispatched task
        # to a live app session. Literal prefix — MUST precede /api/loops/{name}.
        Route("/api/loops/session/dispatch", P.session_dispatch_api, methods=["POST"]),
        # ── the legacy hub — objectives queue + issue log, zero-setup local store
        # (SLICE-1-SPEC §4.5 / BRIEF-ADDENDUM). Literal prefix — MUST precede
        # /api/loops/{name} below, else "hub" is eaten by the loop-detail catch-all.
        Route("/api/loops/hub", P.hub_view_api, methods=["GET"]),
        Route("/api/loops/hub/objective", P.hub_objective_api, methods=["POST"]),
        Route("/api/loops/hub/issue", P.hub_issue_api, methods=["POST"]),
        # ── Hub-as-Workspace (HUB-WORKSPACE-SPEC, LOCKED): objectives = folders of
        # statused docs + the additive-only suggestion store + the two origin
        # agents. Literal/prefixed — MUST precede /api/loops/{name}, or "workspace"
        # is eaten by the loop-detail catch-all. Verb-specific POSTs precede the
        # /doc/{oid}/{slug} and /objective/{id} drill-ins.
        Route("/api/loops/workspace/objectives", P.ws_objectives_api, methods=["GET"]),
        Route("/api/loops/workspace/objective/create", P.ws_objective_create_api, methods=["POST"]),
        Route("/api/loops/workspace/objective/override", P.ws_objective_override_api, methods=["POST"]),
        Route("/api/loops/workspace/objective/build-loop", P.ws_objective_build_loop_api, methods=["POST"]),
        Route("/api/loops/workspace/objective/{id:path}", P.ws_objective_get_api, methods=["GET"]),
        Route("/api/loops/workspace/doc/save", P.ws_doc_save_api, methods=["POST"]),
        Route("/api/loops/workspace/doc/status", P.ws_doc_status_api, methods=["POST"]),
        Route("/api/loops/workspace/doc/{oid}/{slug:path}", P.ws_doc_get_api, methods=["GET"]),
        Route("/api/loops/workspace/suggestion/accept", P.ws_suggestion_accept_api, methods=["POST"]),
        Route("/api/loops/workspace/suggestion/decline", P.ws_suggestion_decline_api, methods=["POST"]),
        Route("/api/loops/workspace/suggestion/discuss", P.ws_suggestion_discuss_api, methods=["POST"]),
        Route("/api/loops/workspace/suggestions/{oid:path}", P.ws_suggestions_api, methods=["GET"]),
        Route("/api/loops/workspace/to-discuss/{oid:path}", P.ws_to_discuss_api, methods=["GET"]),
        Route("/api/loops/workspace/run", P.ws_run_api, methods=["POST"]),
        # ── loops list + registry + analytics + agents
        Route("/api/loops", OWN.loops_list_api, methods=["GET"]),  # ?owner= (Phase 5)
        Route("/api/loops/registry", P.loops_registry_api, methods=["GET"]),
        Route("/api/loops/analytics", P.loop_analytics_api, methods=["GET"]),
        # Library — per-loop + per-agent quality (owner rating beside analyst
        # score). ?days=&project=&compute=. MUST precede /api/loops/{name}.
        Route("/api/loops/quality", OBS.loop_quality_api, methods=["GET"]),
        Route("/api/loops/agent", OWN.loop_agent_api, methods=["GET"]),
        Route("/api/loops/agent/record", P.loop_agent_record_api, methods=["GET"]),
        Route("/api/loops/agent/versions", P.loop_agent_versions_api, methods=["GET"]),
        Route("/api/loops/agent/diff", P.loop_agent_diff_api, methods=["GET"]),
        Route("/api/loops/agent/distill", P.loop_agent_distill_api, methods=["GET"]),
        Route("/api/loops/agent/distill/accept", P.loop_agent_distill_accept_api, methods=["POST"]),
        Route("/api/loops/agents", P.loop_agents_api, methods=["GET"]),
        # ── projects (+ legacy products aliases)
        Route("/api/loops/projects", P.loop_projects_api, methods=["GET"]),
        Route("/api/loops/projects/add", P.loop_project_add_api, methods=["POST"]),
        Route("/api/loops/projects/save", P.loop_project_save_api, methods=["POST"]),
        Route("/api/loops/projects/delete", P.loop_project_delete_api, methods=["POST"]),
        Route("/api/loops/projects/assign", P.loop_projects_assign_api, methods=["POST"]),
        # REDESIGN-SPEC §5.4 — the per-project disposition aggregate ("this project:
        # 4 good / 1 bad") the Loops list badges cards + rollup from.
        Route("/api/loops/projects/{project}/dispositions",
              P.loop_project_dispositions_api, methods=["GET"]),
        # REDESIGN-SPEC §5.4 — the SAME aggregate for the "All projects" fleet view.
        # A project-less path (no double-slash) so the frontend never builds the
        # empty-segment `/projects//dispositions` that Starlette 404s (uxui LIVE
        # FAILURE #2); the handler reads project="" ⇒ whole fleet. MUST precede
        # /api/loops/{name} below.
        Route("/api/loops/dispositions",
              P.loop_project_dispositions_api, methods=["GET"]),
        # REDESIGN-SPEC §2 — per-project nav counts (the switcher rescopes the rail
        # in lockstep). ?project= empty ⇒ fleet. MUST precede /api/loops/{name}.
        Route("/api/loops/counts", P.loop_project_counts_api, methods=["GET"]),
        # Home's getting-started checklist (mcp_loops/onboarding.py is the one
        # source of truth). Read-only. MUST precede /api/loops/{name}.
        Route("/api/loops/onboarding", ONB.onboarding_api, methods=["GET"]),
        Route("/api/loops/products", P.loop_products_api, methods=["GET"]),
        Route("/api/loops/products/add", P.loop_product_add_api, methods=["POST"]),
        Route("/api/loops/products/save", P.loop_product_save_api, methods=["POST"]),
        Route("/api/loops/products/delete", P.loop_product_delete_api, methods=["POST"]),
        # ── origins (REDESIGN-SPEC §5.1 — the standalone /runs read-API is retired
        # with the /runs surface; the standalone CREATE path below is kept).
        Route("/api/loops/origins", P.loops_origins_api, methods=["GET"]),
        # owner-side mint / status / revoke of one-time connect links (gated API)
        # the current Hub (which machine; switchable). Literal, before /{name}.
        Route("/api/loops/origin-hub", OCX.hub_status_api, methods=["GET"]),
        Route("/api/loops/origin-hub", OCX.hub_set_api, methods=["POST"]),
        Route("/api/loops/origin-connect", OCX.mint_api, methods=["POST"]),
        Route("/api/loops/origin-connect/{id}", OCX.status_api, methods=["GET"]),
        Route("/api/loops/origin-connect/{id}/revoke", OCX.revoke_api, methods=["POST"]),
        Route("/api/loops/origin/{id}/capabilities", P.loop_origin_capabilities_api, methods=["GET"]),
        # ── §7 read-models (rd-origins / rd-sessions): the Fleet pill, the Sessions
        # roster, and the one shared health-aware Origin picker. Literal prefixes —
        # must precede the /api/loops/{name} catch-all.
        Route("/api/loops/fleet", P.fleet_summary_api, methods=["GET"]),
        Route("/api/loops/sessions", P.sessions_list_api, methods=["GET"]),
        # the Sessions page's "Attach a session" panel (mint / list / revoke a
        # one-time link). Literal — precedes the /{name} catch-all.
        Route("/api/loops/sessions/attach-link", SAA.attach_link_api, methods=["POST"]),
        Route("/api/loops/sessions/attaches", SAA.attaches_api, methods=["GET"]),
        Route("/api/loops/sessions/attach/revoke", SAA.attach_revoke_api, methods=["POST"]),
        # ── the unified Device Registry (DEVICE-REGISTRY-SPEC Phase 2): ONE list
        # over boxes + sessions + the dial-out origin-agent. Additive; origins and
        # sessions above are unchanged. Literal — precedes the /{name} catch-all.
        Route("/api/loops/devices", OWN.devices_list_api, methods=["GET"]),  # ?owner= (Phase 5)
        Route("/api/loops/origin-picker", P.origin_picker_api, methods=["GET"]),
        Route("/api/loops/agent/favorite", P.loop_agent_favorite_api, methods=["POST"]),
        Route("/api/loops/standalone", P.loop_standalone_api, methods=["POST"]),
        # ── the +Loop creator: preprompt, validate, and the rule-checked SUGGEST
        # (these MUST precede /api/loops/{name}).
        Route("/api/loops/creator/prompt", P.loop_creator_prompt_api, methods=["GET"]),
        Route("/api/loops/creator/validate", P.loop_creator_validate_api, methods=["POST"]),
        Route("/api/loops/creator/suggest", P.loop_creator_suggest_api, methods=["POST"]),
        # AI-AUTHORING — live lint + schematic for an UNSAVED New-Loop draft (pure).
        Route("/api/loops/creator/draft-check", P.loop_creator_draft_check_api, methods=["POST"]),
        Route("/api/loops/creator/scaffold", P.loop_creator_scaffold_api, methods=["POST"]),
        # REDESIGN-SPEC §5.5 — the native conversational BRIEF (Door B) for the +Loop
        # composer's create-by-chat flow; daemon-free. MUST precede /api/loops/{name}.
        Route("/api/loops/creator/brief", P.loop_creator_brief_api, methods=["POST"]),
        # OWNER RESTORE (2026-09-22): the per-loop ✎Brief / ↩Debrief buttons drive the
        # REAL tmux adopt-the-manager session again (loop_brief_session/loop_debrief_session
        # via loops_panel). These lived only in the coordinator app's route table, so the
        # redesign's clean subset 404'd them — hence the temporary swap to the native chat.
        # Registering them here restores the old behaviour: spawning the session is also
        # the owner's smoke-test that the daemon/tmux substrate is up. The native chat
        # stays for the composer. MUST precede /api/loops/{name}.
        Route("/api/loops/{name}/brief", P.loop_brief_api, methods=["POST"]),
        Route("/api/loops/{name}/debrief", P.loop_debrief_api, methods=["POST"]),
        Route("/api/loops/save", P.loop_save_api, methods=["POST"]),
        Route("/api/loops/clone", P.loop_clone_api, methods=["POST"]),
        # ── per-loop detail (catch-all — LAST)
        Route("/api/loops/{name}", OWN.loop_detail_api, methods=["GET"]),  # ?owner= refusal (Phase 5)
        Route("/api/loops/{name}/config", OWN.loop_config_api, methods=["GET"]),  # ?owner= refusal (Phase 5)
        # REDESIGN-SPEC §5.3 — the team-room re-composition + an agent's report history.
        # Phase 5: every per-loop GET reader below is OWN-guarded (?owner= → 403 cross-owner).
        Route("/api/loops/{name}/teamroom", OWN.guarded(P.loop_teamroom_api), methods=["GET"]),
        # REDESIGN-SPEC §5.4 — the closing Resolution card (computed honest verdict).
        Route("/api/loops/{name}/resolution", OWN.guarded(P.loop_resolution_api), methods=["GET"]),
        # Run observability §1 — dual goodness (owner rating beside analyst score).
        Route("/api/loops/{name}/goodness", OWN.guarded(OBS.loop_goodness_api), methods=["GET"]),
        # Run observability §2 — thought-log (capped one-line gist per turn).
        Route("/api/loops/{name}/thoughtlog", OWN.guarded(OBS.loop_thoughtlog_api), methods=["GET"]),
        Route("/api/loops/{name}/agent-reports", OWN.guarded(P.loop_agent_reports_api), methods=["GET"]),
        Route("/api/loops/{name}/turn", OWN.guarded(P.loop_turn_api), methods=["GET"]),
        Route("/api/loops/{name}/files", OWN.guarded(P.loop_files_api), methods=["GET"]),
        # City building style: ext counts of the files the loop delivered in git. GET-only, cached.
        Route("/api/loops/{name}/delivered", OWN.guarded(CITY.loop_delivered_api), methods=["GET"]),
        Route("/api/loops/{name}/file", OWN.guarded(P.loop_file_api), methods=["GET"]),
        Route("/api/loops/{name}/download", OWN.guarded(P.loop_download_api), methods=["GET"]),
        Route("/api/loops/{name}/action", P.loop_action_api, methods=["POST"]),
        Route("/api/loops/{name}/disposition", P.loop_disposition_api, methods=["POST"]),
        # ── web Terminal (PTY over WebSocket) for the LOCAL origin. Under the
        # coordinator gate this is auth-gated; here the localhost bind + the handshake
        # Origin/Host check (terminal_ws_local, loopyard-bug-1790562043) are the trust
        # boundary (see module docstring). Powers the Terminal view AND the +Loop
        # creator's live terminal pane.
        WebSocketRoute("/ws/terminal", terminal.terminal_ws_local),
        # ── same-origin assets the SPA loads: PWA manifest/service-worker (installable
        # dashboard) + the narrow /static mount (vendored xterm, PWA icons). No CDN,
        # CSP-safe. Without these the creator/terminal xterm + the manifest 404.
        Route("/manifest.webmanifest", _manifest, methods=["GET"]),
        Route("/sw.js", _service_worker, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=str(_STATIC), check_dir=False), name="static"),
    ]
    return Starlette(routes=routes)


app = build_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("LOOPYARD_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("LOOPYARD_DASH_PORT", "8811"))
    url = os.environ.get("MCP_LOOPS_URL", "http://127.0.0.1:8771/mcp")
    print(f"loops-dashboard: serving the Loopyard SPA on http://{host}:{port}  "
          f"(loops server: {url})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
