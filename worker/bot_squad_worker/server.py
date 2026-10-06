"""FastAPI app the worker exposes over a Unix socket."""
from __future__ import annotations

import time
from importlib.metadata import version, PackageNotFoundError

from fastapi import FastAPI, HTTPException

from bot_squad_worker.actions import ActionError, DegradedError, dispatch, get_degraded


def _pkg_version() -> str:
    try:
        return version("bot-squad-worker")
    except PackageNotFoundError:
        return "0.0.0"


def build_app() -> FastAPI:
    """Build a fresh FastAPI app. Tests instantiate one per test for isolation."""
    app = FastAPI(title="bot-squad-worker", version=_pkg_version())
    started = time.monotonic()

    @app.get("/health")
    def health() -> dict:
        degraded = get_degraded()
        body = {
            "ok": not degraded,
            "version": _pkg_version(),
            "uptime": time.monotonic() - started,
        }
        if degraded:
            # Surface *why* the worker is degraded so an operator hitting
            # /health sees which file / which keys are wrong, instead of a
            # bare "connection refused" from a crash-looping daemon.
            body["degraded"] = True
            body["error"] = degraded
        return body

    @app.post("/actions/{name}")
    def call_action(name: str, params: dict | None = None) -> dict:
        try:
            return dispatch(name, params or {})
        except DegradedError as e:
            # Bad startup config: socket is up but actions can't run. 503 so
            # callers retry/alert rather than treating it as a bad request.
            raise HTTPException(status_code=503, detail=str(e))
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return app


# Module-level app for `uvicorn bot_squad_worker.server:app --uds …`
app = build_app()
