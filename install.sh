#!/bin/sh
# install.sh — install or upgrade a Loopyard origin bundle (PHASE-B-SPEC §1.8, D12).
#
#   curl -fsSL <release-host>/install.sh | sh                 # fresh → ~/loopyard
#   curl -fsSL <release-host>/install.sh | sh -s -- --dir D   # re-run = upgrade
#   sh install.sh --tarball ./loopyard-origin-<slug>.tar.gz   # offline / CI
#
# Provider preferences: export LOOPYARD_RUNTIME=codex
#                       export LOOPYARD_TRUSTED_WORKSPACE=1
# Flags: --runtime <claude|codex|cursor>, --trusted-workspace, --dir <root> (default ~/loopyard), --version <v> (default latest),
#        --tarball <path> (verified against <path>.sha256 + <path>.sig), --force.
#        --target <slug>, --keep-existing, --allow-downgrade.
# Every bundle must carry a detached Ed25519 SSH signature (<tarball>.sig, made
# with `ssh-keygen -Y sign -n loopyard-release`) that verifies against the
# public key PINNED below via `ssh-keygen -Y verify` (OpenSSH >= 8.1: stock on
# macOS 10.15+ and every current Linux). The pin is compiled in, never read from
# the environment. Until a key is pinned, install.sh refuses to install unless
# the test-only LOOPYARD_ALLOW_UNSIGNED=1 is set (checksum-only, loud warning;
# never suggested in user-facing output); once pinned, it is ignored. Release process: docs/ops/RELEASE-SIGNING.md.
# The release host is RELEASE_DEFAULT_URL below (baked in at release time) or
# $LOOPYARD_RELEASE_URL; either must be https:// (curl/wget pinned to https + TLS>=1.2);
# plain http is accepted only for 127.0.0.1/localhost with LOOPYARD_ALLOW_INSECURE=1
# (local test servers).
# Portal mode (invite-gated release store): when PORTAL_DEFAULT_URL below or
# $LOOPYARD_PORTAL_URL is set, assets come from <portal>/releases/<latest|vX.Y.Z>/
# and every fetch carries $LOOPYARD_INSTALL_TOKEN (a short-lived lyr_ download
# token from the portal) as an Authorization: Bearer header read from a 0600
# file, never in a URL or argv; SHA256SUMS is then mandatory. It takes
# precedence over the release host. Same https / loopback rules.
# A network install records where it came from in <root>/config/release-source.json
# ({"portalUrl":U} or {"releaseUrl":U}, 0644, URL only — never the token) so a
# later `yard update` needs no env; --tarball installs record nothing.
# Non-interactive, never prompts. Exit codes: 0 ok / already current, 1 upgrade
# failed and was reverted, 2 refused (not a bundle dir, unsupported os/arch, bad
# args), 3 download/verify failure, 4 loops still running (use --force).
#
# Upgrade = stop v1 → stage v2 beside <root> → carry config/ data/ workspace/ →
# rename swap → start + status → drop the old code. The root PATH never changes,
# so every absolute path recorded in state stays valid. Crash-resumable: an
# interrupted run leaves <root>.new-* / <root>.old-*, and the next run completes
# or reverts from what it finds. State dirs are moved, never copied or deleted.
set -eu

# >>> portable.sh (verbatim copy of scripts/lib/portable.sh)
# portable.sh — the ONE definition of the Linux/macOS helpers (PHASE-B-SPEC §1.6,
# P10). Sourced by scripts/phase_b_acceptance.sh; install.sh carries a verbatim
# copy between its `>>> portable.sh` / `<<< portable.sh` markers because a piped
# `curl | sh` has nothing to source (test_install_sh.py pins the copy to this file).
sha256_of() {  # prints the bare hex digest of $1
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | cut -d' ' -f1
  else shasum -a 256 -- "$1" | cut -d' ' -f1; fi      # stock macOS has shasum (perl), not sha256sum
}
proc_env()     { if [ -r "/proc/$1/environ" ]; then tr '\0' '\n' < "/proc/$1/environ"; else ps -E -ww -o command= -p "$1"; fi; }
proc_cmdline() { if [ -r "/proc/$1/cmdline" ]; then tr '\0' ' '  < "/proc/$1/cmdline"; else ps -ww -o command= -p "$1"; fi; }
# <<< portable.sh

STATE_DIRS="config data workspace"          # == mcp_loops.paths.STATE_DIRS
ROOT="${HOME}/loopyard"
DEFAULT_VERSION="latest"
VERSION=""
TARBALL=""
FORCE=0
KEEP_EXISTING=0
ALLOW_DOWNGRADE=0
TARGET=""
# The release host this install.sh downloads from (https://…, no trailing
# /<version>), baked in by the release process per docs/ops/RELEASE-SIGNING.md
# so the piped one-liner needs no env var. Empty = a development copy, not one
# published with a release. $LOOPYARD_RELEASE_URL still overrides (mirrors, tests).
RELEASE_DEFAULT_URL=''
RELEASE_URL="${LOOPYARD_RELEASE_URL:-$RELEASE_DEFAULT_URL}"
# The control-plane portal serving invite-gated releases (https://…, no
# /releases suffix), baked in when the portal publishes this install.sh.
# Non-empty (or $LOOPYARD_PORTAL_URL) = portal mode.
PORTAL_DEFAULT_URL=''
PORTAL_URL="${LOOPYARD_PORTAL_URL:-$PORTAL_DEFAULT_URL}"

# The release signing public key (one `ssh-ed25519 AAAA...` line), set by the
# publisher from the owner-provided LOOPYARD_RELEASE_SIGNING_KEY.
# publish_local replaces this placeholder in every published installer.
# Empty is allowed only in this development source copy.
RELEASE_PUBKEY=''
RUNTIME="${LOOPYARD_RUNTIME:-}"
TRUSTED_WORKSPACE="${LOOPYARD_TRUSTED_WORKSPACE:-0}"
SIG_NAMESPACE="loopyard-release"            # == mcp_loops.origin_bundle.SIG_NAMESPACE

say()  { printf 'loopyard-install: %s\n' "$*"; }
fail() { code="$1"; shift; printf 'loopyard-install: %s\n' "$*" >&2; exit "$code"; }

# Versions become path components (<root>.new-<v>, <root>.old-<v>) and URL
# segments, so they must match a strict semver-ish grammar: optional "v",
# dotted digits, optional -/+ suffix of [A-Za-z0-9.]; no "/", no "..", <= 64 chars.
valid_version() {
    case "$1" in ""|*..*|*[!A-Za-z0-9.+-]*) return 1 ;; esac
    [ "${#1}" -le 64 ] || return 1
    vv="${1#v}"; core="${vv%%[-+]*}"; suffix="${vv#"$core"}"
    case "$core" in ""|.*|*.|*[!0-9.]*) return 1 ;; esac      # ^v?N(.N)*
    case "$suffix" in *[-+]|*[-+][-+]*) return 1 ;; esac      # ([-+][A-Za-z0-9.]+)*$
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)       [ $# -ge 2 ] || fail 2 "--dir needs a value"; ROOT="$2"; shift 2 ;;
        --dir=*)     ROOT="${1#--dir=}"; shift ;;
        --version)   [ $# -ge 2 ] || fail 2 "--version needs a value"; VERSION="$2"; [ -n "$VERSION" ] || fail 2 "invalid --version: empty"; shift 2 ;;
        --version=*) VERSION="${1#--version=}"; [ -n "$VERSION" ] || fail 2 "invalid --version: empty"; shift ;;
        --tarball)   [ $# -ge 2 ] || fail 2 "--tarball needs a value"; TARBALL="$2"; shift 2 ;;
        --tarball=*) TARBALL="${1#--tarball=}"; shift ;;
        --force)     FORCE=1; shift ;;
        --runtime) [ $# -ge 2 ] || fail 2 "--runtime needs a value"; RUNTIME="$2"; shift 2 ;;
        --runtime=*) RUNTIME="${1#--runtime=}"; shift ;;
        --trusted-workspace) TRUSTED_WORKSPACE=1; shift ;;
        --keep-existing) KEEP_EXISTING=1; shift ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
        --target) [ $# -ge 2 ] || fail 2 "--target needs a value"; TARGET="$2"; shift 2 ;;
        --target=*) TARGET="${1#--target=}"; shift ;;
        -h|--help)   sed -n '2,33p' "$0" 2>/dev/null || true; exit 0 ;;
        *)           fail 2 "unknown argument: $1" ;;
    esac
done
case "$RUNTIME" in ''|claude|codex|cursor) : ;; *) fail 2 "runtime must be claude, codex, or cursor" ;; esac
case "$RUNTIME:$TRUSTED_WORKSPACE" in codex:1|cursor:1|claude:*|:*) : ;;
    *) fail 2 "Codex/Cursor require --trusted-workspace (or LOOPYARD_TRUSTED_WORKSPACE=1); role restrictions are not enforced in this beta" ;; esac
case "$ROOT" in /*) : ;; *) ROOT="$(pwd -P)/$ROOT" ;; esac
ROOT="${ROOT%/}"
EFFECTIVE_VERSION="${VERSION:-$DEFAULT_VERSION}"
[ "$EFFECTIVE_VERSION" = latest ] || valid_version "$EFFECTIVE_VERSION" \
    || fail 2 "invalid --version '$VERSION' (expected e.g. 1.2.3, 1.2.3-rc.1, 0.0.0+gabc123)"

# ── os/arch → bundle target ──────────────────────────────────────────────────
detect_target() {
    os="$(uname -s)"; arch="$(uname -m)"
    case "$os/$arch" in
        Linux/x86_64|Linux/amd64)   echo linux-x86_64 ;;
        Linux/aarch64|Linux/arm64)  echo linux-aarch64 ;;
        Darwin/arm64)               echo macos-arm64 ;;
        Darwin/x86_64)
            if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
                echo macos-arm64
            else
                echo macos-x86_64
            fi ;;
        MINGW*|MSYS*|CYGWIN*)       fail 2 "Windows is not supported natively — use the linux bundle under WSL2" ;;
        *)                          fail 2 "unsupported platform $os/$arch" ;;
    esac
}

# >>> release_urls (shell twin of mcp_loops.release_urls.release_urls)
release_urls() {
    ru_base="$1"
    while [ "${ru_base%/}" != "$ru_base" ]; do ru_base="${ru_base%/}"; done
    case "$ru_base" in
        */releases)
            if [ "$2" = latest ]; then printf '%s/latest/download\n' "$ru_base"
            else printf '%s/download/v%s\n' "$ru_base" "${2#v}"; fi ;;
        *) printf '%s/%s\n' "$ru_base" "$2" ;;
    esac
}
# <<< release_urls

# >>> portal_release_urls (shell twin of mcp_loops.release_urls.portal_release_urls)
portal_release_urls() {
    pr_base="$1"
    while [ "${pr_base%/}" != "$pr_base" ]; do pr_base="${pr_base%/}"; done
    if [ "$2" = latest ]; then printf '%s/releases/latest\n' "$pr_base"
    else printf '%s/releases/v%s\n' "$pr_base" "${2#v}"; fi
}
# <<< portal_release_urls

# Warn only: dependencies can be installed after the bundle. Match yard's
# _resolve_claude search without executing the CLI or requiring system Python.
prerequisite_report() {
    # This report is advisory even if the platform probe itself fails.
    pre_os="$(uname -s 2>/dev/null || true)"
    if ! command -v tmux >/dev/null 2>&1; then
        if [ "$pre_os" = Darwin ]; then
            if ! command -v brew >/dev/null 2>&1; then
                say 'WARNING: Homebrew missing; install: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            fi
            say 'WARNING: tmux missing; install: brew install tmux'
        else
            say 'WARNING: tmux missing; install: sudo apt-get install -y tmux git'
        fi
    fi
    if [ "$pre_os" = Darwin ]; then
        xcode-select -p >/dev/null 2>&1 || say 'WARNING: git tools missing; install: xcode-select --install'
    elif ! command -v git >/dev/null 2>&1; then
        say 'WARNING: git missing; install: sudo apt-get install -y tmux git'
    fi
    if [ -n "$RUNTIME" ] && [ "$RUNTIME" != claude ]; then
        pre_cli="$RUNTIME"
        [ "$pre_cli" != cursor ] || pre_cli=cursor-agent
        command -v "$pre_cli" >/dev/null 2>&1 || say "WARNING: $pre_cli missing; install it and run $pre_cli login"
        return 0
    fi
    pre_claude=""
    for pre_candidate in "${LOOPS_CLAUDE_BIN:-}" "$(command -v claude 2>/dev/null || true)" "$HOME/.local/bin/claude"; do
        case "$pre_candidate" in '~/'*) pre_candidate="$HOME/${pre_candidate#\~/}" ;; esac
        if [ -f "$pre_candidate" ] && [ -x "$pre_candidate" ]; then
            pre_claude="$pre_candidate"; break
        fi
    done
    [ -n "$pre_claude" ] || say 'WARNING: claude missing; install: curl -fsSL https://claude.ai/install.sh | bash; then: claude auth login'
    return 0
}

json_field() {  # json_field <file> <key> — a flat string field of BUNDLE.json / run.json
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$1" | head -n 1
}

parked_engine_alive() {  # parked_engine_alive <run.json> — is the stamped owner engine pid alive?
    # The engine stamps run.json with "engine": {"pid": N, ...} (indent=2, pid on
    # its own line). No stamp / unreadable pid = unknown → treat as alive (block).
    _pid="$(sed -n '/"engine"[[:space:]]*:/,/}/s/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" | head -n 1)"
    [ -n "$_pid" ] || return 0
    kill -0 "$_pid" 2>/dev/null || [ -d "/proc/$_pid" ]
}

# Personalized portal installs finish by claiming this machine's enrollment.
finish_enrollment() {
    if [ -n "$RUNTIME" ]; then
        runtime_config="${LOOPYARD_CONFIG_DIR:-${LOOPYARD_HOME:-$ROOT}/config}/runtime.json"
        if [ -f "$runtime_config" ]; then
            say "preserving saved provider choice; use yard down then yard runtime use to change it"
        elif [ "$TRUSTED_WORKSPACE" = 1 ]; then
            "$ROOT/bin/yard" runtime use "$RUNTIME" --trusted-workspace || fail 3 "could not save provider settings"
        else
            "$ROOT/bin/yard" runtime use "$RUNTIME" || fail 3 "could not save provider settings"
        fi
    fi
    if [ -n "${LOOPYARD_ENROLL_TOKEN:-}" ]; then
        "$ROOT/bin/yard" enroll || fail 3 "portal enrollment failed; re-run yard enroll with the portal environment"
    fi
}

# A join leaves even interrupted upgrades untouched on existing boxes.
if [ "$KEEP_EXISTING" -eq 1 ] && [ -f "$ROOT/BUNDLE.json" ]; then
    kept="$(json_field "$ROOT/BUNDLE.json" version)"
    valid_version "$kept" || fail 2 "invalid installed version '$kept'"
    say "this box runs v${kept#v}, the Hub v${EFFECTIVE_VERSION#v}; keeping the installed bundle; use yard update to change versions"
    finish_enrollment
    exit 0
fi

# SemVer precedence without build metadata. Compare decimal strings by length
# then lexically, avoiding awk floating-point precision loss.
version_older() {
    LC_ALL=C awk -v a="$1" -v b="$2" '
        function numcmp(x,y) {
            sub(/^0+/, "", x); sub(/^0+/, "", y)
            if (length(x) != length(y)) return length(x) < length(y) ? -1 : 1
            return ("x" x < "x" y) ? -1 : (("x" x > "x" y) ? 1 : 0)
        }
        function cmp(a,b, ac,bc,ap,bp,an,bn,i,c,x,y) {
            sub(/^v/, "", a); sub(/^v/, "", b)
            sub(/\+.*/, "", a); sub(/\+.*/, "", b)
            ap=a; bp=b; sub(/-.*/, "", a); sub(/-.*/, "", b)
            ap=ap==a ? "" : substr(ap,length(a)+2)
            bp=bp==b ? "" : substr(bp,length(b)+2)
            an=split(a,ac,"."); bn=split(b,bc,".")
            for(i=1;i<=an || i<=bn;i++) { c=numcmp(ac[i],bc[i]); if(c) return c }
            if(ap==bp) return 0
            if(ap=="") return 1
            if(bp=="") return -1
            an=split(ap,ac,"."); bn=split(bp,bc,".")
            for(i=1;i<=an && i<=bn;i++) {
                x=ac[i]; y=bc[i]
                if(x ~ /^[0-9]+$/ && y ~ /^[0-9]+$/) c=numcmp(x,y)
                else if(x ~ /^[0-9]+$/) c=-1
                else if(y ~ /^[0-9]+$/) c=1
                else c=("x" x < "x" y) ? -1 : (("x" x > "x" y) ? 1 : 0)
                if(c) return c
            }
            return an<bn ? -1 : (an>bn ? 1 : 0)
        }
        BEGIN { exit cmp(a,b)<0 ? 0 : 1 }'
}

# https only. The loopback exception parses the authority exactly (no userinfo,
# numeric port) so http://localhost.evil / http://127.0.0.1@evil never qualify.
INSECURE=0
check_release_url() {  # check_release_url <url> — exit 2 unless the fetch is safe
    case "$1" in
        https://?*) return 0 ;;
        http://*)   : ;;
        *)          fail 2 "the release URL must be an https:// URL (got: $1)" ;;
    esac
    [ "${LOOPYARD_ALLOW_INSECURE:-0}" = 1 ] \
        || fail 2 "refusing plaintext http:// release host $1 — use https:// (LOOPYARD_ALLOW_INSECURE=1 permits http only for 127.0.0.1/localhost)"
    auth="${1#http://}"; auth="${auth%%/*}"
    host="${auth%%:*}"; port="${auth#"$host"}"; port="${port#:}"
    case "$auth" in *[!A-Za-z0-9.:]*) fail 2 "LOOPYARD_ALLOW_INSECURE: bad release host in $1" ;; esac
    case "$port" in *[!0-9]*) fail 2 "LOOPYARD_ALLOW_INSECURE: bad port in $1" ;; esac
    case "$host" in
        127.0.0.1|localhost) : ;;
        *) fail 2 "LOOPYARD_ALLOW_INSECURE only permits http://127.0.0.1 or http://localhost, not $1 — use https://" ;;
    esac
    INSECURE=1
    printf 'loopyard-install: WARNING: insecure plaintext download from %s (LOOPYARD_ALLOW_INSECURE=1, local testing only)\n' "$1" >&2
}

# Where this install came from, for `yard update` (mcp_loops/yard.py
# RELEASE_SOURCE_FILE). Set only by a network fetch; the value is a URL, never a token.
SOURCE_KEY=""
SOURCE_URL=""
record_release_source() {  # best effort: never fails an install
    [ -n "$SOURCE_KEY" ] || return 0
    rs_url="$SOURCE_URL"
    while [ "${rs_url%/}" != "$rs_url" ]; do rs_url="${rs_url%/}"; done
    # Only plain URL characters: nothing to JSON-escape, and no userinfo (@).
    case "$rs_url" in
        ""|*[!A-Za-z0-9._~:/%+=,-]*)
            say "WARNING: not recording the release source (unexpected characters in the URL); yard update will need LOOPYARD_PORTAL_URL or LOOPYARD_RELEASE_URL"
            return 0 ;;
    esac
    rs_tmp="$ROOT/config/.release-source.json.$$"
    if mkdir -p "$ROOT/config" \
        && (umask 022; printf '{\n  "%s": "%s"\n}\n' "$SOURCE_KEY" "$rs_url" > "$rs_tmp") \
        && mv -f "$rs_tmp" "$ROOT/config/release-source.json"; then
        say "recorded release source for yard update ($SOURCE_KEY)"
    else
        rm -f "$rs_tmp" 2>/dev/null || true
        say "WARNING: could not write $ROOT/config/release-source.json; yard update will need the source URL in the environment"
    fi
    return 0
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/loopyard-install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT INT TERM

# ── fetch (unless --tarball) + verify BEFORE anything is unpacked ────────────
if [ -z "$TARBALL" ]; then
    if [ -z "$TARGET" ] && [ -f "$ROOT/BUNDLE.json" ]; then
        TARGET="$(json_field "$ROOT/BUNDLE.json" target)"
    fi
    [ -n "$TARGET" ] || TARGET="$(detect_target)"
    case "$TARGET" in
        linux-x86_64|linux-aarch64|macos-arm64|macos-x86_64) : ;;
        *) fail 2 "unsupported bundle target: $TARGET" ;;
    esac
    NAME="loopyard-origin-$TARGET.tar.gz"
    TARBALL="$WORK/$NAME"
    AUTH=0
    if [ -n "$PORTAL_URL" ]; then
        # Portal mode: the token only ever travels in a header read from a
        # 0600 file (curl -H @file / WGETRC), never a URL, argv or our output.
        tok="${LOOPYARD_INSTALL_TOKEN:-}"
        [ -n "$tok" ] || fail 3 "portal install needs LOOPYARD_INSTALL_TOKEN (a download token from your Loopyard portal's onboarding page) — copy the whole install command from the portal"
        case "$tok" in
            lyr_?*) case "${tok#lyr_}" in *[!A-Za-z0-9_-]*) fail 3 "LOOPYARD_INSTALL_TOKEN is malformed — copy the install command from the portal again" ;; esac ;;
            *) fail 3 "LOOPYARD_INSTALL_TOKEN is not a Loopyard download token — copy the install command from the portal again" ;;
        esac
        check_release_url "$PORTAL_URL"
        ASSET_URL="$(portal_release_urls "$PORTAL_URL" "$EFFECTIVE_VERSION")"
        SOURCE_KEY=portalUrl; SOURCE_URL="$PORTAL_URL"
        (umask 077
         printf 'Authorization: Bearer %s\n' "$tok" > "$WORK/auth.hdr"
         printf 'header = Authorization: Bearer %s\n' "$tok" > "$WORK/auth.wgetrc")
        unset tok
        AUTH=1
    else
        [ -n "$RELEASE_URL" ] || fail 3 "this install.sh has no release host built in (it is a development copy, not one published with a release) — download install.sh from the release page, pass --tarball <bundle>, or set LOOPYARD_RELEASE_URL=https://<release-host>"
        check_release_url "$RELEASE_URL"
        ASSET_URL="$(release_urls "$RELEASE_URL" "$EFFECTIVE_VERSION")"
        SOURCE_KEY=releaseUrl; SOURCE_URL="$RELEASE_URL"
    fi
    fetch() {  # fetch <asset> — into $WORK/<asset>; non-zero on any failure
        if command -v curl >/dev/null 2>&1; then
            if [ "$INSECURE" -eq 1 ]; then set -- "$1" --proto '=http'; else set -- "$1" --proto '=https' --tlsv1.2; fi
            # Authenticated fetches never follow redirects (the portal serves
            # directly; a redirect must not carry the token elsewhere).
            if [ "$AUTH" -eq 1 ]; then set -- "$@" -fsS -H "@$WORK/auth.hdr"; else set -- "$@" -fsSL; fi
            f="$1"; shift
            curl "$@" -o "$WORK/$f" "$ASSET_URL/$f"
        else
            if [ "$INSECURE" -eq 1 ]; then set -- "$1"; else set -- "$1" --https-only --secure-protocol=TLSv1_2; fi
            f="$1"; shift
            if [ "$AUTH" -eq 1 ]; then
                WGETRC="$WORK/auth.wgetrc" wget -q --max-redirect=0 "$@" -O "$WORK/$f" "$ASSET_URL/$f"
            else
                wget -q "$@" -O "$WORK/$f" "$ASSET_URL/$f"
            fi
        fi
    }
    set -- "$NAME" "$NAME.sha256"
    [ -z "$RELEASE_PUBKEY" ] || set -- "$@" "$NAME.sig"
    for a in "$@"; do
        fetch "$a" || fail 3 "download failed: $a"
    done
    if [ "$AUTH" -eq 1 ]; then
        # The portal always publishes SHA256SUMS; its absence is a failure.
        fetch SHA256SUMS 2>/dev/null || fail 3 "download failed: SHA256SUMS (required from the portal)"
        rm -f "$WORK/auth.hdr" "$WORK/auth.wgetrc"
    else
        # Older releases lack this optional manifest. Discard partial downloads.
        fetch SHA256SUMS 2>/dev/null || rm -f "$WORK/SHA256SUMS"
    fi
fi
[ -f "$TARBALL" ] || fail 3 "no such tarball: $TARBALL"
[ -f "$TARBALL.sha256" ] || fail 3 "missing checksum file: $TARBALL.sha256"
want="$(cut -d' ' -f1 < "$TARBALL.sha256")"
got="$(sha256_of "$TARBALL")"
[ -n "$want" ] && [ "$want" = "$got" ] || fail 3 "checksum mismatch for $TARBALL (want $want, got $got)"
say "verified $(basename "$TARBALL") ($got)"
SUMS="$(dirname "$TARBALL")/SHA256SUMS"
if [ -f "$SUMS" ]; then
    sum_want="$(awk -v name="$(basename "$TARBALL")" '
        { f=$2; sub(/^\*/, "", f); if (f==name) { n++; digest=$1 } }
        END { if(n==1) print digest }' "$SUMS")"
    [ "$sum_want" = "$got" ] || fail 3 "SHA256SUMS mismatch or missing/duplicate entry for $(basename "$TARBALL")"
    say "verified SHA256SUMS entry"
fi

# Provenance: the .sha256 comes from the same host as the tarball, so only the
# signature against the pinned key proves who built it.
verify_signature() {  # verify_signature <tarball> — exit 3 unless signed by the pinned key
    case "$RELEASE_PUBKEY" in
        "ssh-ed25519 "?*) : ;;
        *) fail 3 "the pinned release key is not an ssh-ed25519 public key" ;;
    esac
    command -v ssh-keygen >/dev/null 2>&1 \
        || fail 3 "ssh-keygen (OpenSSH >= 8.1) is required to verify the bundle signature"
    [ -f "$1.sig" ] || fail 3 "missing signature file: $1.sig"
    printf '%s namespaces="%s" %s\n' "$SIG_NAMESPACE" "$SIG_NAMESPACE" "$RELEASE_PUBKEY" \
        > "$WORK/allowed_signers"
    ssh-keygen -Y verify -f "$WORK/allowed_signers" -I "$SIG_NAMESPACE" \
        -n "$SIG_NAMESPACE" -s "$1.sig" < "$1" > "$WORK/sigcheck" 2>&1 \
        || fail 3 "signature verification FAILED for $1 — not signed by the pinned Loopyard release key: $(tr '\n' ' ' < "$WORK/sigcheck") — refusing to install; nothing was changed. Please report this, with this message, to whoever gave you the install link."
}
if [ -n "$RELEASE_PUBKEY" ]; then
    verify_signature "$TARBALL"
    say "signature ok ($(basename "$TARBALL").sig, pinned ed25519 release key)"
elif [ "${LOOPYARD_ALLOW_UNSIGNED:-0}" = 1 ]; then
    printf '%s\n' \
        "loopyard-install: ************************************************************" \
        "loopyard-install: WARNING: NO RELEASE SIGNING KEY IS PINNED IN THIS install.sh." \
        "loopyard-install: WARNING: $(basename "$TARBALL") is checked against a same-host" \
        "loopyard-install: WARNING: .sha256 ONLY — its provenance is NOT verified." \
        "loopyard-install: WARNING: continuing because LOOPYARD_ALLOW_UNSIGNED=1." \
        "loopyard-install: ************************************************************" >&2
else
    # Never advertise the test-only override here (F13): a user must not be
    # talked into switching verification off.
    fail 3 "this Loopyard release is not signed yet (no release signing key is pinned in this install.sh), so $(basename "$TARBALL") cannot be verified — refusing to install; nothing was changed. Please report this, with this message, to whoever gave you the install link."
fi

# Audit every member BEFORE anything is extracted, independent of which tar is
# installed: paths live under loopyard/ and are already canonical — no empty
# ("//", which --strip-components 1 would turn into an absolute path), "." or
# ".." component and no backslash (tar's escape for odd bytes), so the depth the
# audit counts is exactly the depth tar writes; only regular files, dirs,
# symlinks and hardlinks; symlink targets are relative with no "//", use ".."
# only as a leading run and stay inside loopyard/; hardlink targets are canonical
# member paths; and no member sits beneath a symlink member (so a chain of
# links can never be written through to escape the stage dir); nor does a
# hardlink name a symlink member (or a path beneath one).
# Names come from the plain `tar -t` listing; the verbose one only supplies the
# type (its first char) and link target (the text after the exact known name).
# Owner names are member-controlled text printed before the name, so they are
# listed numerically, and neither a name nor a target may contain " -> ",
# " link to " or control chars: then a spoofed separator inside the owner
# fields can only yield a target that still contains one, which is refused.
check_members() {  # check_members <tarball> — exit 3 on any unsafe member
    LC_ALL=C tar -tzf "$1" > "$WORK/members" 2>/dev/null \
        && LC_ALL=C tar --numeric-owner -tvzf "$1" > "$WORK/members.v" 2>/dev/null \
        || fail 3 "cannot list the members of $1"
    bad="$(LC_ALL=C awk '
        function odd(s) { return index(s, " -> ") || index(s, " link to ") || s ~ /[[:cntrl:]]/ }
        function badpath(p,   n, c, i) {
            if (p !~ /^loopyard(\/|$)/ || index(p, "\\") || odd(p)) return 1
            n = split(p, c, "/")
            for (i = 1; i <= n; i++)
                if (c[i] == ".." || c[i] == "." || c[i] == "") return 1
            return 0
        }
        function escapes(p, t,   n, c, i, depth, seen) {
            if (t == "" || t ~ /^\// || t ~ /\/\// || index(t, "\\") || odd(t)) return 1
            depth = split(p, c, "/") - 1          # dirs above the link, incl. loopyard
            n = split(t, c, "/"); seen = 0
            for (i = 1; i <= n; i++) {
                if (c[i] == "..") { if (seen || --depth < 1) return 1 }
                else if (c[i] != "." && c[i] != "") seen = 1
            }
            return 0
        }
        NR == FNR { name[NR] = $0; N = NR; next }
        { v[FNR] = $0; M = FNR }
        END {
            if (N != M || N == 0) { print "unreadable member listing"; exit }
            for (i = 1; i <= N; i++) {
                p = name[i]; sub(/\/$/, "", p)
                if (substr(v[i], 1, 1) == "l") link[p] = 1
            }
            for (i = 1; i <= N; i++) {
                p = name[i]; sub(/\/$/, "", p); ty = substr(v[i], 1, 1)
                if (badpath(p)) { print "unsafe path " name[i]; exit }
                for (q = p; (k = match(q, /\/[^\/]*$/)) > 0; ) {
                    q = substr(q, 1, k - 1)
                    if (q in link) { print "member beneath symlink " q ": " name[i]; exit }
                }
                if (ty == "-" || ty == "d") {
                    k = length(v[i]) - length(name[i])
                    if (k < 2 || substr(v[i], k) != " " name[i]) { print "member listing mismatch " name[i]; exit }
                    continue
                }
                if (ty == "l") {
                    k = index(v[i], " " name[i] " -> ")
                    t = k ? substr(v[i], k + length(name[i]) + 5) : ""
                    if (escapes(p, t)) { print "symlink escapes the bundle " name[i] " -> " t; exit }
                    continue
                }
                if (ty == "h") {
                    k = index(v[i], " " name[i] " link to ")
                    t = k ? substr(v[i], k + length(name[i]) + 10) : ""
                    if (badpath(t)) { print "hardlink escapes the bundle " name[i] " -> " t; exit }
                    # a hardlink to a symlink member relocates its relative target
                    for (q = t; q != ""; q = (k = match(q, /\/[^\/]*$/)) > 0 ? substr(q, 1, k - 1) : "")
                        if (q in link) { print "hardlink to symlink member " name[i] " -> " t; exit }
                    continue
                }
                print "unsupported member type " ty ": " name[i]; exit
            }
        }' "$WORK/members" "$WORK/members.v")" || fail 3 "cannot audit the members of $1"
    [ -z "$bad" ] || fail 3 "refusing $(basename "$1"): $bad"
}
check_members "$TARBALL"

tar -xzf "$TARBALL" -C "$WORK" loopyard/BUNDLE.json 2>/dev/null \
    || fail 3 "$TARBALL is not a Loopyard bundle (no loopyard/BUNDLE.json)"
NEWV="$(json_field "$WORK/loopyard/BUNDLE.json" version)"
[ -n "$NEWV" ] || fail 3 "bundle has no version in BUNDLE.json"
valid_version "$NEWV" || fail 3 "bundle BUNDLE.json has an invalid version '$NEWV' — refusing to stage it"
say "resolved version: $NEWV"

# Defense in depth, independent of how tar listed or wrote the members: audit
# the extracted tree itself before anything is moved or run. Only regular
# files, dirs and symlinks; no setuid/setgid; every symlink resolves (through
# any chain) to a location inside the stage dir; and every multiply-linked file
# has all of its links inside the stage dir (a hardlink to an outside file has
# more links than the tree holds).
audit_tree() {  # audit_tree <dir> — prints the first problem, if any
    top="$(cd -P "$1" && pwd -P)" || { echo "cannot resolve $1"; return 0; }
    find "$1" ! -type f ! -type d ! -type l -print | sed -n '1s/^/special file /p'
    find "$1" -type f \( -perm -4000 -o -perm -2000 \) -print | sed -n '1s/^/setuid\/setgid file /p'
    find "$1" -type l -exec sh -c '
        top="$1"; shift
        for l; do
            t="$(readlink "$l")" || { echo "unreadable symlink $l"; exit 0; }
            case "$t" in /*|"") echo "symlink escapes the bundle $l -> $t"; exit 0 ;; esac
            case "$t" in */*) td="${t%/*}" ;; *) td=. ;; esac
            if [ -d "$l" ]; then r="$(cd -P "$l" 2>/dev/null && pwd -P)"
            else r="$(cd -P "${l%/*}" 2>/dev/null && cd -P "./$td" 2>/dev/null && pwd -P)"
            fi
            case "$r" in "$top"|"$top"/*) ;; *) echo "symlink escapes the bundle $l -> $t"; exit 0 ;; esac
        done' sh "$top" {} + | sed -n 1p
    find "$1" -type f -links +1 -exec ls -ldi {} + | awk '
        { seen[$1]++; want[$1] = $3 }
        END { for (i in seen) if (seen[i] != want[i]) { print "hardlink to a file outside the bundle (inode " i ")"; exit } }'
}

stage() {  # stage <dest> — unpack the verified, audited loopyard/ tree into <dest>
    mkdir -p "$1"
    tar -xzf "$TARBALL" -C "$1" --strip-components 1 --no-same-owner \
        || { rm -rf "$1"; say "cannot extract $(basename "$TARBALL")" >&2; return 1; }
    bad="$(audit_tree "$1" | sed -n 1p)"
    [ -z "$bad" ] || { rm -rf "$1"; say "refusing $(basename "$TARBALL") after extraction: $bad" >&2; return 1; }
}

carry_state() {  # carry_state <from> <to> — move each existing state dir
    for d in $STATE_DIRS; do
        if [ -e "$1/$d" ] && [ ! -e "$2/$d" ]; then mv "$1/$d" "$2/$d"; fi
    done
}

ONE=""
one_of() {  # one_of <glob-prefix> — the single match of "<prefix>*", or ""
    ONE=""
    for p in "$1"*; do if [ -e "$p" ]; then ONE="$p"; fi; done
}

# Step 6: start the new tree with the recorded shape and confirm it is healthy;
# on failure revert to <old> (state dirs moved back, names swapped back).
verify_or_revert() {  # verify_or_revert <old-dir>
    old="$1"
    if "$ROOT/bin/yard" start --resume && "$ROOT/bin/yard" status --check; then
        rm -rf "$old"
        return 0
    fi
    say "the new version did not come up healthy — reverting to $(basename "$old")"
    "$ROOT/bin/yard" down || true
    carry_state "$ROOT" "$old"
    mv "$ROOT" "$WORK/failed"
    mv "$old" "$ROOT"
    rm -rf "$WORK/failed"
    "$ROOT/bin/yard" start --resume || true
    fail 1 "upgrade to $NEWV failed; $ROOT is back on the previous version"
}

# ── step 7: resume an interrupted run from the observable state ──────────────
one_of "$ROOT.new-"; STALE_NEW="$ONE"
one_of "$ROOT.old-"; STALE_OLD="$ONE"
if [ -n "$STALE_NEW" ] || [ -n "$STALE_OLD" ]; then
    say "resuming an interrupted install (${STALE_NEW:+$(basename "$STALE_NEW") }${STALE_OLD:+$(basename "$STALE_OLD")})"
    if [ ! -e "$ROOT" ] && [ -n "$STALE_NEW" ] && [ -n "$STALE_OLD" ]; then
        mv "$STALE_NEW" "$ROOT"                  # killed between the two renames: finish step 5
        verify_or_revert "$STALE_OLD"            # (classify below then says "already at")
        STALE_NEW=""; STALE_OLD=""
    elif [ ! -e "$ROOT" ] && [ -n "$STALE_OLD" ]; then
        mv "$STALE_OLD" "$ROOT"                  # killed mid-revert: finish the revert
        STALE_OLD=""
    elif [ -e "$ROOT" ] && [ -n "$STALE_OLD" ]; then
        [ -z "$STALE_NEW" ] || { carry_state "$STALE_NEW" "$ROOT"; rm -rf "$STALE_NEW"; }
        verify_or_revert "$STALE_OLD"            # killed after the swap: redo step 6
        STALE_OLD=""
    fi
    if [ -n "$STALE_NEW" ] && [ -e "$STALE_NEW" ]; then
        carry_state "$STALE_NEW" "$ROOT"         # killed during steps 3-4: undo, re-stage below
        rm -rf "$STALE_NEW"
    fi
fi

# ── step 1: classify <root> ──────────────────────────────────────────────────
if [ ! -e "$ROOT" ]; then
    mkdir -p "$(dirname "$ROOT")"
    stage "$ROOT.new-$NEWV" || fail 3 "nothing was installed"
    mv "$ROOT.new-$NEWV" "$ROOT"
    say "installed Loopyard $NEWV into $ROOT"
    record_release_source
    prerequisite_report
    say "then:  $ROOT/bin/yard start"
    finish_enrollment
    exit 0
fi
[ -f "$ROOT/BUNDLE.json" ] || fail 2 "$ROOT is not a Loopyard bundle; pass --dir"
OLDV="$(json_field "$ROOT/BUNDLE.json" version)"
valid_version "$OLDV" || fail 2 "$ROOT/BUNDLE.json has an invalid version '$OLDV' — refusing to upgrade it in place"
if [ "$OLDV" = "$NEWV" ]; then
    say "already at $NEWV"
    record_release_source
    finish_enrollment
    exit 0
fi

if version_older "$NEWV" "$OLDV"; then
    if [ -z "$VERSION" ] && [ "$ALLOW_DOWNGRADE" -eq 0 ]; then
        fail 2 "v${OLDV#v} is newer than latest (v${NEWV#v}) — v${OLDV#v} was probably WITHDRAWN, see ${RELEASE_URL:-the release page}; to go back: --version ${NEWV#v}"
    fi
    say "DOWNGRADING v${OLDV#v} → v${NEWV#v}"
fi

# ── step 1b: quiet-point gate — never swap code under running loops ──────────
S="${LOOPYARD_HOME:-$ROOT}"
LIVE=""
for rj in "$S"/data/_loops/*/run.json; do
    [ -f "$rj" ] || continue
    case "$(json_field "$rj" state)" in
        running|stopping) LIVE="$LIVE $(basename "$(dirname "$rj")")" ;;
        waiting_owner|needs_owner)
            # H6a: a run parked on the owner holds no process of its own — only
            # its engine's in-memory reply queue. Parked under a DEAD engine it
            # can never resume (the next engine start sweeps it to wedged), so it
            # is not a loop the swap would cut; only count it under a live engine.
            if parked_engine_alive "$rj"; then
                LIVE="$LIVE $(basename "$(dirname "$rj")")"
            else
                say "not blocking on $(basename "$(dirname "$rj")"): parked on the owner but its engine is gone (swept on next start)"
            fi ;;
    esac
done
if [ -n "$LIVE" ]; then
    if [ "$FORCE" -ne 1 ]; then
        fail 4 "loops still running:$LIVE. Stop them (\`yard\` / the app) or wait for them to finish, then re-run; --force upgrades anyway and their agents may fail to report across the swap."
    fi
    say "--force: upgrading with loops still running:$LIVE (restart them afterwards)"
fi

# ── steps 2-6 ────────────────────────────────────────────────────────────────
say "upgrading $ROOT: $OLDV → $NEWV"
"$ROOT/bin/yard" down || fail 1 "\`$ROOT/bin/yard down\` failed — nothing was changed"
if ! stage "$ROOT.new-$NEWV"; then
    "$ROOT/bin/yard" start --resume || true
    fail 3 "nothing was changed; $ROOT is still on $OLDV"
fi
carry_state "$ROOT" "$ROOT.new-$NEWV"
mv "$ROOT" "$ROOT.old-$OLDV"
mv "$ROOT.new-$NEWV" "$ROOT"
verify_or_revert "$ROOT.old-$OLDV"
say "upgraded to $NEWV (config/, data/ and workspace/ preserved)"
record_release_source

finish_enrollment
# Completeness sentinel: the connect line runs a DOWNLOADED copy of this file
# only when this is its last line, so a truncated fetch never half-executes.
# loopyard-install-end
