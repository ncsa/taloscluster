"""Tiny logging + dry-run helpers, the heirs of bin/cluster.sh's log()/die().

`--dry-run` sets DRY_RUN true; state-changing code paths check `dry_run()` and
print what they *would* do via `action()` instead of doing it, mirroring the
DEBUG=echo behaviour of the shell script.
"""

from __future__ import annotations

import sys

_DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global _DRY_RUN
    _DRY_RUN = value


def dry_run() -> bool:
    return _DRY_RUN


def log(msg: str) -> None:
    """A phase banner, like the shell script's `log`."""
    print(f"\n==> {msg}", flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def action(msg: str) -> None:
    """Announce a state-changing action; prefixed [dry-run] when applicable."""
    prefix = "[dry-run] " if _DRY_RUN else ""
    print(f"    {prefix}{msg}", flush=True)


class Die(SystemExit):
    """Fatal error that cleanly aborts the CLI with a message + exit code 1."""

    def __init__(self, msg: str):
        super().__init__(f"ERROR: {msg}")
