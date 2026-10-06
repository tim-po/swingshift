# tracking_ui/chat-ui: RETIRED (agent-chat-build, 2026-09-26)

This folder held a scaffold assistant-ui client (board ac1c5a6c) with a placeholder
echo adapter. It talked to the coordinator-only `/api/chat/{project}` routes
(coord_core `poll_web` / `dash_chat`), which the Loopyard dashboard
(`tracking_ui/loops_dashboard.py`) does not serve, and it fell back to a hard-coded
`swarmdev` project. Nothing built or served it here. The code was removed so no
fake or broken chat ships. Its plan (coord_core stream-json, gate SSE) is
superseded by the Loopyard-native design below.

## Where agent chat lives now
- **Store and dispatch:** `mcp_loops/threads.py`. There is one durable thread per
  Hub doc. Each agent turn is a connect task sent to the attached session, and the
  reply folds in only from that session's real returned envelope.
- **Tools:** `loop_thread_get`, `loop_thread_attach`, `loop_thread_post` and
  `loop_thread_list` (`mcp_loops/server.py`).
- **HTTP, served by `tracking_ui/loops_dashboard.py`:**
  - `GET /api/loops/thread/{id}`, `POST /api/loops/thread/attach`,
    `POST /api/loops/thread/post`: read, attach/detach, send.
  - `GET /api/loops/chat/events/{id}`: SSE (`thread` / `error` / `bye` events).
  - `GET /api/loops/chat/wait/{id}?rev=`: long-poll fallback.
  - `GET /api/loops/chat/threads?project=`: the Chat index over real projects.
  All of these are in `tracking_ui/chat_api.py`, except the thread
  read/attach/post proxies, which live in `loops_panel.py`.
- **UI:** the Hub doc thread (`frontend/apps/web/src/views/hub/Thread.tsx`) and the
  SPA Chat view (`frontend/apps/web/src/views/chat/`), which reuses that same
  thread surface.

The connect protocol has no partial-result channel. "Live" means the pending
states (queued, then working, then folded) are pushed as they happen. Reply text
arrives whole when the session returns and is never simulated.
