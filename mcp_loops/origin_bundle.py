"""origin_bundle — P2.5 §6.5: package the ONE Python origin-agent as a
self-contained, per-OS/arch bundle (PyInstaller-style), so a machine becomes a
full origin from a single downloaded artifact with no Python install.

The spec's §6.5 decision is baked in here and NOT re-litigated: keep ONE Python
origin-agent (the keypair-auth channel, mTLS mint/parse, the executor) and ship
it as a bundle — do NOT port the security-critical handshake to Node. This module
is the deterministic *planner + spec generator* for that bundle. It does not shell
out to PyInstaller (that is a per-OS CI step run on each target host); it produces
the exact build inputs and proves they are REAL — every module it names as a
hidden-import actually imports on the host — so the manifest is a fact, not a wish.

Two facts from the code make an explicit manifest mandatory, not optional:

  * **Lazy imports defeat static analysis.** :mod:`mcp_loops.origin_client` pulls
    its entire security spine in *inside methods* — ``from mcp_loops.origin_proto
    import identity``/``channel``/``tls``/``agent_core`` and ``from mcp_loops import
    detach`` are all function-local (see ``OriginClient.identity`` / ``.enroll`` /
    ``.build_client_ssl_context`` / ``.start_worker_daemon``). PyInstaller builds its
    dependency graph by static import analysis; a module imported only inside a
    method body is **invisible** to it and would be dropped from the bundle, so the
    frozen origin would ImportError on first dial. :data:`HIDDEN_IMPORTS` names them
    explicitly for ``hiddenimports=`` — this is the concrete reason the manifest
    exists.
  * **`cryptography` carries a native, per-platform binary.** ``tls.py`` mints +
    parses the Ed25519 device/Hub certs with ``cryptography`` (§6.5), which ships a
    compiled Rust/OpenSSL binding (``cryptography.hazmat.bindings._rust``). That
    binary is platform + arch specific, so the bundle matrix is **per-OS/arch** —
    there is no one portable artifact. :func:`native_dependency` names the binding
    each target must carry.

The bundle's entry point is the SAME CLI a terminal uses — ``mcp_loops.yard:main``
(the ``yard origin up`` verb, §6.2). One engine, N faces (§6.3): the CLI, the
bundle, and the GUI all funnel through it; the bundle is just that CLI frozen.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── the frozen entry point ─────────────────────────────────────────────────────

# The bundle IS the `yard` CLI frozen — `yard origin up --hub …` (§6.2). No new
# entry code; the same main() a terminal invokes. One engine, N faces (§6.3).
ENTRYPOINT_MODULE = "mcp_loops.yard"
ENTRYPOINT_FUNC = "main"

# The default artifact name (before the per-target suffix, added by BundleTarget).
BUNDLE_BASENAME = "loopyard-origin"

# Phase-B D1: the default packager is the embedded python-build-standalone
# runtime tree (``build``); the PyInstaller spec path is kept, not default.
PACKAGERS = ("pbs", "pyinstaller")
DEFAULT_PACKAGER = "pbs"

# ── the hidden-import manifest (the reason this module exists) ──────────────────

# Modules the frozen bundle needs at RUNTIME that PyInstaller's static import graph
# does NOT see — because origin_client.py imports them lazily inside methods (see
# module docstring). Every entry is verified importable by :func:`verify_manifest`,
# so this list is a checked fact, not a guess. Order is stable for a deterministic
# spec (byte-identical spec for the same target → reproducible builds).
HIDDEN_IMPORTS: tuple[str, ...] = (
    # the CLI + bring-up engine
    "mcp_loops.yard",
    "mcp_loops.origin_client",
    "mcp_loops.detach",
    # the security spine — all lazily imported inside OriginClient methods
    "mcp_loops.origin_proto",
    "mcp_loops.origin_proto.identity",
    "mcp_loops.origin_proto.tls",
    "mcp_loops.origin_proto.channel",
    "mcp_loops.origin_proto.wire",
    "mcp_loops.origin_proto.wsio",
    "mcp_loops.origin_proto.hub_tunnel",   # lazy in wsio.connect (a /hub Hub)
    "mcp_loops.origin_proto.agent_core",
    "mcp_loops.origin_proto.enrollment",
    "mcp_loops.origin_proto.control_plane",
    "mcp_loops.origin_proto.dispatch",
    # native + transport third-party deps (wheels PyInstaller must collect)
    "cryptography",
    "cryptography.hazmat.bindings._rust",  # the compiled OpenSSL/Rust binding
    "wsproto",
    "h11",
    # the worker daemon — the EXECUTION half of an origin. It cannot be reached as
    # `-m bot_squad_worker` inside a bundle (sys.executable is the exe), so it is
    # collected here and reached via the `origin run-worker` subcommand instead
    # (BUNDLE_WORKER_NOTE, closed). `.__main__` is named explicitly so the entry
    # `main()` the subcommand calls rides along even though nothing imports it
    # statically. (collect_submodules pulls the rest of the package — see the spec.)
    "bot_squad_worker",
    "bot_squad_worker.__main__",
)

# Phase-B §1.5: the PBS bundle carries the ENGINE too, so its import smoke list
# extends the hidden-import manifest with every engine entry point an agent or
# `yard start` reaches via `-m`. ``verify`` runs it INSIDE the unpacked bundle
# with ``runtime/bin/python3 -I`` — a bundle check, not a host check.
BUNDLE_SMOKE_IMPORTS: tuple[str, ...] = HIDDEN_IMPORTS + (
    "mcp_loops.server",
    "mcp_loops.report",
    "mcp_loops.poller",
    "mcp_loops.headless",
    "mcp_loops.workspaces",
    "loops_engine",
    "hub",
    "origin_wire",
    "tracking_ui.loops_dashboard",
)

# The worker daemon is a SEPARATE process the origin spawns (OriginClient.
# worker_command). Inside a frozen one-file bundle sys.executable is the bundle
# itself, so `-m bot_squad_worker` cannot resolve a module the way a real
# interpreter would. This was a build constraint; it is now RESOLVED by option (a):
# `bot_squad_worker` (+ `.__main__`) is collected into the bundle via HIDDEN_IMPORTS
# and reached through the exe's own `origin run-worker` subcommand — which
# OriginClient.worker_command() emits automatically when `sys.frozen` is set. So the
# ONE bundle carries both halves of an origin: the channel agent and the execution
# worker. BUNDLE_WORKER_ENTRY records the exact argv the frozen exe self-invokes.
BUNDLE_WORKER_ENTRY: tuple[str, ...] = ("origin", "run-worker")
BUNDLE_WORKER_NOTE = (
    "worker rides IN the bundle: `bot_squad_worker` is a collected hidden import "
    "and OriginClient.worker_command() launches it as `<exe> origin run-worker` "
    "when frozen (no `-m` module lookup) — the one-file bundle self-supervises it"
)


@dataclass(frozen=True)
class BundleTarget:
    """One cell of the per-OS/arch packaging matrix (§6.5). ``os`` is the
    PyInstaller/Python OS family; ``arch`` the machine arch; ``exe_suffix`` the
    platform's executable extension. ``pyinstaller_only`` records whether a pure
    (architecture-independent) pyz is possible — it is NOT, for any target, because
    of the native ``cryptography`` binding, which is exactly why the matrix is
    per-cell."""
    os: str          # "linux" | "windows" | "macos"
    arch: str        # "x86_64" | "aarch64" | "arm64" | "amd64"
    exe_suffix: str  # "" | ".exe"
    # Phase-B D3: windows-native is NOT a beta target (tmux has no Windows build);
    # the cell stays in the matrix for the test/CI shape but `build` refuses it.
    beta: bool = True

    @property
    def slug(self) -> str:
        """A stable, filename-safe target id, e.g. ``linux-x86_64``."""
        return f"{self.os}-{self.arch}"

    @property
    def artifact_name(self) -> str:
        """The bundle's output filename for this target, e.g.
        ``loopyard-origin-macos-arm64`` / ``loopyard-origin-windows-amd64.exe``."""
        return f"{BUNDLE_BASENAME}-{self.slug}{self.exe_suffix}"

    def artifact_for(self, packager: str = "pbs") -> str:
        """The artifact filename for ``packager``: the PBS runtime tree ships as
        ``loopyard-origin-<slug>.tar.gz`` (§1.6); the PyInstaller one-file keeps
        :attr:`artifact_name`."""
        if packager == "pyinstaller":
            return self.artifact_name
        return f"{BUNDLE_BASENAME}-{self.slug}.tar.gz"

    @property
    def detach_kind(self) -> str:
        """Which detach path the frozen origin uses on this OS (§6.2): the
        Windows process-group spawn or the POSIX ``setsid`` session leader — the
        SAME choice :func:`mcp_loops.detach.platform_detach_kind` makes at runtime,
        surfaced here so the matrix is honest about the Windows port."""
        return "windows" if self.os == "windows" else "posix"


# The supported matrix — one focused builder's target set (§8 P2.5: three bundles,
# Linux/Windows/macOS, per-arch). ARM + x86_64 for the two OSes that ship both.
SUPPORTED_TARGETS: tuple[BundleTarget, ...] = (
    BundleTarget("linux", "x86_64", ""),
    BundleTarget("linux", "aarch64", ""),
    BundleTarget("macos", "arm64", ""),
    BundleTarget("macos", "x86_64", ""),
    BundleTarget("windows", "amd64", ".exe", beta=False),
)


def target_for(slug: str) -> BundleTarget:
    """Look a target up by its :attr:`~BundleTarget.slug` (``os-arch``). Raises
    ``KeyError`` with the known set on an unknown slug — a build script fails loud,
    never silently packages the wrong matrix cell."""
    by_slug = {t.slug: t for t in SUPPORTED_TARGETS}
    try:
        return by_slug[slug]
    except KeyError:
        known = ", ".join(sorted(by_slug))
        raise KeyError(f"unknown bundle target {slug!r} — known: {known}") from None


def native_dependency(target: BundleTarget) -> dict:
    """Name the native, platform-specific artifact this target's bundle MUST carry
    (§6.5). It is the compiled ``cryptography`` OpenSSL/Rust binding — the reason no
    architecture-independent pyz exists and the matrix is per-cell. Returned as a
    fact the build/CI can assert against the wheel it fetches for the target."""
    return {
        "package": "cryptography",
        "binding_module": "cryptography.hazmat.bindings._rust",
        "reason": "Ed25519 device/Hub cert mint+parse (tls.py) — compiled OpenSSL binding",
        "portable": False,  # per-OS/arch wheel, not one pyz
        "target": target.slug,
    }


def verify_manifest(imports: tuple[str, ...] = HIDDEN_IMPORTS) -> dict:
    """Import every module in the hidden-import manifest ON THIS HOST and report
    which resolve. This is the deterministic proof that the bundle's manifest is a
    fact: if the frozen bundle names these hidden-imports and they all import from
    source here, they are real modules PyInstaller can collect — not a wishlist. A
    module that fails to import is returned in ``missing`` so a build never ships a
    manifest that would ImportError inside the frozen origin. Returns
    ``{ok, present:[...], missing:[{module, error}]}``."""
    present: list[str] = []
    missing: list[dict] = []
    with _source_worker_on_path():
        for name in imports:
            try:
                importlib.import_module(name)
                present.append(name)
            except Exception as e:  # noqa: BLE001 — a missing dep is a reportable fact
                missing.append({"module": name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": not missing, "present": present, "missing": missing}


@contextlib.contextmanager
def _source_worker_on_path():
    """In a source checkout ``bot_squad_worker`` lives at ``<repo>/worker/`` (the
    same ``worker/bot_squad_worker/`` ship root the bundle carries), which is not
    a top-level package on ``sys.path``. Unless the host already resolves it (an
    installed/editable worker), put ``<repo>/worker`` on the path for the manifest
    imports only. Without this the check passed only when another module happened
    to insert that path first: the windows unit subset (which never collects
    test_decouple.py) reported it unimportable (B-0c)."""
    worker_root = str(Path(__file__).resolve().parents[1] / "worker")
    added = (importlib.util.find_spec("bot_squad_worker") is None
             and Path(worker_root, "bot_squad_worker", "__init__.py").is_file()
             and worker_root not in sys.path)
    if added:
        sys.path.insert(0, worker_root)
    try:
        yield
    finally:
        if added and worker_root in sys.path:
            sys.path.remove(worker_root)


def generate_pyinstaller_spec(target: BundleTarget) -> str:
    """Emit a valid PyInstaller ``.spec`` (real Python that ``pyinstaller`` runs)
    for ``target``. Deterministic: the same target yields a byte-identical spec, so
    a build is reproducible and reviewable in a diff. The spec freezes the SAME
    ``yard`` CLI entry (§6.2), forces every :data:`HIDDEN_IMPORTS` module in
    (defeating the lazy-import gap), and collects ``cryptography``'s native binding
    via ``collect_dynamic_libs`` so the compiled OpenSSL artifact rides along. It
    builds a one-file, console bundle named for the target."""
    hidden = ",\n        ".join(repr(m) for m in HIDDEN_IMPORTS)
    exe_name = target.artifact_name
    # A console app (the CLI needs stdio for `--dry-run`/status output). onefile via
    # EXE(..., a.binaries, a.datas, ...) with no COLLECT (the classic one-file form).
    return f'''# -*- mode: python ; coding: utf-8 -*-
# GENERATED by mcp_loops.origin_bundle for target {target.slug} — do not edit by
# hand; regenerate with `python -m mcp_loops.origin_bundle --target {target.slug}
# --emit-spec`. Freezes the `yard` CLI (§6.2) as a self-contained origin bundle.
#
# Native artifact carried (per-OS/arch, §6.5): {native_dependency(target)["binding_module"]}
# Detach path on this target (§6.2): {target.detach_kind}
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# The compiled cryptography OpenSSL/Rust binding is native + platform-specific —
# collect its shared libs so the frozen origin can mint/parse Ed25519 certs (tls.py).
_crypto_bins = collect_dynamic_libs('cryptography')
# origin_proto is imported lazily inside OriginClient methods; collect the package
# wholesale so no submodule is dropped by static analysis.
_spine = collect_submodules('mcp_loops.origin_proto')
# The worker (execution half of an origin) is reached via `origin run-worker`, not
# a static import, so collect its whole package too — else the frozen exe would
# have the subcommand but not the modules it needs to actually run (§6.5).
_worker = collect_submodules('bot_squad_worker')

a = Analysis(
    ['{_entry_shim_name()}'],
    pathex=[],
    binaries=_crypto_bins,
    datas=[],
    hiddenimports=[
        {hidden},
    ] + _spine + _worker,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name={exe_name!r},
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True,          # the CLI writes to stdout/stderr (dry-run, status)
    disable_windowed_traceback=False,
    target_arch={target.arch!r},
    codesign_identity=None, entitlements_file=None,
)
'''


def _entry_shim_name() -> str:
    """The tiny top-level script PyInstaller freezes as the program entry. It just
    calls ``mcp_loops.yard.main`` — PyInstaller wants a script path, not a
    ``module:func``, so :func:`generate_entry_shim` writes this at build time."""
    return f"_{BUNDLE_BASENAME.replace('-', '_')}_entry.py"


def generate_entry_shim() -> str:
    """The one-line entry script the spec freezes (PyInstaller takes a script path,
    not ``module:func``). It defers to the SAME ``yard`` CLI ``main`` the terminal
    uses — no second entry code path (§6.3)."""
    return (
        "import sys\n"
        f"from {ENTRYPOINT_MODULE} import {ENTRYPOINT_FUNC} as _main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(_main(sys.argv[1:]))\n"
    )


def plan(target: BundleTarget, *, verify: bool = True,
         packager: str = DEFAULT_PACKAGER) -> dict:
    """The full, reviewable build plan for one matrix cell — what the bundle is,
    what it carries, and (when ``verify``) deterministic proof the manifest is real
    on this host. This is what a build script or ``yard origin bundle`` renders."""
    out = {
        "target": target.slug,
        "os": target.os,
        "arch": target.arch,
        "artifact": target.artifact_for(packager),
        "entrypoint": f"{ENTRYPOINT_MODULE}:{ENTRYPOINT_FUNC}",
        "detachKind": target.detach_kind,
        "nativeDependency": native_dependency(target),
        "hiddenImports": list(HIDDEN_IMPORTS),
        "workerNote": BUNDLE_WORKER_NOTE,
        # The self-invoked argv the frozen exe uses to BE its own worker (§6.5) —
        # `<artifact> origin run-worker`. Proof the execution half ships in-bundle.
        "workerEntry": [target.artifact_name, *BUNDLE_WORKER_ENTRY],
        "packager": packager,
        "beta": target.beta,
    }
    if verify:
        out["manifest"] = verify_manifest()
    return out


def matrix_plan(*, verify: bool = True) -> dict:
    """The plan across the whole supported matrix (§8 P2.5). ``verify`` runs the
    manifest check once (host-local, target-independent) and shares it."""
    manifest = verify_manifest() if verify else None
    cells = []
    for t in SUPPORTED_TARGETS:
        cell = plan(t, verify=False)
        if manifest is not None:
            cell["manifest"] = manifest
        cells.append(cell)
    return {"targets": cells, "count": len(cells),
            "manifestOk": None if manifest is None else manifest["ok"]}


# ── Phase-B B1: the PBS runtime-tree builder (PHASE-B-SPEC §1.3, §1.5, §1.6) ────
#
# `python -m mcp_loops.origin_bundle build --target <slug> --out dist/` assembles
# a relocatable directory tree — a pinned python-build-standalone CPython, the
# target's hash-locked wheels, and the Loopyard source (+ unchecked-hash pycs) —
# and packs it into a byte-reproducible `loopyard-origin-<slug>.tar.gz`. Every
# target is CROSS-ASSEMBLED on the build host (no compilation happens); it is
# only EXECUTED on a native runner (§1.7). `uv` is a build-host-only tool.

PBS_PYTHON_MINOR = "3.13"
# The bundle's root dir name inside the tarball, and the pyc co_filename prefix
# (compileall -p, P12) — tracebacks still show real paths (the import system
# rewrites co_filename on load).
BUNDLE_ROOT_NAME = "loopyard"
PYC_PREFIX = "/loopyard"

# slug -> uv --python-platform triple (§1.6 step 2 table)
UV_PLATFORMS: dict[str, str] = {
    "linux-x86_64": "x86_64-manylinux_2_28",
    "linux-aarch64": "aarch64-manylinux_2_28",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
}
# slug -> (os family, wheel-tag arch, min-OS floor) — the ceiling every staged
# wheel tag must sit at or under (B1(d)). linux floor = glibc; macos = macOS.
MIN_OS: dict[str, tuple[str, str, tuple[int, int]]] = {
    "linux-x86_64": ("linux", "x86_64", (2, 28)),
    "linux-aarch64": ("linux", "aarch64", (2, 28)),
    "macos-arm64": ("macos", "arm64", (11, 0)),
    "macos-x86_64": ("macos", "x86_64", (10, 15)),
}
MACOSX_DEPLOYMENT_TARGET: dict[str, str] = {
    "macos-arm64": "11.0",
    "macos-x86_64": "10.15",
}

# What ships (§1.5): these repo roots, minus every tests/ dir, pycaches, the
# chat-ui Vite sources (the dashboard ships prebuilt static assets; Node is not a
# runtime dep) and the coordinator-only worker modules below. `bin/yard` is
# replaced by the portable sh launcher.
SHIP_ROOTS: tuple[str, ...] = (
    "mcp_loops/", "loops_engine/", "hub/", "origin_wire/", "tracking_ui/",
    "control_plane/",
    "bin/", "worker/bot_squad_worker/",
)
SHIP_EXCLUDE_PREFIXES: tuple[str, ...] = ("tracking_ui/chat-ui/",)
# Coordinator-only; `user-worker` never imports them after R23/R30 (§1.5).
# Globs are relative to worker/bot_squad_worker/.
WORKER_EXCLUDE_GLOBS: tuple[str, ...] = (
    "tg_*.py", "icloud_calendar.py", "sleep.py", "swarm_*.py", "autonomous.py",
    "refresh_oauth.py", "xray_resolve.py", "deploy.py", "autoupdate*.py",
    "core/librarian.py", "core/context_processor.py",
)

LOOPYARD_PTH = "../../../..\n../../../../worker\n"
# §1.6 step 3, exact body: portable symlink resolution (no readlink -f), -I (D13).
BIN_YARD = """#!/bin/sh
# resolve symlinks portably (no readlink -f), then cd -P for the physical dir
p=$0; while [ -h "$p" ]; do l=$(ls -ld -- "$p"); t=${l#*' -> '}; case $t in /*) p=$t;; *) p=$(dirname -- "$p")/$t;; esac; done
ROOT=$(cd -P -- "$(dirname -- "$p")/.." && pwd)
exec "$ROOT/runtime/bin/python3" -I -m mcp_loops.yard "$@"
"""

WINDOWS_REFUSAL = ("windows-native is not a beta target (the worker substrate is "
                   "tmux): use the linux bundle under WSL2")


class BundleBuildError(RuntimeError):
    """A build step failed — the builder never ships a partial tree."""


def _repo_root():
    from mcp_loops import paths
    return paths.install_root()


def bundle_dir():
    """``<repo>/bundle`` — pbs.lock, requirements-bundle.in, locks/."""
    return _repo_root() / "bundle"


def load_pbs_lock(path=None) -> dict:
    import tomllib
    p = path or (bundle_dir() / "pbs.lock")
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def pbs_asset_name(lock: dict, slug: str) -> str:
    triple = lock["targets"][slug]["triple"]
    return (f"cpython-{lock['python']}+{lock['release']}-{triple}-"
            f"{lock['flavor']}.tar.gz")


def host_slug() -> str:
    """The beta slug of THIS machine (the one whose runtime can execute here)."""
    import platform
    system, machine = platform.system(), platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "linux-aarch64"
    if system == "Darwin":
        return "macos-arm64" if machine == "arm64" else "macos-x86_64"
    raise BundleBuildError(f"no pinned PBS runtime for build host {system}/{machine}")


def is_shipped(rel: str) -> bool:
    """Is repo path ``rel`` (posix) part of the bundle's code tree (§1.5)?"""
    import fnmatch
    if not rel.startswith(SHIP_ROOTS) or rel.startswith(SHIP_EXCLUDE_PREFIXES):
        return False
    parts = rel.split("/")
    if "tests" in parts or "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
        return False
    wprefix = "worker/bot_squad_worker/"
    if rel.startswith(wprefix):
        sub = rel[len(wprefix):]
        if any(fnmatch.fnmatchcase(sub, g) for g in WORKER_EXCLUDE_GLOBS):
            return False
    return True


def excluded_worker_modules() -> tuple[str, ...]:
    """The exclusion globs as dotted module patterns (for the import trace)."""
    return tuple("bot_squad_worker." + g[:-3].replace("/", ".")
                 for g in WORKER_EXCLUDE_GLOBS)


# ── wheel-tag normalization (§1.6 step 2, B1(d)) ───────────────────────────────

_LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12),
                     "manylinux2014": (2, 17)}


def normalize_platform_tag(tag: str) -> list[tuple[str, str, tuple[int, int]]] | None:
    """Map ONE platform tag to ``[(os, arch, floor), …]``. ``None`` = ``any``
    (pure python, always passes). ``musllinux`` maps to os ``musllinux`` (never a
    glibc target, so it fails). Raises ``ValueError`` on an unknown tag."""
    import re
    if tag == "any":
        return None
    for legacy, floor in _LEGACY_MANYLINUX.items():
        if tag.startswith(legacy + "_"):
            return [("linux", tag[len(legacy) + 1:], floor)]
    m = re.fullmatch(r"manylinux_(\d+)_(\d+)_(\w+)", tag)
    if m:
        return [("linux", m.group(3), (int(m.group(1)), int(m.group(2))))]
    m = re.fullmatch(r"musllinux_(\d+)_(\d+)_(\w+)", tag)
    if m:
        return [("musllinux", m.group(3), (int(m.group(1)), int(m.group(2))))]
    m = re.fullmatch(r"macosx_(\d+)_(\d+)_(\w+)", tag)
    if m:
        floor = (int(m.group(1)), int(m.group(2)))
        arch = m.group(3)
        if arch == "universal2":
            return [("macos", "x86_64", floor), ("macos", "arm64", floor)]
        if arch == "intel" or arch.startswith("fat"):
            return [("macos", "x86_64", floor)]
        return [("macos", arch, floor)]
    raise ValueError(f"unrecognised wheel platform tag {tag!r}")


def platform_tag_ok(tag: str, slug: str) -> bool:
    """Does platform tag ``tag`` run on target ``slug`` (arch match, floor ≤ the
    target's min OS)? A wrong-arch tag is a failure, never a skip."""
    want_os, want_arch, ceiling = MIN_OS[slug]
    try:
        cells = normalize_platform_tag(tag)
    except ValueError:
        return False
    if cells is None:
        return True
    return any(os_ == want_os and arch == want_arch and floor <= ceiling
               for os_, arch, floor in cells)


def wheel_tags_ok(tag_lines: list[str], slug: str) -> bool:
    """A wheel passes if ANY of its ``Tag:`` lines has ANY compressed platform
    component (``a.b`` → ``a``, ``b``) that passes :func:`platform_tag_ok`."""
    for line in tag_lines:
        plat = line.strip().split("-")[-1]
        if any(platform_tag_ok(p, slug) for p in plat.split(".")):
            return True
    return False


def check_staged_wheels(site_packages, slug: str) -> dict:
    """B1(d): parse every ``*.dist-info/WHEEL`` under ``site_packages``; report
    ``{ok, checked, failures:[{dist, tags}]}``."""
    from pathlib import Path
    failures, checked = [], 0
    for wheel in sorted(Path(site_packages).glob("*.dist-info/WHEEL")):
        tags = [ln.split(":", 1)[1].strip()
                for ln in wheel.read_text().splitlines() if ln.startswith("Tag:")]
        checked += 1
        if not wheel_tags_ok(tags, slug):
            failures.append({"dist": wheel.parent.name, "tags": tags})
    return {"ok": checked > 0 and not failures, "checked": checked,
            "failures": failures}


# ── reproducible tarball (§1.6 step 4) ─────────────────────────────────────────

def _norm_mode(st_mode: int, is_dir: bool) -> int:
    return 0o755 if is_dir or (st_mode & 0o111) else 0o644


def tar_entries(stage) -> list[str]:
    """Every path under ``stage`` (relative, posix), sorted — the tar order."""
    import os
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(stage):
        rel_dir = os.path.relpath(dirpath, stage)
        for name in dirnames + filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def write_reproducible_tarball(stage, out_path, epoch: int) -> None:
    """Pack ``stage`` as ``loopyard/<rel>`` entries: sorted, mtime=epoch,
    uid/gid 0, empty uname/gname, normalized modes, PAX, gzip mtime 0 + no name."""
    import gzip
    import os
    import stat as st_
    import tarfile
    from pathlib import Path
    stage = Path(stage)

    def info(name: str, full: Path | None) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(name)
        ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = epoch, 0, 0, "", ""
        if full is None:
            ti.type, ti.mode = tarfile.DIRTYPE, 0o755
            return ti
        st = os.lstat(full)
        if st_.S_ISLNK(st.st_mode):
            target = os.readlink(full)
            if os.path.isabs(target):
                raise BundleBuildError(f"absolute symlink in stage: {name} -> {target}")
            ti.type, ti.linkname, ti.mode = tarfile.SYMTYPE, target, 0o777
        elif st_.S_ISDIR(st.st_mode):
            ti.type, ti.mode = tarfile.DIRTYPE, 0o755
        elif st_.S_ISREG(st.st_mode):
            ti.type, ti.size = tarfile.REGTYPE, st.st_size
            ti.mode = _norm_mode(st.st_mode, False)
        else:
            raise BundleBuildError(f"unsupported file type in stage: {name}")
        return ti

    with open(out_path, "wb") as fh, \
            gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="",
                          compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
        tf.addfile(info(BUNDLE_ROOT_NAME, None))
        for rel in tar_entries(stage):
            full = stage / rel
            ti = info(f"{BUNDLE_ROOT_NAME}/{rel}", full)
            if ti.type == tarfile.REGTYPE:
                with open(full, "rb") as src:
                    tf.addfile(ti, src)
            else:
                tf.addfile(ti)


def _sha256_file(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── release signing (loopyard-bug-1790562150) ─────────────────────────────────
# A same-host .sha256 proves integrity, not provenance. Each artifact also gets a
# detached Ed25519 SSH signature, ``<artifact>.sig`` (``ssh-keygen -Y sign``),
# which install.sh verifies with ``ssh-keygen -Y verify`` against the public key
# pinned in install.sh. The private key never lives in the repo: the build reads
# its PATH from $LOOPYARD_RELEASE_SIGNING_KEY / --signing-key (an unencrypted
# OpenSSH ed25519 private key, or its .pub with the private half in ssh-agent).
# Release process: docs/ops/RELEASE-SIGNING.md.

SIG_NAMESPACE = "loopyard-release"          # == install.sh SIG_NAMESPACE
SIGNING_KEY_ENV = "LOOPYARD_RELEASE_SIGNING_KEY"


def _ssh_keygen() -> str:
    import shutil
    exe = shutil.which("ssh-keygen")
    if not exe:
        raise BundleBuildError("ssh-keygen (OpenSSH >= 8.1) is required to sign the bundle")
    return exe


def signing_public_key(key_path) -> str:
    """The ``ssh-ed25519 AAAA…`` public line for ``key_path`` (a .pub file or an
    unencrypted private key). Anything but Ed25519 is refused."""
    import subprocess
    from pathlib import Path
    key_path = Path(key_path)
    if not key_path.is_file():
        raise BundleBuildError(f"signing key not found: {key_path}")
    head = key_path.read_bytes()[:64]
    if head.startswith(b"ssh-"):
        line = key_path.read_text().split("\n", 1)[0]
    else:
        r = subprocess.run([_ssh_keygen(), "-y", "-f", str(key_path)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           start_new_session=True)   # no tty: an encrypted key fails, never prompts
        if r.returncode != 0:
            raise BundleBuildError(f"cannot read signing key {key_path} "
                                   f"(encrypted? use its .pub + ssh-agent): {r.stderr.strip()}")
        line = r.stdout.strip()
    parts = line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise BundleBuildError(f"signing key {key_path} is not an ssh-ed25519 key")
    # Validate the SSH wire encoding before embedding this literal in install.sh.
    # A type label alone does not make an untrusted .pub payload a public key.
    import base64
    import binascii
    prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        decoded = b""
    if not decoded.startswith(prefix) or len(decoded) != len(prefix) + 32:
        raise BundleBuildError(f"invalid Ed25519 public key: {key_path}")
    return f"{parts[0]} {parts[1]}"


def verify_artifact(artifact, public_key: str, sig=None) -> bool:
    """True iff ``sig`` (default ``<artifact>.sig``) is a valid ``SIG_NAMESPACE``
    signature of ``artifact`` by ``public_key`` — the exact check install.sh runs."""
    import subprocess
    import tempfile
    from pathlib import Path
    artifact = Path(artifact)
    sig = Path(sig) if sig else artifact.with_name(artifact.name + ".sig")
    if not sig.is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="loopyard-sigcheck-") as td:
        allowed = Path(td) / "allowed_signers"
        allowed.write_text(f'{SIG_NAMESPACE} namespaces="{SIG_NAMESPACE}" {public_key}\n')
        with open(artifact, "rb") as fh:
            r = subprocess.run([_ssh_keygen(), "-Y", "verify", "-f", str(allowed),
                                "-I", SIG_NAMESPACE, "-n", SIG_NAMESPACE, "-s", str(sig)],
                               stdin=fh, capture_output=True)
    return r.returncode == 0


def sign_artifact(artifact, key_path) -> dict:
    """Write ``<artifact>.sig`` with ``key_path`` and self-verify it."""
    import subprocess
    from pathlib import Path
    artifact = Path(artifact)
    pub = signing_public_key(key_path)
    sig = artifact.with_name(artifact.name + ".sig")
    import tempfile
    # Sign stdin into a sibling temporary file; preserve any published signature
    # until the replacement has passed the same verification as the installer.
    with tempfile.TemporaryDirectory(prefix=".sign-", dir=artifact.parent) as td:
        candidate = Path(td) / "signature"
        with artifact.open("rb") as payload, candidate.open("wb") as output:
            r = subprocess.run([_ssh_keygen(), "-Y", "sign", "-q", "-f", str(key_path),
                                "-n", SIG_NAMESPACE],
                               stdin=payload, stdout=output, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
        if r.returncode != 0:
            raise BundleBuildError(f"signing {artifact.name} failed: {r.stderr.strip()[-1000:]}")
        if not verify_artifact(artifact, pub, candidate):
            raise BundleBuildError(f"{sig.name} does not verify against {key_path}")
        candidate.replace(sig)
    return {"signature": str(sig), "publicKey": pub, "namespace": SIG_NAMESPACE}


# ── build steps ────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    import sys
    print(f"[origin_bundle] {msg}", file=sys.stderr, flush=True)


def _run(argv, **kw):
    import subprocess
    r = subprocess.run(argv, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise BundleBuildError(
            f"command failed ({r.returncode}): {' '.join(map(str, argv))}\n"
            f"{(r.stdout or '')[-2000:]}{(r.stderr or '')[-4000:]}")
    return r


def _clean_env(extra: Optional[dict] = None) -> dict:
    """The host env minus every PYTHON*/VIRTUAL_ENV/UV_* knob, so no host path
    leaks into a build step."""
    import os
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTHON", "UV_", "VIRTUAL_ENV", "MACOSX_DEPLOYMENT"))}
    env.update(extra or {})
    return env


def cache_dir():
    import os
    from pathlib import Path
    env = os.environ.get("LOOPYARD_BUNDLE_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "loopyard-bundle"


def fetch_pbs(lock: dict, slug: str):
    """Step 1: the pinned PBS tarball for ``slug`` (cached), sha256-verified."""
    import urllib.request
    from urllib.parse import quote
    name = pbs_asset_name(lock, slug)
    want = lock["targets"][slug]["sha256"]
    dest = cache_dir() / "pbs" / name
    if not dest.exists() or _sha256_file(dest) != want:
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{lock['url_base']}/{lock['release']}/{quote(name)}"
        _log(f"fetch {url}")
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    got = _sha256_file(dest)
    if got != want:
        dest.unlink(missing_ok=True)
        raise BundleBuildError(f"PBS sha256 mismatch for {name}: {got} != {want}")
    return dest


def unpack_pbs(tarball, runtime_dir) -> None:
    """Unpack a PBS ``install_only*`` tarball (top dir ``python/``) as
    ``runtime_dir``."""
    import tarfile
    runtime_dir.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            if m.name == "python":
                continue
            if not m.name.startswith("python/"):
                raise BundleBuildError(f"unexpected PBS entry {m.name!r}")
            m.name = m.name[len("python/"):]
            members.append(m)
        tf.extractall(runtime_dir, members=members, filter="tar")


def site_packages(runtime_dir):
    return runtime_dir / "lib" / f"python{PBS_PYTHON_MINOR}" / "site-packages"


def _uv() -> str:
    import os
    import shutil
    uv = os.environ.get("LOOPYARD_UV") or shutil.which("uv") or \
        str(__import__("pathlib").Path.home() / ".local" / "bin" / "uv")
    if not os.path.exists(uv):
        raise BundleBuildError("uv not found (build-host tool; pinned in bundle/pbs.lock)")
    return uv


def install_wheels(slug: str, site: "object", host_python, lock_file) -> None:
    """Step 2: target wheels from the per-target hash lock, binary-only.
    ``uv --target`` also writes console scripts whose shebang is the build
    interpreter's absolute path; the bundle only ever runs ``python -m``, so the
    scripts dir is dropped and its RECORD lines with it (path independence)."""
    import shutil
    env = _clean_env({"MACOSX_DEPLOYMENT_TARGET": MACOSX_DEPLOYMENT_TARGET[slug]}
                     if slug in MACOSX_DEPLOYMENT_TARGET else None)
    _run([_uv(), "pip", "install", "--no-config", "--target", str(site),
          "--python", str(host_python), "--python-version", PBS_PYTHON_MINOR,
          "--python-platform", UV_PLATFORMS[slug], "--only-binary", ":all:",
          "--require-hashes", "--link-mode", "copy", "--no-progress",
          "-r", str(lock_file)], env=env)
    scripts = site / "bin"
    if scripts.is_dir():
        shutil.rmtree(scripts)
    for record in site.glob("*.dist-info/RECORD"):
        lines = record.read_text().splitlines(keepends=True)
        kept = [ln for ln in lines if not ln.startswith(("bin/", "../../bin/"))]
        if kept != lines:
            record.write_text("".join(kept))


def git_head(repo) -> tuple[str, int]:
    r = _run(["git", "-C", str(repo), "log", "-1", "--format=%H %ct", "HEAD"])
    sha, ct = r.stdout.split()
    return sha, int(ct)


_VERSION_RE = r'^__version__\s*=\s*"([^"]+)"'
_STATE_VERSION_RE = r'^STATE_VERSION\s*=\s*(\d+)'


def head_versions(repo) -> tuple[str, int]:
    """``(__version__, STATE_VERSION)`` from ``mcp_loops/_version.py`` AT HEAD —
    the commit being built, not the working tree nor the importing process."""
    import re
    src = _run(["git", "-C", str(repo), "show", "HEAD:mcp_loops/_version.py"]).stdout
    v = re.search(_VERSION_RE, src, re.M)
    sv = re.search(_STATE_VERSION_RE, src, re.M)
    if not v or not sv:
        raise BundleBuildError("mcp_loops/_version.py at HEAD has no __version__/STATE_VERSION")
    return v.group(1), int(sv.group(1))


def default_version(base: str, sha: str) -> str:
    """R2: an untagged build is ``<__version__>+g<sha12>`` (``0.1.0-dev+g…``),
    so a dev build never compares equal to a release."""
    return f"{base}+g{sha[:12]}"


def stage_source(repo, stage, extra_files: tuple[str, ...] = ()) -> int:
    """Step 3a: ``git archive HEAD`` the shipped set into ``stage`` (never the
    working tree — a build is a function of the commit). Returns the file count."""
    import io
    import subprocess
    import tarfile
    roots = sorted({r.rstrip("/") for r in SHIP_ROOTS})
    blob = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                           "HEAD", "--", *roots, *extra_files],
                          capture_output=True, check=True).stdout
    count = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tf:
        for m in tf.getmembers():
            rel = m.name.rstrip("/")
            if not (m.isfile() or m.issym()):
                continue
            if rel not in extra_files and not is_shipped(rel):
                continue
            dest = stage / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if m.issym():
                dest.symlink_to(m.linkname)
            else:
                dest.write_bytes(tf.extractfile(m).read())
                dest.chmod(0o755 if m.mode & 0o111 else 0o644)
            count += 1
    return count


# F7-big / LAUNCH-CHECKLIST 1.11: the built web app (frontend/apps/web, `vite
# build` with base /app/) rides in the bundle at the path the bundled dashboard
# serves /app/ from when the repo path is absent (mcp_loops.paths.web_dist ->
# <install_root>/web, B-3). Node is a BUILD-time dep only: the release builds
# the SPA at the release commit and hands its dist/ to `build --web-dist`.
WEB_DIST_REL = "web"
WEB_DIST_BASE_MARK = "/app/assets/"      # index.html of a `vite build` (base /app/)


def stage_web_dist(web_dist, stage) -> dict:
    """Copy a built SPA ``dist/`` into ``stage/web`` (B-3); returns
    ``{files, sha256}`` (a digest over sorted rel paths + bytes). Refuses a dist
    with no index.html, one not built for the /app/ base (a dev build would 404
    every asset), or any non-regular file (symlink, device)."""
    import hashlib
    import os
    import shutil
    import stat as st_
    from pathlib import Path
    src = Path(web_dist).resolve()
    index = src / "index.html"
    if not index.is_file():
        raise BundleBuildError(f"--web-dist {src}: no index.html (run the web build first)")
    if WEB_DIST_BASE_MARK not in index.read_text(encoding="utf-8", errors="replace"):
        raise BundleBuildError(f"--web-dist {src}: index.html is not a /app/-based build "
                               f"(expected {WEB_DIST_BASE_MARK} asset URLs)")
    dest = Path(stage) / WEB_DIST_REL
    h = hashlib.sha256()
    files = 0
    for full in sorted(src.rglob("*")):
        mode = os.lstat(full).st_mode
        if st_.S_ISDIR(mode):
            continue
        if not st_.S_ISREG(mode):
            raise BundleBuildError(f"--web-dist: not a regular file: {full}")
        rel = full.relative_to(src).as_posix()
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(full, dest / rel)
        (dest / rel).chmod(0o644)
        h.update(rel.encode() + b"\0" + hashlib.sha256(full.read_bytes()).digest())
        files += 1
    return {"files": files, "sha256": h.hexdigest()}


def compile_stage(python, stage, exclude: str = "") -> None:
    """Step 3c (P7/P12), exactly:
    ``python -B -m compileall -f -q -j0 --invalidation-mode unchecked-hash
    -s <STAGE> -p /loopyard <STAGE>`` — over the whole stage incl. runtime/lib."""
    argv = [str(python), "-B", "-m", "compileall", "-f", "-q", "-j0",
            "--invalidation-mode", "unchecked-hash", "-s", str(stage),
            "-p", PYC_PREFIX]
    if exclude:
        argv += ["-x", exclude]
    _run(argv + [str(stage)], env=_clean_env())


def _iso_utc(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def build(slug: str, out_dir, *, version: Optional[str] = None,
          extra_files: tuple[str, ...] = (), repo=None, keep_stage: bool = False,
          signing_key=None, web_dist=None, release_url: Optional[str] = None) -> dict:
    """Build ``loopyard-origin-<slug>.tar.gz`` (+ ``.sha256``, + ``.sig`` when a
    signing key is given or $LOOPYARD_RELEASE_SIGNING_KEY is set) into ``out_dir``
    (§1.6). Deterministic in (commit, locks, pbs.lock): two builds of the same
    HEAD — any time, any ``--out``, any stage path — are byte-identical.
    ``web_dist`` = a built frontend/apps/web dist/ to serve at /app/ (1.11);
    without it the bundle ships the legacy dashboard only (logged NO WEB APP).
    ``release_url`` = the release host ``yard update`` points at (B-2), written
    as ``BUNDLE.json.releaseUrl`` (null when not given)."""
    import json
    import os
    import shutil
    import tempfile
    from pathlib import Path

    target = target_for(slug)
    if not target.beta:
        raise BundleBuildError(WINDOWS_REFUSAL)
    signing_key = signing_key or os.environ.get(SIGNING_KEY_ENV) or None
    if signing_key:
        signing_public_key(signing_key)         # fail before the slow build, not after
    repo = Path(repo or _repo_root())
    lock = load_pbs_lock(repo / "bundle" / "pbs.lock")
    lock_file = repo / "bundle" / "locks" / f"{slug}.lock"
    if not lock_file.exists():
        raise BundleBuildError(f"missing {lock_file} — run `origin_bundle lock`")
    sha, epoch = git_head(repo)
    code_version, state_version = head_versions(repo)
    version = version or default_version(code_version, sha)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stage = Path(tempfile.mkdtemp(prefix="loopyard-stage-")).resolve()
    helper = None
    _log(f"stage={stage}")
    try:
        runtime = stage / "runtime"
        unpack_pbs(fetch_pbs(lock, slug), runtime)
        native = slug == host_slug()
        if native:
            host_python = runtime / "bin" / "python3"
        else:
            # Cross-assembly: the target runtime cannot execute here. Resolve
            # and compile with the SAME pinned CPython release built for the
            # host — bytecode is platform-independent within one CPython version.
            helper = Path(tempfile.mkdtemp(prefix="loopyard-hostpy-")).resolve()
            unpack_pbs(fetch_pbs(lock, host_slug()), helper / "runtime")
            host_python = helper / "runtime" / "bin" / "python3"
        site = site_packages(runtime)
        install_wheels(slug, site, host_python, lock_file)
        wheels = check_staged_wheels(site, slug)
        if not wheels["ok"]:
            raise BundleBuildError(f"wheel tags exceed {slug} min-OS: {wheels['failures']}")
        n_src = stage_source(repo, stage, tuple(extra_files))
        web = stage_web_dist(web_dist, stage) if web_dist else None
        if web is None:
            _log("NO WEB APP: no --web-dist — the bundle serves the legacy dashboard at / only")
        (site / "loopyard.pth").write_text(LOOPYARD_PTH)
        (stage / "bin").mkdir(exist_ok=True)
        (stage / "bin" / "yard").write_text(BIN_YARD)
        (stage / "bin" / "yard").chmod(0o755)
        meta = {
            "target": slug, "version": version, "gitSha": sha,
            "pbsRelease": lock["release"], "pbsFlavor": lock["flavor"],
            "python": lock["python"], "lockSha256": _sha256_file(lock_file),
            "builtAt": _iso_utc(epoch), "webApp": web,
            "webSha256": web["sha256"] if web else None,
            "releaseUrl": release_url.rstrip("/") if release_url else None,
            "stateVersion": state_version,
        }
        (stage / "BUNDLE.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        compile_stage(host_python, stage, lock.get("compileall_exclude", ""))
        artifact = out_dir / target.artifact_for("pbs")
        write_reproducible_tarball(stage, artifact, epoch)
        digest = _sha256_file(artifact)
        (out_dir / (artifact.name + ".sha256")).write_text(f"{digest}  {artifact.name}\n")
        signed = sign_artifact(artifact, signing_key) if signing_key else None
        if not signed:
            artifact.with_name(artifact.name + ".sig").unlink(missing_ok=True)
            _log(f"UNSIGNED: no {SIGNING_KEY_ENV} — install.sh with a pinned key will refuse this")
        return {"ok": True, "artifact": str(artifact), "sha256": digest, "signed": signed,
                "size": artifact.stat().st_size, "stage": str(stage),
                "native": native, "sourceFiles": n_src, "wheels": wheels,
                "bundle": meta}
    finally:
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)
        if helper is not None:
            shutil.rmtree(helper, ignore_errors=True)


def lock(slugs: Optional[list[str]] = None, *, check: bool = False, repo=None) -> dict:
    """(Re)generate ``bundle/locks/<slug>.lock`` from ``requirements-bundle.in``
    with ``uv pip compile --generate-hashes`` (§1.5). ``check`` compiles into a
    temp copy (existing pins as preferences) and reports any diff instead of
    writing — the CI ``locks`` job / B1(d2)."""
    import shutil
    import tempfile
    from pathlib import Path
    repo = Path(repo or _repo_root())
    bdir = repo / "bundle"
    result: dict = {"ok": True, "targets": {}}
    for slug in slugs or list(UV_PLATFORMS):
        dest = bdir / "locks" / f"{slug}.lock"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / dest.name
            if dest.exists():
                shutil.copyfile(dest, out)
            env = _clean_env({"MACOSX_DEPLOYMENT_TARGET": MACOSX_DEPLOYMENT_TARGET[slug]}
                             if slug in MACOSX_DEPLOYMENT_TARGET else None)
            _run([_uv(), "pip", "compile", "--no-config", "-q", "requirements-bundle.in",
                  "--python-version", PBS_PYTHON_MINOR,
                  "--python-platform", UV_PLATFORMS[slug], "--generate-hashes",
                  "--custom-compile-command", "python -m mcp_loops.origin_bundle lock",
                  "-o", str(out)], cwd=str(bdir), env=env)
            new = out.read_text()
        old = dest.read_text() if dest.exists() else None
        same = old == new
        result["targets"][slug] = {"lock": str(dest), "unchanged": same}
        if check:
            result["ok"] = result["ok"] and same
        elif not same:
            dest.write_text(new)
    return result


def import_trace(python, root, *, modules: tuple[str, ...] = (
        "bot_squad_worker.__main__", "bot_squad_worker.sessions",
        "bot_squad_worker.server", "bot_squad_worker.actions")) -> dict:
    """B1(e): in the bundled tree, ``-X importtime`` the user-worker boot imports
    (``__main__`` + the spawn_session path) and report every loaded module that
    matches an exclusion glob. The trace is the authority (§1.5)."""
    import fnmatch
    import subprocess
    code = "import " + ", ".join(modules)
    env = _clean_env({"BOT_SQUAD_MODE": "user-worker"})
    r = subprocess.run([str(python), "-I", "-X", "importtime", "-c", code],
                       capture_output=True, text=True, cwd=str(root), env=env)
    loaded = []
    for ln in r.stderr.splitlines():
        if ln.startswith("import time:") and "|" in ln:
            name = ln.rsplit("|", 1)[1].strip()
            if name and name != "package":
                loaded.append(name)
    pats = excluded_worker_modules()
    hits = sorted({m for m in loaded if any(fnmatch.fnmatchcase(m, p) for p in pats)})
    return {"ok": r.returncode == 0 and not hits, "returncode": r.returncode,
            "loaded": len(loaded), "excludedLoaded": hits,
            "stderrTail": "" if r.returncode == 0 else r.stderr[-2000:]}


def _cmd_build(argv: list[str]) -> int:
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(prog="mcp_loops.origin_bundle build")
    ap.add_argument("--target", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version")
    ap.add_argument("--extra-file", action="append", default=[],
                    help="test-only: also ship this repo-relative path (B3 Leg C)")
    ap.add_argument("--keep-stage", action="store_true")
    ap.add_argument("--signing-key", help=f"ed25519 key path (default ${SIGNING_KEY_ENV})")
    ap.add_argument("--web-dist", help="built frontend/apps/web dist/ (base /app/) to ship at /app/")
    ap.add_argument("--release-url", help="release host base -> BUNDLE.json.releaseUrl "
                    "(what `yard update` prints)")
    a = ap.parse_args(argv)
    try:
        target = target_for(a.target)
    except KeyError as e:
        print(str(e).strip("'\""), file=sys.stderr)
        return 2
    if not target.beta:
        print(f"refusing {target.slug}: {WINDOWS_REFUSAL}", file=sys.stderr)
        return 2
    try:
        res = build(a.target, a.out, version=a.version,
                    extra_files=tuple(a.extra_file), keep_stage=a.keep_stage,
                    signing_key=a.signing_key, web_dist=a.web_dist,
                    release_url=a.release_url)
    except BundleBuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


def _cmd_lock(argv: list[str]) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="mcp_loops.origin_bundle lock")
    ap.add_argument("--target", action="append")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    res = lock(a.target, check=a.check)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["ok"] else 1


def _cmd_sign(argv: list[str]) -> int:
    """``sign ARTIFACT… [--key PATH]`` — (re)sign already-built artifacts."""
    import argparse
    import json
    import os
    import sys
    ap = argparse.ArgumentParser(prog="mcp_loops.origin_bundle sign")
    ap.add_argument("artifacts", nargs="+")
    ap.add_argument("--key", default=os.environ.get(SIGNING_KEY_ENV),
                    help=f"ed25519 key path (default ${SIGNING_KEY_ENV})")
    a = ap.parse_args(argv)
    if not a.key:
        print(f"no signing key: pass --key or set {SIGNING_KEY_ENV}", file=sys.stderr)
        return 2
    try:
        res = [sign_artifact(p, a.key) for p in a.artifacts]
    except BundleBuildError as e:
        print(f"sign failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


def _cmd_verify(argv: list[str]) -> int:
    """``verify`` — the smoke-import manifest, run by whatever interpreter runs
    this (inside a bundle: ``runtime/bin/python3 -I -m mcp_loops.origin_bundle
    verify``)."""
    import json
    res = verify_manifest(BUNDLE_SMOKE_IMPORTS)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m mcp_loops.origin_bundle`` — render the packaging plan or emit a
    PyInstaller spec / entry shim for a target. Deterministic + side-effect-free
    unless ``--out`` is given. Exit 0 iff the hidden-import manifest verifies."""
    import argparse
    import json
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    verbs = {"build": _cmd_build, "lock": _cmd_lock, "sign": _cmd_sign,
             "verify": _cmd_verify}
    if argv and argv[0] in verbs:
        return verbs[argv[0]](argv[1:])

    ap = argparse.ArgumentParser(
        prog="mcp_loops.origin_bundle",
        description="Plan / emit the self-contained origin bundle (§6.5).")
    ap.add_argument("--target", help="matrix cell slug, e.g. linux-x86_64; "
                    "omit to plan the whole matrix")
    ap.add_argument("--emit-spec", action="store_true",
                    help="print the PyInstaller .spec for --target")
    ap.add_argument("--emit-shim", action="store_true",
                    help="print the entry shim script")
    ap.add_argument("--out", help="write the emitted spec/shim here instead of stdout")
    ap.add_argument("--list", action="store_true", help="list supported targets")
    args = ap.parse_args(argv)

    if args.list:
        for t in SUPPORTED_TARGETS:
            print(f"{t.slug}\t{t.artifact_name}\t(detach: {t.detach_kind})")
        return 0

    if args.emit_shim:
        text = generate_entry_shim()
        _emit(text, args.out)
        return 0

    if args.emit_spec:
        if not args.target:
            print("--emit-spec requires --target SLUG", file=sys.stderr)
            return 2
        text = generate_pyinstaller_spec(target_for(args.target))
        _emit(text, args.out)
        return 0

    # default: render the plan (single target or the whole matrix)
    result = plan(target_for(args.target)) if args.target else matrix_plan()
    print(json.dumps(result, indent=2))
    ok = result.get("manifest", {}).get("ok", result.get("manifestOk", True))
    return 0 if ok else 1


def _emit(text: str, out: Optional[str]) -> None:
    if out:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {out}")
    else:
        print(text, end="")


if __name__ == "__main__":
    raise SystemExit(main())
