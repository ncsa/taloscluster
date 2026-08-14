"""OpenStack connection + a per-run inventory cache.

The connection uses the same application-credential auth the terraform provider
and the shell script used (OS_AUTH_TYPE=v3applicationcredential, RegionOne).

Inventory is the performance heart of the rewrite: OpenStack API calls are
expensive, so instead of a `find_*` per resource we do ONE bulk `list` per
resource type up front, filtered to the tags this cluster owns, and index it by
name. Reconcile functions read from the cache and write freshly created/updated
objects back into it so later phases see them without a re-list.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import openstack
from openstack.connection import Connection

from .. import naming
from ..config import Config, Secrets

REGION = "RegionOne"

# Neutron resource kinds we cache (all support tags). Servers/volumes are Nova/
# Cinder and cached separately because their proxies differ.
_NETWORK_KINDS = ("networks", "subnets", "routers", "ports", "security_groups", "ips")


def connect(cfg: Config, secrets: Secrets) -> Connection:
    return openstack.connect(
        auth_type="v3applicationcredential",
        auth={
            "auth_url": cfg.openstack_url,
            "application_credential_id": secrets.openstack_credential_id,
            "application_credential_secret": secrets.openstack_credential_secret,
        },
        region_name=REGION,
    )


def _has_all_tags(obj: Any, required: Iterable[str]) -> bool:
    tags = set(getattr(obj, "tags", None) or [])
    return set(required).issubset(tags)


class Inventory:
    """Cache of the OpenStack resources tagged as belonging to this cluster."""

    def __init__(self, conn: Connection, cluster: str):
        self.conn = conn
        self.cluster = cluster
        self._filter = naming.base_tags(cluster)
        # kind -> {name -> object}
        self._by_name: dict[str, dict[str, Any]] = {}

    # -- population --------------------------------------------------------

    def load(self) -> Inventory:
        net = self.conn.network
        listers = {
            "networks": net.networks,
            "subnets": net.subnets,
            "routers": net.routers,
            "ports": net.ports,
            "security_groups": net.security_groups,
        }
        for kind, lister in listers.items():
            self._by_name[kind] = {
                o.name: o for o in lister() if _has_all_tags(o, self._filter) and o.name
            }
        # floating ips have no name field; network.tf keyed them by `description`,
        # so index them that way (and tag them for the ownership filter).
        self._by_name["ips"] = {
            o.description: o
            for o in net.ips()
            if _has_all_tags(o, self._filter) and o.description
        }
        # Nova servers (boot volume is managed by Nova via block-device-mapping
        # with delete_on_termination, so we don't track Cinder volumes -- they
        # also lack Neutron-style tags).
        self._by_name["servers"] = {
            s.name: s
            for s in self.conn.compute.servers(details=True)
            if _has_all_tags(s, self._filter) and s.name
        }
        return self

    # -- access ------------------------------------------------------------

    def get(self, kind: str, name: str) -> Any | None:
        return self._by_name.get(kind, {}).get(name)

    def all(self, kind: str) -> dict[str, Any]:
        return dict(self._by_name.get(kind, {}))

    def put(self, kind: str, obj: Any) -> Any:
        """Write a freshly created/updated object back into the cache."""
        self._by_name.setdefault(kind, {})[obj.name] = obj
        return obj

    def put_keyed(self, kind: str, key: str, obj: Any) -> Any:
        """Write back under an explicit key (floating ips have no name)."""
        self._by_name.setdefault(kind, {})[key] = obj
        return obj

    def drop(self, kind: str, name: str) -> None:
        self._by_name.get(kind, {}).pop(name, None)
