"""What a plugin is handed: the cluster facts taloscluster already knows.

A plugin should never have to re-derive endpoint or infrastructure facts --
converge computed them a moment ago. `Context` carries them, plus the loaded
cluster.yaml and the paths to the derived client configs.

The expensive part (the status payload, which needs an OpenStack connection) is
either pre-filled by converge from the refs it already holds, or fetched lazily
on first access and cached. A plugin that never looks at infrastructure/ingress
(rancher does not) therefore costs nothing.

`results` is the channel between plugins: whatever a plugin's converge returns is
stored under its name before the next plugin runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, load_config


@dataclass
class Context:
    root: Path
    cfg: Config
    #  plugin name -> whatever its converge returned (empty for earlier plugins
    #  that were not installed, not configured, or failed)
    results: dict[str, Any] = field(default_factory=dict)
    # the `status` payload; None means "not fetched yet" (see _report)
    status: dict[str, Any] | None = None

    @classmethod
    def load(cls, root: Path) -> Context:
        """Standalone constructor: cluster.yaml only, status fetched on demand."""
        return cls(root=root, cfg=load_config(root))

    @classmethod
    def from_converge(cls, root: Path, cfg: Config, kubeapi: dict[str, str],
                      ingress: dict[str, str], openstack: dict[str, str],
                      infrastructure: dict[str, str] | None = None) -> Context:
        """In-converge constructor: the status payload is already known, so no
        plugin can trigger a second round-trip to OpenStack."""
        return cls(
            root=root,
            cfg=cfg,
            status={
                "cluster": cfg.name,
                "infrastructure": infrastructure or {
                    "provider": "openstack",
                    **openstack,
                },
                "openstack": openstack,
                "kubernetes": kubeapi,
                "ingress": ingress,
            },
        )

    # -- paths --------------------------------------------------------------

    @property
    def talosconfig(self) -> Path:
        return self.root / "talosconfig"

    @property
    def kubeconfig(self) -> Path:
        return self.root / "kubeconfig"

    # -- cluster facts ------------------------------------------------------

    def _report(self) -> dict[str, Any]:
        if self.status is None:
            # imported here, not at module scope: converge imports this module
            from . import converge

            self.status = converge.status_report(self.root)
        return self.status

    @property
    def infrastructure(self) -> dict[str, str]:
        """Provider-neutral infrastructure identity and provider details."""
        return dict(self._report().get("infrastructure") or {})

    @property
    def openstack(self) -> dict[str, str]:
        """url / region / project of the cloud this cluster lives in."""
        return dict(self._report().get("openstack") or {})

    @property
    def kubernetes(self) -> dict[str, str]:
        """floating_ip / vip / endpoint of the kube api."""
        return dict(self._report().get("kubernetes") or {})

    @property
    def ingress(self) -> dict[str, str]:
        """floating_ip / vip reserved for the ingress controller."""
        return dict(self._report().get("ingress") or {})
