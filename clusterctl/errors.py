"""Typed errors + preflight checks.

Preflight mirrors bin/cluster.sh's tool check, but the Python rewrite only needs
two external binaries (talosctl, kubectl); everything the shell did with yq / jq
/ curl / xz / openstack is done in-process via libraries.
"""

from __future__ import annotations

import shutil

# External binaries clusterctl shells out to. Unlike the shell script we do NOT
# need terraform, yq, jq, curl, xz, or the openstack CLI.
REQUIRED_TOOLS = ("talosctl", "kubectl")


class ConfigError(Exception):
    """cluster.yaml / secrets.yaml is missing or invalid."""


class StateError(Exception):
    """A problem with persisted local state (talossecrets.yaml)."""


class ReconcileError(Exception):
    """An OpenStack resource could not be converged to the desired state."""


class PreflightError(Exception):
    """A required external tool is missing."""


def preflight_tools(tools=REQUIRED_TOOLS) -> None:
    """Fail fast if a required external binary is not on PATH."""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise PreflightError("required command(s) not found: " + " ".join(missing))
