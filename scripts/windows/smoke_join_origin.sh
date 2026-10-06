#!/bin/sh
# Static/behavioural smoke of join-origin.ps1 on LINUX pwsh with a stub wsl.exe —
# the most that is honestly provable without Windows. Proves the wrapper's
# decisions (no WSL / no distro / WSL1 refusals, bad line, preflight, rc passing),
# that the join line reaches the distro on STDIN and the code is never on argv,
# and the keep-alive command. It does NOT prove WSL2 itself, Task Scheduler, or
# networking from inside WSL — the owner runs those on a real PC (WINDOWS-WSL2.md).
#
#   docker run --rm -v "$PWD/scripts/windows:/w:ro" \
#     mcr.microsoft.com/powershell:lts-ubuntu-22.04 sh /w/smoke_join_origin.sh
set -u
PS1=/w/join-origin.ps1
T=$(mktemp -d)
STUB="$T/wsl.exe"
cat > "$STUB" <<'STUBEOF'
#!/bin/sh
# stub wsl.exe: `--list --verbose` prints $STUB_LIST (rc $STUB_LIST_RC);
# `-d D -- sh -s` logs argv + stdin, returns $STUB_PRE_RC for the preflight
# script (it mentions `command -v`), else $STUB_RC.
if [ "$1" = "--list" ]; then
  [ "${STUB_LIST_RC:-0}" = 0 ] || exit "$STUB_LIST_RC"
  printf '%b' "$STUB_LIST"; exit 0
fi
n=$(ls "$STUB_LOG" 2>/dev/null | grep -c "\.argv$")
printf '%s\n' "$*" > "$STUB_LOG/call$n.argv"
cat > "$STUB_LOG/call$n.stdin"
if grep -q 'command -v' "$STUB_LOG/call$n.stdin"; then exit "${STUB_PRE_RC:-0}"; fi
exit "${STUB_RC:-0}"
STUBEOF
chmod +x "$STUB"
LINE="printf '%s\\n' S3CRETcd | ~/loopyard/bin/yard origin up --hub wss://203.0.113.7:19821 --hub-fingerprint SHA256:9f --pair-code -"
OK_LIST='  NAME            STATE           VERSION\n* Ubuntu-24.04    Running         2\n  docker-desktop  Stopped         2\n'
pass=0; failn=0
check() { if [ "$1" = 0 ]; then pass=$((pass+1)); echo "  ok   $2"; else failn=$((failn+1)); echo "  FAIL $2"; fi; }
run() {  # run <name> [pwsh args...] -> $RC, $OUT, fresh $STUB_LOG
  export STUB_LOG="$T/log-$1"; rm -rf "$STUB_LOG"; mkdir -p "$STUB_LOG"; shift
  OUT=$(pwsh -NoProfile -NonInteractive -File "$PS1" -Wsl "$STUB" "$@" 2>&1); RC=$?
}

echo "== join-origin.ps1 smoke (pwsh $(pwsh -NoProfile -c '$PSVersionTable.PSVersion.ToString()'))"
pwsh -NoProfile -c "\$e=\$null; [void][System.Management.Automation.Language.Parser]::ParseFile('$PS1',[ref]\$null,[ref]\$e); if (\$e.Count) { \$e; exit 1 }"
check $? "parses with zero PowerShell syntax errors"

export STUB_LIST_RC=1 STUB_LIST=""
run nowsl -JoinLine "$LINE"
[ $RC = 2 ] && echo "$OUT" | grep -q 'WSL is not installed' ; check $? "no WSL -> rc 2 + install hint"
export STUB_LIST_RC=0

export STUB_LIST='  NAME   STATE   VERSION\n* Debian Running 2\n'
run nodistro -JoinLine "$LINE"
[ $RC = 2 ] && echo "$OUT" | grep -q "distro 'Ubuntu-24.04' not found"; check $? "missing distro -> rc 2"

export STUB_LIST='  NAME   STATE   VERSION\n* Ubuntu-24.04 Running 1\n'
run wsl1 -JoinLine "$LINE"
[ $RC = 2 ] && echo "$OUT" | grep -q 'needs WSL2'; check $? "WSL1 distro -> rc 2 + set-version hint"

export STUB_LIST="$OK_LIST"
run badline -JoinLine "curl evil | sh"
[ $RC = 2 ] && echo "$OUT" | grep -q 'not a Loopyard join line' && [ -z "$(ls "$STUB_LOG")" ]
check $? "a non-join line is refused before anything runs in WSL"

export STUB_PRE_RC=2
run notready -JoinLine "$LINE"
[ $RC = 2 ] && [ "$(ls "$STUB_LOG" | grep -c stdin)" = 1 ]; check $? "distro preflight failure -> rc 2, join line never sent (code not spent)"
export STUB_PRE_RC=0

export STUB_RC=0
run join -JoinLine "$LINE"
[ $RC = 0 ]; check $? "join -> rc 0"
[ "$(cat "$STUB_LOG/call1.stdin")" = "$LINE" ]; check $? "the join line reaches the distro verbatim on STDIN"
grep -q 'S3CRETcd' "$STUB_LOG"/*.argv; [ $? = 1 ]; check $? "the pairing code is on NO wsl.exe argv"
[ "$(cat "$STUB_LOG/call1.argv")" = "-d Ubuntu-24.04 -- sh -s" ]; check $? "wsl argv is exactly: -d Ubuntu-24.04 -- sh -s"

export STUB_RC=2
run joinfail -JoinLine "$LINE"
[ $RC = 2 ] && echo "$OUT" | grep -q 'yard origin up failed'; check $? "yard's non-zero exit (bad code / unreachable Hub) is passed through"
export STUB_RC=0

run status -Status
[ $RC = 0 ] && [ "$(cat "$STUB_LOG/call0.stdin")" = "~/loopyard/bin/yard origin status" ]; check $? "-Status runs \`yard origin status\` in the distro"
run down -Down
[ "$(cat "$STUB_LOG/call0.stdin")" = "~/loopyard/bin/yard origin down" ]; check $? "-Down runs \`yard origin down\`"

run keep -JoinLine "$LINE" -KeepAlive
[ $RC = 0 ] && echo "$OUT" | grep -qF -- "-d Ubuntu-24.04 -- sh -lc \"~/loopyard/bin/yard origin up --hub wss://203.0.113.7:19821; exec sleep infinity\""
check $? "-KeepAlive: logon re-join command (idempotent up, no code) is emitted"
echo "$OUT" | grep -q 'S3CRETcd'; [ $? = 1 ]; check $? "the pairing code never appears in the wrapper's output"

rm -rf "$T"
echo "== $pass passed, $failn failed"
[ "$failn" = 0 ] && echo WIN_WSL_SMOKE_OK
[ "$failn" = 0 ]
