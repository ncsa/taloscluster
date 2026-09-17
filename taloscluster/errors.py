"""Typed errors + preflight checks.

The preflight check only needs two external binaries (talosctl, kubectl);
everything else is done in-process via libraries.
"""

from __future__ import annotations

import shutil

# The only external binaries taloscluster shells out to; everything else is
# handled by libraries at runtime.
REQUIRED_TOOLS = ("talosctl", "kubectl")


class ConfigError(Exception):
    """cluster.yaml / secrets.yaml is missing or invalid."""


class StateError(Exception):
    """A problem with persisted local state (talossecrets.yaml)."""


class ReconcileError(Exception):
    """A provider resource could not be converged to the desired state."""


class PreflightError(Exception):
    """A required external tool is missing."""


def preflight_tools(tools=REQUIRED_TOOLS) -> None:
    """Fail fast if a required external binary is not on PATH."""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise PreflightError("required command(s) not found: " + " ".join(missing))
