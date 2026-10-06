<#
.SYNOPSIS
  Join this Windows PC to a Loopyard Hub as an origin — through WSL2 (PHASE-B D3).

.DESCRIPTION
  Loopyard's worker substrate is tmux, so Windows is NOT a native target: the PC
  runs the LINUX bundle inside a WSL2 distro. This wrapper adds no engine logic.
  It checks that WSL2 and the distro are present, then feeds the Hub's minted
  join line (the SAME `yard origin up` a Linux box pastes) to `sh -s` inside the
  distro on STDIN. The pairing code therefore never appears on any argv, neither
  wsl.exe's (visible in Task Manager) nor yard's (visible in `ps`).

  -KeepAlive registers a logon Scheduled Task that re-runs the idempotent
  `yard origin up --hub URL` (an already-paired box needs no code) and then holds
  the distro open, so the origin comes back after a reboot or WSL idle shutdown.

.PARAMETER Distro     WSL distro to use (default Ubuntu-24.04).
.PARAMETER JoinLine   The line printed by `yard hub pair-code` (prompted when omitted).
.PARAMETER Status     Only print `yard origin status` from the distro.
.PARAMETER Down       Run `yard origin down` in the distro.
.PARAMETER KeepAlive  Also register the logon task (needs the Hub URL: from -JoinLine or -Hub).
.PARAMETER Hub        The Hub URL, for -KeepAlive on an already-joined PC.
.PARAMETER Wsl        The wsl executable (tests substitute a stub).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File join-origin.ps1          # paste the line when asked
  powershell -ExecutionPolicy Bypass -File join-origin.ps1 -Status
#>
[CmdletBinding()]
param(
  [string]$Distro = 'Ubuntu-24.04',
  [string]$JoinLine,
  [switch]$Status,
  [switch]$Down,
  [switch]$KeepAlive,
  [string]$Hub,
  [string]$Wsl = 'wsl.exe'
)
$ErrorActionPreference = 'Stop'
# wsl.exe prints UTF-16 unless told otherwise; WSL >= 0.64 honours this.
$env:WSL_UTF8 = '1'
$Yard = '~/loopyard/bin/yard'

function Fail([int]$Code, [string]$Msg) {
  [Console]::Error.WriteLine("loopyard-wsl: $Msg")
  exit $Code
}

# Run a short shell script inside the distro, fed on stdin (never on argv).
function Invoke-InDistro([string]$Script) {
  # Out-Host: the distro's output goes to the console, not into our return value.
  $Script | & $Wsl -d $Distro -- sh -s | Out-Host
  return $LASTEXITCODE
}

# The WSL version of $Distro from `wsl --list --verbose`, or $null when absent.
function Get-DistroVersion {
  $rows = & $Wsl --list --verbose 2>$null
  if ($LASTEXITCODE -ne 0) { return 'no-wsl' }
  foreach ($row in $rows) {
    $cols = (($row -replace "`0", '') -replace '^\s*\*?\s*', '') -split '\s+' | Where-Object { $_ }
    if ($cols.Count -ge 3 -and $cols[0] -eq $Distro) { return $cols[-1] }
  }
  return $null
}

# Pull --hub out of a join line (only for the keep-alive task; the line itself
# is run by sh inside the distro, not parsed into a command here).
function Get-HubFromLine([string]$Line) {
  if ($Line -match "--hub\s+'?([^'\s]+)'?") { return $Matches[1] }
  return $null
}

# The command the logon task runs: the idempotent re-join, then hold the distro.
function Get-KeepAliveArgs([string]$HubUrl) {
  return "-d $Distro -- sh -lc `"$Yard origin up --hub $HubUrl; exec sleep infinity`""
}

$ver = Get-DistroVersion
if ($ver -eq 'no-wsl') {
  Fail 2 "WSL is not installed. In an admin PowerShell: wsl --install -d $Distro ; reboot; open '$Distro' once to create your user; then re-run this script."
}
if ($null -eq $ver) {
  Fail 2 "WSL distro '$Distro' not found. Install it: wsl --install -d $Distro (or pass -Distro NAME; see: wsl --list --verbose)."
}
if ($ver -ne '2') {
  Fail 2 "WSL distro '$Distro' is WSL version $ver; Loopyard needs WSL2: wsl --set-version $Distro 2"
}

if ($Status) { exit (Invoke-InDistro "$Yard origin status") }
if ($Down)   { exit (Invoke-InDistro "$Yard origin down") }

if (-not $JoinLine -and -not ($KeepAlive -and $Hub)) {
  $JoinLine = Read-Host 'Paste the join line from the Hub (yard hub pair-code)'
}
if ($JoinLine) {
  $JoinLine = $JoinLine.Trim()
  if ($JoinLine -notmatch 'yard origin up' -or $JoinLine -notmatch '--hub\s') {
    Fail 2 'that is not a Loopyard join line (expected: printf ... | ~/loopyard/bin/yard origin up --hub ... --pair-code -).'
  }
  # Preflight the distro's own tools with the SAME messages `yard start` gives, but
  # before anything is claimed: a consumed code on a box that cannot run is waste.
  $pre = Invoke-InDistro @'
missing=""
for t in tmux git curl; do command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; done
if [ -n "$missing" ]; then echo "missing in WSL:$missing  ->  sudo apt-get update && sudo apt-get install -y$missing" >&2; exit 2; fi
command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] || { echo "claude CLI not found in WSL -> curl -fsSL https://claude.ai/install.sh | bash" >&2; exit 2; }
'@
  if ($pre -ne 0) { Fail 2 "the '$Distro' distro is not ready (see above)." }
  $rc = Invoke-InDistro $JoinLine
  if ($rc -ne 0) { Fail $rc "yard origin up failed in '$Distro' (exit $rc; the reason is printed above)." }
  if (-not $Hub) { $Hub = Get-HubFromLine $JoinLine }
}

if ($KeepAlive) {
  if (-not $Hub) { Fail 2 '-KeepAlive needs the Hub URL (from the join line or -Hub).' }
  $argsLine = Get-KeepAliveArgs $Hub
  if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    Write-Output "keep-alive (not registered: no Task Scheduler here): $Wsl $argsLine"
    exit 0
  }
  $action = New-ScheduledTaskAction -Execute $Wsl -Argument $argsLine
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
  Register-ScheduledTask -TaskName "Loopyard origin ($Distro)" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Write-Output "keep-alive registered: 'Loopyard origin ($Distro)' runs at logon: $Wsl $argsLine"
}
exit 0
