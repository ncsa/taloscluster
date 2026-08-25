"""Typed errors for the rancher plugin.

They subclass taloscluster's ReconcileError -- semantically "a resource could not
be converged to the desired state" -- so the CLI's existing handler catches them
and prints a clean message instead of a traceback.
"""

from __future__ import annotations

from taloscluster.errors import ReconcileError


class RancherError(ReconcileError):
    """An API call to Rancher failed."""


class NotFoundError(RancherError):
    """An expected Rancher resource (user, cluster, binding) was not found."""
