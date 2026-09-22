"""Tiny logging + dry-run helpers.

`--dry-run` sets DRY_RUN true; state-changing code paths check `dry_run()` and
print what they *would* do via `action()` instead of doing it.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from typing import Any

import yaml

_DRY_RUN = False

# a mapping key that looks like it holds a credential, at any depth
SECRET_KEY_RE = re.compile(r"(?i)pass|token|secret|key|credential")


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


def redact(data: Any, extra_keys: Iterable[str] = ()) -> Any:
    """Display copy of `data` with credential values replaced by REDACTED.

    Two rules, so a preview never leaks what converge would apply. A key that
    looks like a credential (password/token/secret/key/credential, or one of
    `extra_keys`) is masked wherever it sits. A Kubernetes Secret has every
    value under `data` and `stringData` masked whatever the keys are called
    (`cloud.conf`, `config`, `userID`), since the block is secret as a whole.
    Display only -- the real values still reach helm and kubectl.
    """
    extra = set(extra_keys)

    def walk(node: Any, in_secret_data: bool = False) -> Any:
        if isinstance(node, dict):
            is_secret = node.get("kind") == "Secret"
            out: dict = {}
            for key, value in node.items():
                if in_secret_data or SECRET_KEY_RE.search(str(key)) or key in extra:
                    out[key] = "REDACTED"
                else:
                    out[key] = walk(value, is_secret and key in ("data", "stringData"))
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(data)


def show_yaml(doc: str | dict, extra_keys: Iterable[str] = ()) -> None:
    """Print a manifest (YAML text, possibly multi-document) or a values mapping
    as an indented, redacted preview under the current dry-run action line."""
    documents = list(yaml.safe_load_all(doc)) if isinstance(doc, str) else [doc]
    text = yaml.safe_dump_all(
        [redact(d, extra_keys) for d in documents if d is not None],
        explicit_start=isinstance(doc, str),
        default_flow_style=False,
    )
    for line in text.rstrip().splitlines():
        info("      " + line)


def report(data, indent: str = "") -> None:
    """Render a nested dict/list as indented `key: value` lines.

    Plugins return data, never text, so every plugin's status/check report is
    printed the same way and a plugin never has to know about text-vs-yaml
    output.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                info(f"{indent}{key}:")
                report(value, indent + "    ")
            else:
                info(f"{indent}{key}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                report(item, indent)
            else:
                info(f"{indent}- {item}")
    else:
        info(f"{indent}{data}")


class Die(SystemExit):
    """Fatal error that cleanly aborts the CLI with a message + exit code 1."""

    def __init__(self, msg: str):
        super().__init__(f"ERROR: {msg}")
