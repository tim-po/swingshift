# The `/loopyard/` convention + the capability mechanism (Round B1)

This is the platform **primitive** that later features (Objectives, Specs,
Liked-Results, Known-Issues …) are built ON — not a feature itself. Two pieces:

## 1. The `/loopyard/` project-state convention (#5)

A **Project** (a connected git repo, or a dir on an origin) may carry a
top-level **`loopyard/`** directory *inside its checkout*, holding project-scoped
state: loop configs, references, and capability **data**.

Core ships a simple manager (`mcp_loops/loopyard.py`, pure filesystem over a
resolved checkout dir):

| MCP tool | HTTP | does |
|---|---|---|
| `loopyard_list(project_id, subpath="")` | `GET /api/loops/loopyard/{project}?subpath=` | list one level (absent = empty) |
| `loopyard_read(project_id, path)` | `GET /api/loops/loopyard/{project}/file?path=` | read a text file |
| `loopyard_write(project_id, path, content)` | `POST /api/loops/loopyard/{project}/file` | create/edit a file |
| `loopyard_delete(project_id, path)` | — | delete a file |

The Project → checkout resolution reuses the server's existing
`_resolve_product_checkout`. **Safe path handling** is enforced in one place
(`loopyard.safe_join`): a relative path that escapes `loopyard/` via `..`, an
absolute path, or a symlink pointing out is refused (`LoopyardPathError` → a
clean 400). Nothing can touch a byte outside `loopyard/`.

## 2. The capability mechanism (#6)

A **capability** is a small, git-shaped package (a folder + a `manifest.json`)
that contributes any of: typed **structure** (entity-kinds), a declarative
**page**, **settings**, and an **attached loop** (its agent-native behaviour).

The split the platform honours:
- **UI is declarative** — the page is served whole and mounted by the dashboard
  shell (an iframe on the Project page), not shipped as privileged JS.
- **Backend + attached loop** run on the origin/engine.
- **Data** lives under the Project's `loopyard/<dataDir>/`.

**No sandbox**: bundled, first-party capabilities are trusted. Sandboxing is a
later shared-marketplace concern and is deliberately not built here.

### Manifest format (`manifest.json`)

```json
{
  "id": "notes",                       // slug — folder-safe, loop-name-safe (required)
  "name": "Notes",                     // display name (required)
  "version": "1",
  "entityKinds": ["note"],             // the kinds of doc/entity it manages
  "dataDir": "notes",                  // loopyard/<dataDir>/ (default: id)
  "page": { "kind": "html", "asset": "page.html" },   // declarative page (required)
  "settings": {},                      // lean settings schema (optional)
  "attachedLoop": {                    // optional agent-native behaviour
    "title": "Summarize notes",
    "config": { /* an inline loop config — validated by schema.validate_config */ }
  }
}
```

Parsed + validated in one place (`mcp_loops/capabilities.py::parse_manifest`).

### Discovery is directory-driven

Bundled capabilities ship in `mcp_loops/capabilities_bundled/`. The dashboard
**discovers** them from that dir and **mounts** each capability's page as a
Project section. Dropping a folder in or out changes what mounts — that IS the
mechanism (no registry).

| MCP tool | HTTP | does |
|---|---|---|
| `capabilities_list()` | `GET /api/loops/capabilities` | discover + list manifests (+ load errors) |
| `capability_page(cap_id)` | `GET /api/loops/capability/{cap}/page?project=` | the declarative page HTML |
| `capability_data_list(cap_id, project_id)` | `GET /api/loops/capability/{cap}/{project}/data` | list data under `loopyard/<dataDir>/` (JSON parsed inline) |
| `capability_data_write(cap_id, project_id, path, content)` | `POST …/{project}/data` | write a data file |
| `capability_trigger(cap_id, project_id)` | `POST …/{project}/trigger` | seed + `loop_save` + `loop_start` the attached loop → result envelope |
| `capability_result(cap_id, project_id)` | `GET …/{project}/result` | the attached loop's result envelope |

### Attached loops

`capability_trigger` reads the capability's `loopyard/<dataDir>/` data, seeds it
into a copy of the manifest's inline loop config (the per-run project + data
summary is appended to the **top-level goal** — agent goals stay generic, the
linter's reusable-team rule), then `loop_save` + `loop_start`. The loop name is
deterministic per (capability, project) — `cap-<cap>-<project>` — so
re-triggering reuses one record. `capability_result` returns the standard result
envelope (`task_id/status/summary/artifacts/git_commit/…`).

### The example capability: `notes`

`mcp_loops/capabilities_bundled/notes/` is the **conformance proof**, not a real
feature: a page that lists/creates notes under `loopyard/notes/`, plus a
*Summarize notes* button that triggers a trivial attached loop and surfaces its
result. Removing this folder removes the section; adding another folder adds one.

---

## First-party bundled capabilities (Round B2)

The B2 round ships the hub's first REAL capabilities on the B1 mechanism — each a
folder under `capabilities_bundled/` (manifest + declarative `page.html`), data
under the Project's `loopyard/<dataDir>/`, discovered + mounted exactly like
`notes`. They need a little more than the generic JSON round-trip; that extra is
**declared in the manifest's `settings`** (no new manifest schema) and served by
two ADDITIVE tools — the generic `capability_*` tools above are untouched.

### `settings` conventions (declarative extras)

| `settings` key | type | effect |
|---|---|---|
| `importMd` | string | a legacy markdown file under `loopyard/` (sibling of `dataDir`) to import into the item list — e.g. `objectives.md`. Parsed by `mcp_loops.loopyard_import`. |
| `importKind` | `"objective"` \| `"issue"` | which parser/vocabulary to use for `importMd`. |
| `unifyAutofiledIssues` | bool | also fold the auto-filed loop-failure issues (`data/_issues`) for THIS Project into the list (Known-Issues). |

### New tools (additive)

| MCP tool | HTTP | does |
|---|---|---|
| `capability_items(cap_id, project_id)` | `GET /api/loops/capability/{cap}/{project}/items` | unified item list: STORED JSON records + IMPORTED markdown items (`settings.importMd`) + AUTO-FILED project issues (`settings.unifyAutofiledIssues`). Each item carries a `source` (`stored`\|`imported`\|`autofiled`). Returns `{ok, items, counts}`. |
| `capability_build_loop(cap_id, project_id, title, detail?, item_id?)` | `POST …/{project}/build` | seed a loop config from an objective, bound to the Project (`projectId`), `loop_save` it as a draft. Returns `{ok, loop, editUrl, goal}`. |

The dogfood importers are pure + deterministic (`loopyard_import.parse_objectives_md`
/ `parse_issues_md`, `hub_capabilities.build_loop_from_objective` /
`filter_autofiled`), tested in `test_loopyard_import.py`,
`test_hub_capabilities.py`, `test_hub_capabilities_server.py`.

### C1 — `objectives`

`capabilities_bundled/objectives/` — list/create/edit a Project's objectives
(title, detail, status, priority) under `loopyard/objectives/`, merged with the
imported `loopyard/objectives.md`. The KEY action: each objective's **→ Build**
button `POST …/build`s a loop seeded with the objective as the goal and the
Project pre-bound, then opens it in the editor (`editUrl` → `/loops/<name>`; the
`/newloop` creator can't be URL-seeded, so a draft is saved and opened prefilled).
This is the bridge **intent → team**.

### C4 — `known-issues`

`capabilities_bundled/known-issues/` — a curated per-Project issue list (title,
detail, severity, status) under `loopyard/known-issues/`, imported from
`loopyard/known-issues.md`, **unified in one view** with the existing auto-filed
loop-failure issues (`data/_issues`) filtered to this Project. Auto-filed records
carry no `projectId`, so the join is `issue.loop → loop config → projectId`.
Auto-filed rows link to the existing Issues detail view; manual issues add/edit
via the generic `…/data` write.

### Doc-shaped capabilities: CSP-safe rendering (C2 + C3)

Specs and Liked-Results both **render a file** (markdown OR arbitrary,
hand-authored HTML) in the dashboard. The renderer (`mcp_loops/hub_render.py`) is
pure + deterministic and safe by **two independent guards**:

1. a tiny dependency-free **markdown → sanitized HTML** pass (headings, emphasis,
   inline + fenced code, links with a `http/https/mailto/relative` scheme
   allowlist, lists, blockquotes, rules) that HTML-escapes **every** text run, so
   no markup in the source can inject an element;
2. every rendered payload is a **complete document carrying a strict
   `Content-Security-Policy` meta** (`default-src 'none'; style-src
   'unsafe-inline'; img-src data:`) — a raw-HTML spec can fetch nothing. The
   dashboard drops the document into a **`sandbox` iframe via `srcdoc`** (no
   `allow-scripts`), which independently neutralizes any script.

| MCP tool | HTTP | does |
|---|---|---|
| `capability_render(cap_id, project_id, path)` | `GET …/{project}/render?path=` | render one doc under `loopyard/<dataDir>/` (md/html/text) → `{ok, kind, title, document}` (a full CSP-carrying doc for an iframe `srcdoc`). `path` must be under the capability's own `dataDir`. |

### C2 — `specs`

`capabilities_bundled/specs/` — design docs attached to a Project under
`loopyard/specs/`, listed and **rendered in-dashboard** (md AND html, CSP-safe).
Any spec can be **handed to the `+ loop` creator as trusted context** — reusing
the C1 creator-context store (`_creator_context/*.md`), framed there as reference
background (not instructions). Existing `specs/ARCHITECTURE.md` renders with no
re-authoring.

| MCP tool | HTTP | does |
|---|---|---|
| `capability_specs_list(project_id)` | `GET /api/loops/capability/specs/{project}/list` | list `loopyard/specs/` files with render `kind` |
| `capability_spec_use_context(project_id, path, title?)` | `POST …/{project}/use-context` | write a spec into the creator context-handoff store (deterministic doc name per project+spec) → `{ok, contextDoc, bytes}` |

### C3 — `liked-results`

`capabilities_bundled/liked-results/` — a gallery of outputs a user pinned, kept
under `loopyard/results/` with a `<name>.meta.json` **provenance sidecar**
(source run/loop, note, pinned-at). Results **render inline** (html/md, CSP-safe).
A bare content file with no sidecar (e.g. the dogfood `results/architecture.html`)
lists as `source:"imported"`. Pin **from a loop's `_output/`** or a provided file.

| MCP tool | HTTP | does |
|---|---|---|
| `capability_results_list(project_id)` | `GET /api/loops/capability/liked-results/{project}/list` | pinned results fused with their provenance sidecars, newest first |
| `capability_result_pin(project_id, name?, note?, loop?, src?, content?, pinned_at?)` | `POST …/{project}/pin` | pin a loop-output file (`loop`+`src`, read safely from `_output/<loop>/`) or provided `content`; writes the content + a `.meta.json` sidecar → `{ok, entry, meta}` |

The renderer + tools are covered by `test_hub_render.py` (incl. adversarial
script/link/remote-asset cases) and `test_hub_docs_server.py` (list/render/
use-context/pin + the loopyard-Project dogfood).
