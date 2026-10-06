# Swingshift

Swingshift is a self-hosted loop engine for AI work that should keep moving
after the first prompt. Give a small team of agents a goal, let them hand work
to one another, and come back to a result with its checks and decisions intact.

It is designed to stay CLI-agnostic. Use the runtimes and model subscriptions
you already have, including Codex and Cursor, and keep your code, keys, and
outputs on the machine you control.

## See the product

- **[Swingshift landing page](https://tim-po.github.io/swingshift/)**
- **[Quickstart](docs/QUICKSTART.md)**
- **[Setup and installation](docs/SETUP.md)**
- **[Connecting an agent](docs/CONNECT.md)**
- **[Loop lifecycle](docs/LOOP-LIFECYCLE.md)**

The landing page is a static GitHub Pages site. Its beta form submits to the
public signup endpoint at `swingshift.nolimlabs.uk`; no bot credentials or
signup data are stored in this repository.

## What is in this repository

- `mcp_loops/` — loop engine, authoring, runtimes, connectors, and CLI
- `worker/` — worker process and runtime dependencies
- `tracking_ui/` — local dashboard and loop views
- `control_plane/` and `hub/` — optional self-hosted control-plane pieces
- `frontend/` — dashboard and desktop/portal clients
- `bundle/` and `install.sh` — installation and origin bundle tooling
- `docs/` — product and operator documentation suitable for a public checkout

## Local development

```bash
git clone https://github.com/tim-po/swingshift.git
cd swingshift
./install.sh
```

The installer creates local runtime state under the install directory and reads
credentials from your own ignored `config/secrets.toml`. Start with the
quickstart before enabling the optional control plane or remote hub.

## Scope and privacy

This repository contains source and public-facing documentation only. Runtime
data, local projects, logs, credentials, release keys, private beta notes, and
deployment state are intentionally excluded. Contributions should keep secrets
in ignored local configuration and should never commit customer data.

## License

No license has been declared yet. Until one is added, the code is available for
inspection and evaluation from this repository; do not assume broad reuse
rights.
