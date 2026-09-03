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

from openstack.connection import Connection

import openstack

from .. import naming
from ..config import Config, Secrets
from ..errors import ReconcileError

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


def project_name(conn: Connection) -> str:
    """Name of the OpenStack project the application credential is scoped to.

    Read from the keystone token itself (no extra API call, no identity-read
    permission needed) -- and it is the PROJECT name (e.g. "bbdb"), not the
    user name that owns the credential.
    """
    get_access = getattr(conn.session.auth, "get_access", None)
    if not callable(get_access):
        return ""
    return get_access(conn.session).project_name or ""


def _has_all_tags(obj: Any, required: Iterable[str], any_of: Iterable[str] = ()) -> bool:
    tags = set(getattr(obj, "tags", None) or [])
    if not set(required).issubset(tags):
        return False
    any_of = set(any_of)
    return not any_of or bool(tags & any_of)


class Inventory:
    """Cache of the OpenStack resources tagged as belonging to this cluster."""

    def __init__(self, conn: Connection, cluster: str):
        self.conn = conn
        self.cluster = cluster
        self._filter = [naming.tag_cluster(cluster)]
        # accept the pre-rename managed-by value too (see naming.LEGACY_MANAGED_BY)
        self._managed = naming.managed_tags()
        # kind -> {name -> object}
        self._by_name: dict[str, dict[str, Any]] = {}
        # kind -> names already used by resources this cluster does not own.
        # Checking these at get() prevents a crash-created untagged resource (or
        # a genuinely foreign collision) from being silently duplicated/adopted.
        self._foreign_names: dict[str, set[str]] = {}
        self._duplicate_names: dict[str, set[str]] = {}

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
            self._index(kind, lister(), key="name")
        # floating ips have no name field; network.tf keyed them by `description`,
        # so index them that way (and tag them for the ownership filter).
        self._index("ips", net.ips(), key="description")
        # Nova servers (boot volume is managed by Nova via block-device-mapping
        # with delete_on_termination, so we don't track Cinder volumes -- they
        # also lack Neutron-style tags).
        self._index("servers", self.conn.compute.servers(details=True), key="name")
        return self

    def _index(self, kind: str, objects: Iterable[Any], key: str) -> None:
        owned: dict[str, Any] = {}
        foreign: set[str] = set()
        duplicates: set[str] = set()
        for obj in objects:
            name = getattr(obj, key, None)
            if not name:
                continue
            if _has_all_tags(obj, self._filter, self._managed):
                if name in owned:
                    duplicates.add(name)
                owned[name] = obj
            else:
                foreign.add(name)
        self._by_name[kind] = owned
        self._foreign_names[kind] = foreign
        self._duplicate_names[kind] = duplicates

    # -- access ------------------------------------------------------------

    def get(self, kind: str, name: str) -> Any | None:
        if name in self._duplicate_names.get(kind, set()):
            raise ReconcileError(
                f"multiple managed {kind} resources are named {name!r}; "
                "refusing an ambiguous reconciliation"
            )
        if name in self._foreign_names.get(kind, set()):
            raise ReconcileError(
                f"{kind} resource {name!r} exists without this cluster's ownership tags; "
                "refusing to adopt it or create a duplicate"
            )
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
