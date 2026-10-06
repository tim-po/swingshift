"""Run Cursor's own credential refresh before a task; never retry the task."""
import os
import shutil
import subprocess
import sys


def main():
    binary = os.environ.get("LOOPS_CURSOR_REAL_BIN") or shutil.which("cursor-agent")
    if not binary:
        print("cursor-agent is not installed; install it and run cursor-agent login", file=sys.stderr)
        return 127
    try:
        check = subprocess.run([binary, "status"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Cursor login check failed: {exc}", file=sys.stderr)
        return 1
    if check.returncode:
        print("Cursor login check failed; run cursor-agent login", file=sys.stderr)
        return 1
    os.execv(binary, [binary, *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
