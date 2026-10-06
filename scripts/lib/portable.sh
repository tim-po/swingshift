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
