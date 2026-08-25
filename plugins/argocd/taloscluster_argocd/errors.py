"""Typed errors for the argocd plugin.

They subclass taloscluster's ReconcileError -- semantically "a resource could not
be converged to the desired state" -- so the CLI's existing handler catches them
and prints a clean message instead of a traceback.
"""

from __future__ import annotations

from taloscluster.errors import ReconcileError


class ApplyError(ReconcileError):
    """A kubectl apply / generate step failed."""
