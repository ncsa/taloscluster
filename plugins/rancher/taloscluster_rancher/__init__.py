"""taloscluster rancher plugin -- register the cluster and reconcile its members.

Reads the `rancher:` section of cluster.yaml (admins/users as NCSA netid lists)
plus rancher.url / rancher.token from secrets.yaml, and makes Rancher match: the
cluster is imported if missing (agent installed via kubectl), and every member is
added with the right role (admin -> cluster-owner, user -> cluster-member).

Installed as `taloscluster[rancher]`; taloscluster discovers it through the
`taloscluster.plugins` entry point and runs it as part of converge / plan /
destroy / status / check. This module is the entry point, so it re-exports the
plugin protocol.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from taloscluster.context import Context

from .config import rancher_configured
from .reconcile import check, converge, destroy, status

try:
    __version__ = version("taloscluster-rancher")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"

# nothing to wait for: rancher runs first and hands its cluster id to whoever
# declares AFTER = ("rancher",)
AFTER: tuple[str, ...] = ()

__all__ = ["AFTER", "check", "configured", "converge", "destroy", "status"]


def configured(ctx: Context) -> bool:
    """True only when BOTH cluster.yaml and secrets.yaml carry a `rancher:`
    section (the latter with url + token). A missing section in either means this
    cluster is not managed by Rancher and the plugin does nothing."""
    return rancher_configured(ctx.root)
