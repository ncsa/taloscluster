"""Proxmox compute backend using existing bridges or VNets."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import shlex
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import naming
from ..config import (
    Config,
    Machine,
    ProxmoxConfig,
    ProxmoxSdn,
    ProxmoxSecrets,
    Secrets,
    proxmox_sdn,
)
from ..errors import ConfigError, ReconcileError
from ..infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
    TalosContribution,
)
from ..output import action, dry_run, info, warn
from ..talos import factory
from . import cidata, talos
from .client import ProxmoxClient
from .inventory import (
    ProxmoxInventory,
    ProxmoxPool,
    ProxmoxVM,
    is_owned,
    load,
    owned_tags,
)
from .permissions import requirements, validate_effective_permissions
from .placement import place

_MIB_PER_GB = 1024
# Marks the per-VM firewall rules this tool wrote. A VM's firewall is shared with
# whoever else administers it, so ownership has to be visible in the rule itself:
# rules without this marker are never deleted.
_FIREWALL_MARKER = "taloscluster: "
# Tagged on a VM whose Proxmox disk was grown but not yet rebooted, so Talos has
# not extended its EPHEMERAL partition. Unlike pending cores/memory (which Proxmox
# keeps as pending until the next start), a grown disk shows the new size in the
# live config immediately, so the un-rebooted grow would otherwise be invisible on
# the next converge. The tag is written on the grow and cleared on the Proxmox reboot.
_RESIZE_TAG = "taloscluster-pending-resize"
# (proto, destination port or None, source CIDR or None)
_FirewallKey = tuple[str, int | None, str | None]
# How long to keep waiting for the SDN VNet bridge to appear on every node after
# applying SDN. The apply task can return before each node's network reload finishes.
_SDN_BRIDGE_DEADLINE = 60.0


def _memory_mib(memory_gb: int) -> int:
    """Convert the whole-GB cluster.yaml value to Proxmox's MiB API unit."""
    return memory_gb * _MIB_PER_GB


class ProxmoxBackend:
    name = "proxmox"
    installer_platform = talos.INSTALLER_PLATFORM

    def __init__(self, cfg: Config, secrets: Secrets, client: ProxmoxClient | None = None):
        if not isinstance(cfg.provider, ProxmoxConfig):
            raise ConfigError("Proxmox backend requires proxmox configuration")
        if not isinstance(secrets.provider, ProxmoxSecrets):
            raise ConfigError("Proxmox backend requires proxmox credentials")
        self.cfg = cfg
        self.provider = cfg.provider
        self.secrets = secrets.provider
        self.client = client or ProxmoxClient(
            self.provider.url,
            self.secrets.token_id,
            self.secrets.token_secret,
            verify=self.provider.tls_verify,
        )
        self.sdn: ProxmoxSdn | None = proxmox_sdn(cfg.name, self.provider)
        self._inventory: ProxmoxInventory | None = None
        self._sdn_cache: dict[str, Any] | None = None
        self._preflight_complete = False
        self._compute_nodes: tuple[str, ...] = ()
        self._anchors_checked = False

    def talos_contribution(
        self, machine: Machine, endpoint: Endpoint
    ) -> TalosContribution:
        # Reject anchor collisions across the whole cluster before any machine
        # config is rendered, not once the Nth host happens to clash. The check
        # covers every machine, so run it once however many times we are called.
        if self.external_network and not self._anchors_checked:
            talos.anchor_addresses(
                self.external_network["anchor_cidr"], self.cfg.name, self.cfg.machines
            )
            self._anchors_checked = True
        return talos.contribution(machine, self.cfg, endpoint)

    @property
    def pool_id(self) -> str:
        return f"taloscluster-{self.cfg.name}"

    @property
    def pool_comment(self) -> str:
        return f"managed-by=taloscluster cluster={self.cfg.name}"

    @property
    def cluster_network(self) -> dict[str, Any]:
        value = self.provider.network.get("cluster")
        return value if isinstance(value, dict) else {}

    @property
    def cluster_link(self) -> str:
        """The bridge/VNet name a VM's private NIC attaches to."""
        if self.sdn:
            return self.sdn.name
        return str(self.cluster_network.get("bridge") or self.cluster_network.get("vnet"))

    @property
    def external_network(self) -> dict[str, Any]:
        """The external L2 facts (merged from both locations) plus the bridge."""
        return talos.external_network(self.cfg)

    def _raw_inventory(self, *, refresh: bool = False) -> ProxmoxInventory:
        if refresh or self._inventory is None:
            self._preflight_complete = False
            self._inventory = load(self.client)
            self._refuse_duplicate_names(self._inventory)
            self._validate_environment(self._inventory)
            self._validate_permissions(self._inventory)
        return self._inventory

    def _refuse_duplicate_names(self, inventory: ProxmoxInventory) -> None:
        """Reject two VMs sharing a name when a managed one is involved.

        The inventory keys machines by VM name, so when two VMIDs share a name
        the later entry overwrites the earlier in ``vms``. A later unowned VM
        would hide an owned one -- reading as an empty inventory to the
        Talos-identity guard (secrets are then generated for, or destroy wipes
        identity for, a cluster that still has managed VMs). Two owned VMs
        colliding is equally ambiguous: whichever the list order keeps, the
        other managed machine is invisible. Reject the ambiguity up front rather
        than gamble on the ``cluster/resources`` order; two VMs sharing a name
        where neither is managed by this cluster is left alone.
        """
        ambiguous = [
            name
            for name, group in inventory.vm_collisions.items()
            if any(self._owns_vm(inventory, vm) for vm in group)
        ]
        if ambiguous:
            raise ReconcileError(
                "duplicate Proxmox VM names among "
                f"cluster-managed machines: {', '.join(sorted(ambiguous))}; "
                "the inventory keys machines by name, so rename the VMs so "
                "every managed name is unique"
            )

    def _validate_environment(self, inventory: ProxmoxInventory) -> None:
        online = {name for name, node in inventory.nodes.items() if node.online}
        required_nodes = set(self.provider.nodes)
        offline = sorted(required_nodes - online)
        if offline:
            raise ReconcileError(
                "configured Proxmox nodes are missing or offline: " + ", ".join(offline)
            )
        usable = set(online)
        for storage, content_type in (
            (self.provider.storage, "images"),
            (self.provider.iso_storage, "iso"),
            (self.provider.cidata_storage, "iso"),
        ):
            if storage not in inventory.storages:
                raise ReconcileError(f"Proxmox storage {storage!r} was not found")
            storage_data = inventory.storages[storage]
            if _truthy(storage_data.get("disable")):
                raise ReconcileError(f"Proxmox storage {storage!r} is disabled")
            content = {item.strip() for item in str(storage_data.get("content") or "").split(",")}
            if content_type not in content:
                raise ReconcileError(
                    f"Proxmox storage {storage!r} does not support {content_type!r} content"
                )
            usable.intersection_update(_storage_nodes(storage_data, online))
        if _truthy(inventory.storages[self.provider.cidata_storage].get("shared")):
            raise ReconcileError("proxmox.cidata_storage must be node-local, not shared")
        required = set(self.provider.nodes)
        inaccessible = sorted(required - usable)
        if inaccessible:
            raise ReconcileError(
                "configured Proxmox nodes cannot access every required storage: "
                + ", ".join(inaccessible)
            )
        self._compute_nodes = tuple(sorted(required or usable))
        if not self._compute_nodes:
            raise ReconcileError("no online Proxmox node can access every required storage")
        self._check_firewall(inventory)

    def _check_firewall(self, inventory: ProxmoxInventory) -> None:
        """Warn if the Proxmox firewall is not fully enabled.

        Proxmox requires three levels of enablement for VM firewall rules to
        take effect: cluster-wide, per-VM, and per-NIC.  We set the VM and NIC
        flags ourselves during creation, but the cluster-wide switch is
        operator-controlled.  Warn if it is off, and warn if existing owned VMs
        are missing NIC firewall flags (e.g. created before this was enforced).
        """
        if not _truthy(inventory.firewall_options.get("enable")):
            warn(
                "Proxmox cluster firewall is not enabled — "
                "security allowlists are NOT enforced"
            )
        for name, vm in inventory.vms.items():
            if not self._owns_vm(inventory, vm):
                continue
            try:
                config = self.client.get(f"nodes/{vm.node}/qemu/{vm.vmid}/config")
            except ReconcileError:
                continue
            if not isinstance(config, dict):
                continue
            for key in sorted(config):
                if not key.startswith("net"):
                    continue
                net = config[key]
                if isinstance(net, str) and "firewall=1" not in net:
                    warn(
                        f"VM {name} {key} does not have firewall=1 — "
                        "allowlists not enforced on this interface"
                    )

    def _validate_permissions(self, inventory: ProxmoxInventory) -> None:
        nodes = self._compute_nodes
        if self.sdn:
            network_path = f"/sdn/vnets/{self.sdn.name}"
        elif self.cluster_network.get("vnet"):
            network_path = f"/sdn/vnets/{self.cluster_network['vnet']}"
        else:
            network_path = "/sdn/zones/localnetwork"
        vmids = [
            vm.vmid for vm in inventory.vms.values() if self._owns_vm(inventory, vm)
        ]
        validate_effective_permissions(
            inventory.permissions,
            requirements(
                iso_storage=self.provider.iso_storage,
                cidata_storage=self.provider.cidata_storage,
                vm_storage=self.provider.storage,
                nodes=nodes,
                network_path=network_path,
                vmids=vmids,
                manage_sdn=self.sdn is not None,
            ),
        )
        self._preflight_complete = True

    def _require_preflight(self) -> ProxmoxInventory:
        inventory = self._raw_inventory()
        if not self._preflight_complete:
            raise ReconcileError("Proxmox permission preflight did not complete")
        return inventory

    def load_inventory(self) -> InfrastructureInventory:
        raw = self._raw_inventory(refresh=True)
        machines: dict[str, InfrastructureMachine] = {}
        for name, vm in raw.vms.items():
            if not self._owns_vm(raw, vm):
                continue
            address = self._guest_address(vm) if vm.status == "running" else ""
            attachments = (
                (NetworkAttachment(name="cluster", address=address),) if address else ()
            )
            machines[name] = InfrastructureMachine(
                name=name,
                provider_id=str(vm.vmid),
                attachments=attachments,
            )
        return InfrastructureInventory(
            machines=machines,
            resources={
                "nodes": sorted(name for name, node in raw.nodes.items() if node.online),
                "storages": sorted(raw.storages),
                "pools": sorted(
                    pool.poolid
                    for pool in raw.pools.values()
                    if pool.comment == self.pool_comment
                ),
                "vms": sorted(machines),
            },
            provider_data=raw,
        )

    @staticmethod
    def _raw(inventory: InfrastructureInventory) -> ProxmoxInventory:
        raw = inventory.provider_data
        if not isinstance(raw, ProxmoxInventory):
            raise RuntimeError("Proxmox inventory is unavailable")
        return raw

    def ensure_boot_artifact(self) -> str:
        inventory = self._require_preflight()
        schematic = factory.schematic_id(naming.BASE_EXTENSIONS)
        filename = _boot_iso_name(self.cfg.talos_version, schematic)
        nodes = self._iso_nodes(inventory)
        volumes = {
            node: self._find_iso(node, self.provider.iso_storage, filename) for node in nodes
        }
        if all(volumes.values()):
            info(f"image {filename} exists")
            return next(iter(volumes.values()))

        action(f"download image {filename} to {self.provider.iso_storage}")
        expected = f"{self.provider.iso_storage}:iso/{filename}"
        if dry_run():
            return expected
        url = factory.nocloud_iso_url(schematic, self.cfg.talos_version)
        for node, volume in volumes.items():
            if not volume:
                self._download_iso(node, self.provider.iso_storage, url, filename)
        return self._find_iso(nodes[0], self.provider.iso_storage, filename) or expected

    def reconcile_network(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> NetworkResult:
        if self.sdn:
            self._require_preflight()
            self._check_static_addresses(machines, inventory)
            self._reconcile_sdn()
        return self.current_network(inventory)

    def current_network(self, inventory: InfrastructureInventory) -> NetworkResult:
        ext = self.external_network
        vip = str(ext.get("kubeapi_vip") or self.cfg.network.cluster.kubeapi_vip or "")
        attachments: dict[str, tuple[NetworkAttachment, ...]] = {}
        if self.sdn:
            # static addresses are pure config, so plan/dry-run can resolve
            # node addresses without the guest agent
            worker_pools = tuple(self.cfg.workers)
            attachments = {
                m.name: (
                    NetworkAttachment(
                        name="cluster",
                        address=str(
                            naming.node_address(
                                self.cfg.cidr, m.name, m.role, m.pool, worker_pools
                            ).ip
                        ),
                    ),
                )
                for m in self.cfg.machines.values()
            }
        ingress_pool = str(ext.get("ingress_pool") or "")
        return NetworkResult(
            kubernetes=Endpoint(vip=vip, advertised_address=vip),
            metallb=(ingress_pool,) if ingress_pool else (),
            machine_attachments=attachments,
        )

    # ---- managed SDN (EVPN zone + VNet + subnet) ----------------------------

    def _sdn_state(self, *, refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Zones, VNets, and controllers, with pending changes included.

        Read lazily after the permission preflight, never in inventory load(),
        so a token without SDN privileges gets the clean missing-permission
        error instead of a raw 403.
        """
        if refresh:
            self._sdn_cache = None
        if self._sdn_cache is None:
            state: dict[str, list[dict[str, Any]]] = {}
            for key, path in (
                ("zones", "cluster/sdn/zones"),
                ("vnets", "cluster/sdn/vnets"),
                ("controllers", "cluster/sdn/controllers"),
            ):
                value = self.client.get(path, params={"pending": 1})
                if not isinstance(value, list):
                    raise ReconcileError(f"Proxmox returned no SDN {key} list: {value!r}")
                state[key] = [item for item in value if isinstance(item, dict)]
            self._sdn_cache = state
        return self._sdn_cache

    @staticmethod
    def _sdn_effective(item: dict[str, Any]) -> dict[str, Any]:
        """The object's configuration with staged (pending) values merged in."""
        merged = dict(item)
        pending = item.get("pending")
        if isinstance(pending, dict):
            merged.update(pending)
        return merged

    def _owns_vnet(self, item: dict[str, Any]) -> bool:
        alias = str(self._sdn_effective(item).get("alias") or "")
        return alias == naming.sdn_alias(self.cfg.name)

    def _refuse_foreign_pending(self, state: dict[str, list[dict[str, Any]]]) -> None:
        """Applying SDN is cluster-wide: never deploy another admin's staged edits.

        Our own pending objects (a converge that crashed between staging and
        apply) are resumable and do not refuse.
        """
        assert self.sdn is not None
        ours = {self.sdn.name}
        foreign: list[str] = []
        for kind, id_key in (
            ("zones", "zone"),
            ("vnets", "vnet"),
            ("controllers", "controller"),
        ):
            for item in state[kind]:
                if not item.get("state"):
                    continue
                identifier = str(item.get(id_key) or "")
                if kind == "controllers" and identifier == self.sdn.controller:
                    continue
                if kind != "controllers" and identifier in ours:
                    continue
                foreign.append(f"{id_key} {identifier}")
        # subnets live under per-vnet endpoints, so pending subnet edits are
        # invisible in the zone/vnet listings and have to be scanned separately
        our_vnet = self.sdn.name
        for item in state["vnets"]:
            vnet_id = str(item.get("vnet") or "")
            if str(item.get("state") or "") == "new":
                continue  # a never-applied vnet is already reported above
            for subnet in self._sdn_subnets(vnet_id):
                if not subnet.get("state") and not isinstance(subnet.get("pending"), dict):
                    continue
                cidr = str(self._sdn_effective(subnet).get("cidr") or "")
                if vnet_id == our_vnet and cidr == self.cfg.cidr:
                    continue
                foreign.append(f"subnet {subnet.get('subnet')}")
        if foreign:
            raise ReconcileError(
                "unapplied Proxmox SDN changes exist ("
                + ", ".join(sorted(foreign))
                + "); applying SDN is cluster-wide, so apply or revert them first"
            )

    def _refuse_own_destructive_pending(
        self, state: dict[str, list[dict[str, Any]]]
    ) -> None:
        """A pending `new` is the only safe leftover of an interrupted create.

        Our own ids are skipped by the foreign-pending scan as resumable, but
        that only holds for pending `new` state. A staged `deleted` or
        `changed` on our zone, VNet, subnet, or the shared controller would
        otherwise be committed by the apply, tearing down a network that
        running VMs depend on.
        """
        assert self.sdn is not None

        def _pending_state(item: dict[str, Any]) -> str:
            return str(item.get("state") or "") if item.get("state") else ""

        dangerous: list[str] = []

        def _scan(kind: str, id_key: str, identifier: str) -> None:
            for item in state[kind]:
                if str(item.get(id_key)) != identifier:
                    continue
                pending = _pending_state(item)
                if pending and pending != "new":
                    dangerous.append(f"{id_key} {identifier} ({pending})")

        _scan("zones", "zone", self.sdn.name)
        _scan("vnets", "vnet", self.sdn.name)

        shared = self._shared_controller_destructive_pending(state)
        if shared:
            dangerous.append(shared)

        for item in state["vnets"]:
            if str(item.get("vnet")) != self.sdn.name:
                continue
            for subnet in self._sdn_subnets_of(item):
                if str(self._sdn_effective(subnet).get("cidr") or "") != self.cfg.cidr:
                    continue
                pending = _pending_state(subnet)
                if not pending and isinstance(subnet.get("pending"), dict):
                    pending = "changed"
                if pending and pending != "new":
                    dangerous.append(f"subnet {self.cfg.cidr} ({pending})")

        if dangerous:
            raise ReconcileError(
                "refusing to resume pending SDN state on the cluster's own "
                "resources ("
                + ", ".join(sorted(dangerous))
                + "); only `new` can be a leftover of an interrupted create"
            )

    def _refuse_shared_controller_destructive_pending(
        self, state: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Teardown never deletes the shared controller, so its staged edits are unsafe.

        Unlike the foreign-pending scan, which exempts our controller by name,
        a pending `deleted` or `changed` on that shared object would be
        committed by teardown's cluster-wide `PUT cluster/sdn`, disrupting
        other clusters that use it. Only `new` can be a resumable leftover.
        """
        assert self.sdn is not None
        shared = self._shared_controller_destructive_pending(state)
        if shared:
            raise ReconcileError(
                "refusing to commit pending SDN state on the shared "
                f"{shared}; teardown never deletes the controller and its "
                "staged edits are cluster-wide, so apply or revert them first"
            )

    def _shared_controller_destructive_pending(
        self, state: dict[str, list[dict[str, Any]]]
    ) -> str | None:
        """Detect a pending `deleted`/`changed` on the shared controller.

        Shared by the converge and teardown guards so they cannot drift: the
        shared controller is the one object both the own-scan (resumable) and
        the teardown rule treat specially, and only `new` is a resumable
        leftover there as anywhere else. Returns a descriptor of the offending
        object, or `None` when the shared controller bears no pending edits.
        """
        assert self.sdn is not None
        for item in state["controllers"]:
            if str(item.get("controller")) != self.sdn.controller:
                continue
            pending = str(item.get("state") or "")
            if pending and pending != "new":
                return f"controller {self.sdn.controller} ({pending})"
        return None

    def _refuse_sdn_destroy(self) -> None:
        """Refuse teardown before any mutation when SDN apply would be unsafe.

        Run at the top of destroy so a foreign-pending or shared-controller
        refusal happens before VMs and the pool are deleted, and from the
        summary so a plan/dry-run reports it instead of showing a teardown it
        would refuse to perform.
        """
        if not self.sdn:
            return
        state = self._sdn_state(refresh=True)
        self._refuse_foreign_pending(state)
        self._refuse_shared_controller_destructive_pending(state)

    def _refuse_vni_collisions(self, state: dict[str, list[dict[str, Any]]]) -> None:
        assert self.sdn is not None
        ours = {str(self.sdn.vrf_tag), str(self.sdn.tag)}
        for item in state["zones"]:
            if str(item.get("zone")) == self.sdn.name:
                continue
            vni = str(self._sdn_effective(item).get("vrf-vxlan") or "")
            if vni in ours:
                raise ReconcileError(
                    f"SDN VNI {vni} is already used by zone {item.get('zone')!r}; "
                    "set explicit vrf_tag/tag"
                )
        for item in state["vnets"]:
            if str(item.get("vnet")) == self.sdn.name:
                continue
            vni = str(self._sdn_effective(item).get("tag") or "")
            if vni in ours:
                raise ReconcileError(
                    f"SDN VNI {vni} is already used by vnet {item.get('vnet')!r}; "
                    "set explicit vrf_tag/tag"
                )

    def _refuse_foreign_ownership(self, state: dict[str, list[dict[str, Any]]]) -> None:
        """Fail before anything is staged when our ids exist but are not ours.

        The zone and VNet are named after the cluster, so a collision with an
        operator's same-named object is plausible; refuse before the controller
        or anything else gets staged, not midway through reconciliation.
        """
        assert self.sdn is not None
        vnet_id = self.sdn.name
        vnet = next(
            (item for item in state["vnets"] if str(item.get("vnet")) == vnet_id), None
        )
        if vnet is not None and not self._owns_vnet(vnet):
            raise ReconcileError(f"refusing to adopt unowned SDN vnet {vnet_id!r}")
        zone_id = self.sdn.name
        zone = next(
            (item for item in state["zones"] if str(item.get("zone")) == zone_id), None
        )
        if zone is None:
            return
        effective = self._sdn_effective(zone)
        if str(effective.get("type") or "") != "evpn":
            raise ReconcileError(
                f"refusing to adopt SDN zone {zone_id!r}: it is not an EVPN zone"
            )
        zone_vnets = [
            item
            for item in state["vnets"]
            if str(self._sdn_effective(item).get("zone")) == zone_id
        ]
        foreign = sorted(
            str(item.get("vnet")) for item in zone_vnets if not self._owns_vnet(item)
        )
        if foreign:
            raise ReconcileError(
                f"refusing to adopt SDN zone {zone_id!r} containing foreign VNets: "
                + ", ".join(foreign)
            )
        if not zone_vnets and not self._zone_matches_ours(effective):
            raise ReconcileError(f"refusing to adopt empty unowned SDN zone {zone_id!r}")

    def _sdn_exit_nodes(self) -> tuple[tuple[str, ...], str]:
        assert self.sdn is not None
        nodes = self.sdn.exit_nodes
        if not nodes:
            # every cluster node, offline included: an online-only default
            # would drift (and flip the SNAT primary) whenever a node is down
            raw = self._raw_inventory()
            nodes = tuple(sorted(raw.nodes))
        if not nodes:
            raise ReconcileError("no Proxmox nodes available as EVPN exit nodes")
        primary = self.sdn.primary_exit_node or nodes[0]
        raw = self._raw_inventory()
        offline = sorted(
            name
            for name in nodes
            if (node := raw.nodes.get(name)) is not None and not node.online
        )
        if offline:
            warn(f"offline EVPN exit nodes: {', '.join(offline)}")
        if primary in offline:
            warn(f"primary EVPN exit node {primary} is offline; SNAT egress will fail")
        return nodes, primary

    def _desired_zone(self) -> dict[str, Any]:
        assert self.sdn is not None
        exit_nodes, primary = self._sdn_exit_nodes()
        desired: dict[str, Any] = {
            "controller": self.sdn.controller,
            "vrf-vxlan": self.sdn.vrf_tag,
            "exitnodes": ",".join(exit_nodes),
            # SNAT forwards through the primary exit node; without one, egress
            # from the overlay does not work
            "exitnodes-primary": primary,
            # advertise subnet prefixes as Type-5 routes so traffic can reach
            # the overlay at all
            "advertise-subnets": 1,
            # the Layer 2 API VIP moves its MAC between owners; ARP/ND
            # suppression would pin it to a stale entry
            "disable-arp-nd-suppression": 1,
        }
        if self.sdn.mtu is not None:
            desired["mtu"] = self.sdn.mtu
        if self.sdn.nodes:
            desired["nodes"] = ",".join(self.sdn.nodes)
        return desired

    @staticmethod
    def _sdn_drift(current: dict[str, Any], desired: dict[str, Any]) -> list[str]:
        """Desired keys whose effective current value differs.

        Proxmox reads echo effective values (node lists in arbitrary order,
        booleans as 0/1), so compare per field rather than dict-equal or a
        steady-state converge would re-stage every run.
        """
        drift = []
        for key, value in desired.items():
            have = current.get(key)
            if key in ("exitnodes", "nodes"):
                if _node_set(have) != _node_set(value):
                    drift.append(key)
            elif str(have if have is not None else "") != str(value):
                drift.append(key)
        return sorted(drift)

    def _ensure_controller(self, state: dict[str, list[dict[str, Any]]]) -> bool:
        assert self.sdn is not None
        found = next(
            (
                item
                for item in state["controllers"]
                if str(item.get("controller")) == self.sdn.controller
            ),
            None,
        )
        if found is not None:
            # shared infrastructure: use as-is, never update or delete
            if str(self._sdn_effective(found).get("type") or "") != "evpn":
                raise ReconcileError(
                    f"SDN controller {self.sdn.controller!r} exists but is not an EVPN controller"
                )
            current_asn = self._sdn_effective(found).get("asn")
            if current_asn is not None and str(current_asn) != str(self.sdn.asn):
                warn(
                    f"SDN controller {self.sdn.controller} has ASN {current_asn} "
                    f"(configured {self.sdn.asn}); using the existing controller"
                )
            return False
        action(f"create SDN controller {self.sdn.controller} (evpn, asn {self.sdn.asn})")
        if dry_run():
            return True
        status = self.client.get("cluster/status")
        peers = sorted(
            str(item["ip"])
            for item in (status if isinstance(status, list) else [])
            if isinstance(item, dict) and item.get("type") == "node" and item.get("ip")
        )
        if not peers:
            raise ReconcileError(
                "could not determine Proxmox node addresses for EVPN controller peers"
            )
        self.client.mutate(
            "POST",
            "cluster/sdn/controllers",
            data={
                "controller": self.sdn.controller,
                "type": "evpn",
                "asn": self.sdn.asn,
                "peers": ",".join(peers),
            },
        )
        return True

    def _ensure_zone(self, state: dict[str, list[dict[str, Any]]]) -> bool:
        assert self.sdn is not None
        zone_id = self.sdn.name
        desired = self._desired_zone()
        existing = next(
            (item for item in state["zones"] if str(item.get("zone")) == zone_id), None
        )
        if existing is None:
            action(f"create SDN zone {zone_id} (evpn, vrf {self.sdn.vrf_tag})")
            if not dry_run():
                self.client.mutate(
                    "POST",
                    "cluster/sdn/zones",
                    data={"zone": zone_id, "type": "evpn", **desired},
                )
            return True
        effective = self._sdn_effective(existing)
        if str(effective.get("type") or "") != "evpn":
            raise ReconcileError(
                f"refusing to adopt SDN zone {zone_id!r}: it is not an EVPN zone"
            )
        zone_vnets = [
            item
            for item in state["vnets"]
            if str(self._sdn_effective(item).get("zone")) == zone_id
        ]
        foreign = sorted(
            str(item.get("vnet")) for item in zone_vnets if not self._owns_vnet(item)
        )
        if foreign:
            raise ReconcileError(
                f"refusing to adopt SDN zone {zone_id!r} containing foreign VNets: "
                + ", ".join(foreign)
            )
        if not zone_vnets and not self._zone_matches_ours(effective):
            # empty + matching controller/vrf is an interrupted create we
            # resume; an id match alone is not ownership
            raise ReconcileError(f"refusing to adopt empty unowned SDN zone {zone_id!r}")
        drift = self._sdn_drift(effective, desired)
        if not drift:
            info(f"SDN zone {zone_id} exists")
            return False
        action(f"update SDN zone {zone_id} ({', '.join(drift)})")
        if not dry_run():
            self.client.mutate("PUT", f"cluster/sdn/zones/{zone_id}", data=desired)
        return True

    def _zone_matches_ours(self, effective: dict[str, Any]) -> bool:
        assert self.sdn is not None
        return (
            str(effective.get("controller") or "") == self.sdn.controller
            and str(effective.get("vrf-vxlan") or "") == str(self.sdn.vrf_tag)
        )

    def _ensure_vnet(self, state: dict[str, list[dict[str, Any]]]) -> bool:
        assert self.sdn is not None
        zone_id = self.sdn.name
        vnet_id = self.sdn.name
        desired: dict[str, Any] = {
            "zone": zone_id,
            "tag": self.sdn.tag,
            "alias": naming.sdn_alias(self.cfg.name),
        }
        existing = next(
            (item for item in state["vnets"] if str(item.get("vnet")) == vnet_id), None
        )
        if existing is None:
            action(f"create SDN vnet {vnet_id} (tag {self.sdn.tag})")
            if not dry_run():
                self.client.mutate(
                    "POST", "cluster/sdn/vnets", data={"vnet": vnet_id, **desired}
                )
            return True
        if not self._owns_vnet(existing):
            raise ReconcileError(f"refusing to adopt unowned SDN vnet {vnet_id!r}")
        drift = self._sdn_drift(self._sdn_effective(existing), desired)
        if not drift:
            info(f"SDN vnet {vnet_id} exists")
            return False
        action(f"update SDN vnet {vnet_id} ({', '.join(drift)})")
        if not dry_run():
            self.client.mutate("PUT", f"cluster/sdn/vnets/{vnet_id}", data=desired)
        return True

    def _sdn_subnets(self, vnet_id: str) -> list[dict[str, Any]]:
        value = self.client.get(
            f"cluster/sdn/vnets/{vnet_id}/subnets", params={"pending": 1}
        )
        if not isinstance(value, list):
            raise ReconcileError(f"Proxmox returned no SDN subnet list: {value!r}")
        return [item for item in value if isinstance(item, dict)]

    def _sdn_subnets_of(self, vnet_item: dict[str, Any]) -> list[dict[str, Any]]:
        """Subnets of a vnet, tolerating a vnet that was staged but never applied.

        Proxmox may 404 the subnet endpoint of a pending-new vnet; failing on
        that would strand the very interrupted run this state comes from.
        """
        vnet_id = str(vnet_item.get("vnet") or "")
        if str(vnet_item.get("state") or "") != "new":
            return self._sdn_subnets(vnet_id)
        try:
            return self._sdn_subnets(vnet_id)
        except ReconcileError:
            return []

    def _ensure_subnet(self, state: dict[str, list[dict[str, Any]]]) -> bool:
        assert self.sdn is not None
        vnet_id = self.sdn.name
        gateway = str(naming.sdn_gateway(self.cfg.cidr))
        vnet_item = next(
            (item for item in state["vnets"] if str(item.get("vnet")) == vnet_id), None
        )
        if vnet_item is None:
            # the vnet was only just staged this run (or not at all under
            # plan): it has no subnets yet, and its endpoint may not answer
            subnets: list[dict[str, Any]] = []
        else:
            subnets = self._sdn_subnets_of(vnet_item)
        existing = next(
            (
                item
                for item in subnets
                if str(self._sdn_effective(item).get("cidr") or "") == self.cfg.cidr
            ),
            None,
        )
        desired: dict[str, Any] = {"gateway": gateway, "snat": 1}
        if existing is None:
            action(f"create SDN subnet {self.cfg.cidr} (gateway {gateway}, snat)")
            if not dry_run():
                self.client.mutate(
                    "POST",
                    f"cluster/sdn/vnets/{vnet_id}/subnets",
                    data={"subnet": self.cfg.cidr, "type": "subnet", **desired},
                )
            return True
        drift = self._sdn_drift(self._sdn_effective(existing), desired)
        if not drift:
            if existing.get("state") or isinstance(existing.get("pending"), dict):
                # staged (possibly by an interrupted run) but never applied;
                # the zone/vnet resumable scan cannot see subnets
                return True
            info(f"SDN subnet {self.cfg.cidr} exists")
            return False
        action(f"update SDN subnet {self.cfg.cidr} ({', '.join(drift)})")
        if not dry_run():
            subnet_id = quote(str(existing.get("subnet")), safe="")
            self.client.mutate(
                "PUT", f"cluster/sdn/vnets/{vnet_id}/subnets/{subnet_id}", data=desired
            )
        return True

    def _reconcile_sdn(self) -> None:
        assert self.sdn is not None
        state = self._sdn_state(refresh=True)
        self._check_zone_placement(state)
        self._refuse_foreign_pending(state)
        self._refuse_own_destructive_pending(state)
        self._refuse_vni_collisions(state)
        self._refuse_foreign_ownership(state)
        staged = self._ensure_controller(state)
        staged = self._ensure_zone(state) or staged
        staged = self._ensure_vnet(state) or staged
        staged = self._ensure_subnet(state) or staged
        ours = {
            ("zones", self.sdn.name),
            ("vnets", self.sdn.name),
            ("controllers", self.sdn.controller),
        }
        resumable = any(
            item.get("state")
            for kind, id_key in (
                ("zones", "zone"),
                ("vnets", "vnet"),
                ("controllers", "controller"),
            )
            for item in state[kind]
            if (kind, str(item.get(id_key))) in ours
        )
        if not staged and not resumable:
            if not dry_run():
                self._verify_sdn_bridges()
            return
        action("apply SDN configuration")
        if dry_run():
            return
        self.client.mutate("PUT", "cluster/sdn")
        self._sdn_cache = None
        self._verify_sdn_bridges()

    def _check_zone_placement(self, state: dict[str, list[dict[str, Any]]]) -> None:
        """Every compute node must be a zone member, or its VMs get no bridge.

        The restriction to enforce is the configured one, or — when
        cluster.yaml no longer sets sdn.nodes but the applied zone still
        carries one (managed keys are never unset) — the zone's own.
        """
        assert self.sdn is not None
        restriction = set(self.sdn.nodes)
        if not restriction:
            zone_id = self.sdn.name
            zone = next(
                (item for item in state["zones"] if str(item.get("zone")) == zone_id),
                None,
            )
            if zone is not None:
                restriction = _node_set(self._sdn_effective(zone).get("nodes"))
                if restriction:
                    warn(
                        f"SDN zone {zone_id} keeps a node restriction cluster.yaml "
                        "no longer sets (" + ",".join(sorted(restriction)) + "); "
                        "clear it in the Proxmox UI or set sdn.nodes"
                    )
        if not restriction:
            return
        outside = sorted(set(self._compute_nodes) - restriction)
        if outside:
            raise ReconcileError(
                "Proxmox nodes outside the SDN zone cannot host VMs: "
                + ", ".join(outside)
            )

    def _verify_sdn_bridges(self) -> None:
        """The apply task can return before every node's network reload finishes."""
        assert self.sdn is not None
        vnet_id = self.sdn.name
        deadline = time.monotonic() + _SDN_BRIDGE_DEADLINE
        while True:
            missing: list[str] = []
            for node in self._compute_nodes:
                # the plain listing reads only /etc/network/interfaces; SDN
                # bridges live in interfaces.d/sdn and need the bridge filter
                interfaces = self.client.get(
                    f"nodes/{node}/network", params={"type": "any_bridge"}
                )
                names = {
                    str(item.get("iface"))
                    for item in (interfaces if isinstance(interfaces, list) else [])
                    if isinstance(item, dict)
                }
                if vnet_id not in names:
                    missing.append(node)
            if not missing:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(5)
        raise ReconcileError(
            f"SDN bridge {vnet_id} is missing after apply on: " + ", ".join(missing)
        )

    def _check_static_addresses(
        self, machines: dict[str, Machine], inventory: InfrastructureInventory
    ) -> None:
        """Refuse to silently renumber a running node.

        Reordering or removing a worker pool shifts later pools' static
        addresses; surface that instead of quietly rewriting machine configs.
        """
        worker_pools = tuple(self.cfg.workers)
        mismatched = []
        for name, machine in machines.items():
            actual = inventory.machine_address(name)
            if not actual:
                continue
            expected = str(
                naming.node_address(
                    self.cfg.cidr, name, machine.role, machine.pool, worker_pools
                ).ip
            )
            if actual != expected:
                mismatched.append(f"{name} has {actual}, expects {expected}")
        if not mismatched:
            return
        for detail in mismatched:
            warn(f"static SDN address mismatch: {detail}")
        if not dry_run():
            raise ReconcileError(
                "running nodes do not match their computed static addresses "
                "(worker pool changes renumber later pools); recreate the "
                "machines or restore the pool layout"
            )

    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> set[str]:
        raw = self._raw(inventory)
        self._require_preflight()
        needs_restart: set[str] = set()

        missing: list[Machine] = []
        stopped: list[ProxmoxVM] = []
        present: list[tuple[ProxmoxVM, Machine]] = []
        for name, machine in machines.items():
            existing = raw.vms.get(name)
            if existing is None:
                missing.append(machine)
                continue
            if not self._owns_vm(raw, existing):
                raise ReconcileError(
                    f"refusing to adopt unowned Proxmox VM named {name!r}"
                )
            present.append((existing, machine))
            if existing.status != "running":
                stopped.append(existing)
        # Read every existing VM's shape first: a network change is refused
        # before any VM is touched, so a partial run never leaves the cluster
        # half-resized.
        drifts = [(vm, machine, self._vm_drift(vm, machine)) for vm, machine in present]
        for vm, machine, drift in drifts:
            stale = drift.pop("stale", False)
            had_disk = "disk" in drift
            if not drift and not stale:
                info(f"server {vm.name} exists")
            elif not drift:
                info(f"server {vm.name} exists (restart pending for its new sizing)")
            self._reconcile_firewall(vm.node, vm.vmid, vm.name)
            applied = self._apply_vm_drift(vm, machine, drift)
            # stale: the running VM still has old cores/memory. had_disk: a disk
            # grow was applied and Talos extends its EPHEMERAL partition on reboot.
            # A cores/memory revert (applied but not stale, no disk) needs no restart.
            if (stale or (applied and had_disk)) and vm.status == "running":
                needs_restart.add(vm.name)
            # remember a grown disk that is still waiting for its boot (only for a
            # running VM: a stopped VM absorbs the grow when it next starts). Written
            # to the VM so a later `converge --reboot` still finds it to restart, and
            # cleared by restart_machine once the reboot happens.
            if applied and had_disk and vm.status == "running" and not dry_run():
                marked = replace(vm, tags=vm.tags | {_RESIZE_TAG})
                raw.vms[vm.name] = marked
                self._set_vm_tags(vm, marked.tags)

        if not dry_run():
            missing_configs = [machine.name for machine in missing if machine.name not in configs]
            if missing_configs:
                raise ReconcileError(
                    "machine configuration is unavailable for: " + ", ".join(missing_configs)
                )
        self._ensure_pool(raw)
        for vm in stopped:
            action(f"start server {vm.name}")
            if not dry_run():
                self.client.mutate("POST", f"nodes/{vm.node}/qemu/{vm.vmid}/status/start")
                raw.vms[vm.name] = replace(vm, status="running")
        placements = (
            place(
                missing,
                raw.nodes,
                allowed_nodes=self._compute_nodes,
                controlplane_nodes=frozenset(
                    vm.node
                    for name, vm in raw.vms.items()
                    if name in machines
                    and machines[name].role == "controlplane"
                    and self._owns_vm(raw, vm)
                ),
            )
            if missing
            else {}
        )
        for machine in missing:
            action(
                f"create server {machine.name} on {placements[machine.name]} "
                f"({machine.cores} cores, {machine.memory}GB RAM, {machine.disk}GB disk)"
            )
            if dry_run():
                self._report_new_vm_firewall(machine.name)
                continue
            self._create_vm(
                raw,
                machine,
                placements[machine.name],
                boot_artifact,
                configs[machine.name],
            )
        return needs_restart

    def validate_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> None:
        """Refuse unsupported Proxmox changes to existing VMs before any mutation.

        Runs as the first converge phase, ahead of the image/network/Talos
        phases, so a disk shrink or NIC attachment change is rejected while the
        cluster is still untouched. Unrecognized or unowned VMs are handled here
        exactly as ``reconcile_machines`` would handle them later, so the preflight
        and the compute phase agree on what is valid.
        """
        raw = self._raw(inventory)
        for name, machine in machines.items():
            existing = raw.vms.get(name)
            if existing is None:
                continue
            if not self._owns_vm(raw, existing):
                raise ReconcileError(
                    f"refusing to adopt unowned Proxmox VM named {name!r}"
                )
            self._assert_supported_changes(existing, machine)

    def _assert_supported_changes(self, vm: ProxmoxVM, machine: Machine) -> dict[str, Any]:
        """Refuse unsupported changes to an existing VM, returning its config.

        Raises when the private or external NIC would move to another bridge/VLAN,
        when the disk would shrink, when an explicit ``node`` placement moves the
        VM to another host, or when the VM's boot disk sits on a different storage
        than ``proxmox.storage``: none of these can be reconciled in place, and
        silently ignoring them would leave cluster.yaml lying about the cluster.
        Shared by the compute phase (``_vm_drift``) and the pre-mutation
        ``validate_machines`` preflight, so a rejected change is caught while
        every earlier converge phase is still unmutated.
        """
        config = self.client.get(f"nodes/{vm.node}/qemu/{vm.vmid}/config")
        if not isinstance(config, dict):
            raise ReconcileError(f"Proxmox returned no config for {vm.name}: {config!r}")
        if machine.node and machine.node != vm.node:
            raise ReconcileError(
                f"refusing to move {vm.name} from host {vm.node} to {machine.node}: "
                "changing the placement of a running cluster's VM is not supported; "
                "revert the `node` in cluster.yaml or recreate the cluster"
            )
        have_storage = str(config.get("scsi0") or "").split(",", 1)[0].split(":", 1)[0]
        if have_storage and have_storage != self.provider.storage:
            raise ReconcileError(
                f"refusing to move the disk of {vm.name} from storage "
                f"{have_storage} to {self.provider.storage}: changing "
                "proxmox.storage does not migrate existing disks; revert the "
                "change in cluster.yaml or recreate the cluster"
            )
        want_nics = {"net0": (self.cluster_link, self.cfg.network.cluster.vlan)}
        ext = self.external_network
        if ext:
            want_nics["net1"] = (str(ext["bridge"]), ext.get("vlan"))
        for nic, (bridge, vlan) in want_nics.items():
            have = _kv(config.get(nic))
            have_bridge, have_tag = have.get("bridge"), have.get("tag")
            want_tag = str(int(vlan)) if vlan is not None else None
            if have_bridge != bridge or have_tag != want_tag:
                raise ReconcileError(
                    f"refusing to move {vm.name} {nic} from "
                    f"{_link(have_bridge, have_tag)} to {_link(bridge, want_tag)}: "
                    "changing a network attachment of a running cluster is not supported; "
                    "revert the change in cluster.yaml or recreate the cluster"
                )
        if not ext and config.get("net1") is not None:
            raise ReconcileError(
                f"refusing to detach the external NIC of {vm.name}: removing "
                "proxmox.network.external from a running cluster is not supported; "
                "revert the change in cluster.yaml or recreate the cluster"
            )
        have_disk = _size_gib(_kv(config.get("scsi0")).get("size"))
        if have_disk is not None and have_disk > machine.disk:
            raise ReconcileError(
                f"refusing to shrink the disk of {vm.name} from {have_disk}GB to "
                f"{machine.disk}GB: Proxmox cannot shrink a disk; revert `disk` in "
                "cluster.yaml, or replace the machine (scale its pool down past it "
                "and back up)"
            )
        return config

    def _vm_drift(self, vm: ProxmoxVM, machine: Machine) -> dict[str, Any]:
        """Compare a VM's Proxmox config with cluster.yaml.

        Returns the reconcilable drift: `cores`, `memory` (MiB) and `disk`
        (GiB, grow only), plus `stale` when the *running* VM differs from the
        desired sizing -- a change written on an earlier run that still waits
        for a restart. Unsupported changes (a NIC moving bridge/VLAN, a disk
        shrinking, a placement move, a storage change) are refused up front by
        ``_assert_supported_changes``.
        """
        config = self._assert_supported_changes(vm, machine)
        # what the VM actually runs with; `config` alone shows pending values
        running = self.client.get(
            f"nodes/{vm.node}/qemu/{vm.vmid}/config", params={"current": 1}
        )
        if not isinstance(running, dict):
            running = config

        drift: dict[str, Any] = {}
        have_cores = int(config.get("cores") or 1)
        if have_cores != machine.cores:
            drift["cores"] = (have_cores, machine.cores)
        have_memory = _memory_of(config.get("memory"))
        if have_memory != _memory_mib(machine.memory):
            drift["memory"] = (have_memory, _memory_mib(machine.memory))
        have_disk = _size_gib(_kv(config.get("scsi0")).get("size"))
        if have_disk is not None and have_disk != machine.disk:
            # not a shrink: _assert_supported_changes refused that already
            drift["disk"] = (have_disk, machine.disk)
        if (
            int(running.get("cores") or 1) != machine.cores
            or _memory_of(running.get("memory")) != _memory_mib(machine.memory)
            # a grown disk shows the new size in the live config immediately, so
            # the only record that its EPHEMERAL extension still waits for a boot
            # is the tag written when it grew
            or _RESIZE_TAG in vm.tags
        ):
            drift["stale"] = True
        return drift

    def _apply_vm_drift(self, vm: ProxmoxVM, machine: Machine, drift: dict[str, Any]) -> bool:
        """Apply reconcilable drift; True when the VM must restart to pick it up."""
        if not drift:
            return False
        sizing = {key: drift[key] for key in ("cores", "memory") if key in drift}
        if sizing:
            parts = [
                f"cores {drift['cores'][0]}->{drift['cores'][1]}" if "cores" in drift else "",
                f"memory {drift['memory'][0] // _MIB_PER_GB}GB->{machine.memory}GB"
                if "memory" in drift
                else "",
            ]
            action(f"resize server {vm.name} ({', '.join(p for p in parts if p)})")
            warn(f"{vm.name}: cores/memory take effect when the VM next restarts")
            if not dry_run():
                data: dict[str, Any] = {}
                if "cores" in drift:
                    data["cores"] = machine.cores
                if "memory" in drift:
                    data["memory"] = _memory_mib(machine.memory)
                self.client.mutate("PUT", f"nodes/{vm.node}/qemu/{vm.vmid}/config", data=data)
        if "disk" in drift:
            have, want = drift["disk"]
            action(f"grow disk of server {vm.name} ({have}GB->{want}GB)")
            warn(f"{vm.name}: Talos extends its EPHEMERAL partition on the next reboot")
            if not dry_run():
                self.client.mutate(
                    "PUT",
                    f"nodes/{vm.node}/qemu/{vm.vmid}/resize",
                    data={"disk": "scsi0", "size": f"{want}G"},
                )
        return True

    def _set_vm_tags(self, vm: ProxmoxVM, tags: frozenset[str]) -> None:
        """Replace a VM's Proxmox tag set (Proxmox `tags` is not additive)."""
        self.client.mutate(
            "PUT",
            f"nodes/{vm.node}/qemu/{vm.vmid}/config",
            data={"tags": ";".join(sorted(tags))},
        )

    def _ensure_pool(self, inventory: ProxmoxInventory) -> None:
        existing = inventory.pools.get(self.pool_id)
        if existing is not None:
            if existing.comment != self.pool_comment:
                raise ReconcileError(
                    f"refusing to adopt Proxmox pool {self.pool_id!r} with a foreign comment"
                )
            return
        action(f"create resource pool {self.pool_id}")
        if not dry_run():
            self.client.mutate(
                "POST", "pools", data={"poolid": self.pool_id, "comment": self.pool_comment}
            )
            inventory.pools[self.pool_id] = ProxmoxPool(self.pool_id, self.pool_comment)

    def _create_vm(
        self,
        inventory: ProxmoxInventory,
        machine: Machine,
        node: str,
        boot_artifact: str,
        machine_config: str,
    ) -> None:
        vmid = int(self.client.get("cluster/nextid"))
        cidata_name = _cidata_name(self.cfg.name, machine.name)
        cidata_volume = f"{self.provider.cidata_storage}:iso/{cidata_name}"
        workdir = Path(tempfile.mkdtemp(prefix=f"taloscluster-{machine.name}-"))
        created = False
        try:
            local_iso = workdir / cidata_name
            cidata.build(workdir / "source", local_iso, machine.name, machine_config)
            self._upload_iso(node, self.provider.cidata_storage, local_iso)
            net0 = (
                f"virtio={naming.mac_address(self.cfg.name, machine.name, 0)},"
                f"bridge={self.cluster_link},"
                f"firewall=1"
            )
            if self.cfg.network.cluster.vlan is not None:
                net0 += f",tag={self.cfg.network.cluster.vlan}"
            data: dict[str, Any] = {
                "vmid": vmid,
                "name": machine.name,
                "pool": self.pool_id,
                "description": self.pool_comment,
                "tags": ";".join(sorted(owned_tags(self.cfg.name, machine.role, machine.pool))),
                "cores": machine.cores,
                "memory": _memory_mib(machine.memory),
                "cpu": "host",
                "ostype": "l26",
                "machine": "q35",
                "bios": "ovmf",
                "efidisk0": f"{self.provider.storage}:1,efitype=4m,pre-enrolled-keys=0",
                "scsihw": "virtio-scsi-single",
                "scsi0": f"{self.provider.storage}:{machine.disk}",
                "ide2": f"{boot_artifact},media=cdrom",
                "ide3": f"{cidata_volume},media=cdrom",
                "net0": net0,
                "agent": "enabled=1",
                "onboot": 1,
                "boot": "order=scsi0;ide2",
                "smbios1": f"uuid={_smbios_uuid(self.cfg.name, machine.name)}",
            }
            ext = self.external_network
            if ext:
                net1 = (
                    f"virtio={naming.mac_address(self.cfg.name, machine.name, 1)},"
                    f"bridge={ext['bridge']},firewall=1"
                )
                if ext.get("vlan") is not None:
                    net1 += f",tag={int(ext['vlan'])}"
                data["net1"] = net1
            self.client.mutate("POST", f"nodes/{node}/qemu", data=data)
            created = True
            self._reconcile_firewall(node, vmid, machine.name)
            self.client.mutate("POST", f"nodes/{node}/qemu/{vmid}/status/start")
            inventory.vms[machine.name] = ProxmoxVM(
                vmid=vmid,
                name=machine.name,
                node=node,
                status="running",
                pool=self.pool_id,
                tags=owned_tags(self.cfg.name, machine.role, machine.pool),
                memory=_memory_mib(machine.memory) * 1024 * 1024,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if not created:
                self._remove_iso_if_present(node, self.provider.cidata_storage, cidata_name)

    def _desired_firewall_rules(self) -> dict[_FirewallKey, str]:
        """The ingress rules this cluster wants, keyed by (proto, dport, source).

        Mirrors the OpenStack security group: ICMP, the ports left open by
        `security:`, one rule per named-rule host CIDR, and intra-cluster
        tcp+udp from the private CIDR (Neutron's remote-group equivalent).
        """
        rules: dict[_FirewallKey, str] = {("icmp", None, None): "icmp"}
        for port in self.cfg.open_ports():
            rules[("tcp", port, None)] = f"tcp/{port} open"
        for rule in self.cfg.security.values():
            for name, cidr in rule.hosts.items():
                rules[("tcp", rule.port, cidr)] = f"{rule.name} from {name}"
        rules[("tcp", None, self.cfg.cidr)] = "intra-cluster tcp"
        rules[("udp", None, self.cfg.cidr)] = "intra-cluster udp"
        return rules

    @staticmethod
    def _firewall_rule_key(rule: Any) -> _FirewallKey | None:
        """Normalize an existing Proxmox rule, or None if it is not shaped like ours."""
        if not isinstance(rule, dict):
            return None
        if rule.get("type") != "in" or str(rule.get("action", "")).upper() != "ACCEPT":
            return None
        proto = str(rule.get("proto") or "").lower()
        if not proto:
            return None
        dport = rule.get("dport")
        if dport in (None, ""):
            port = None
        else:
            try:
                port = int(dport)  # a range or service name is never one of ours
            except (TypeError, ValueError):
                return None
        source = rule.get("source") or None
        return (proto, port, str(source) if source else None)

    def _managed_firewall_ports(self) -> set[int]:
        """Every tcp port whose ingress policy this cluster's config decides."""
        return set(self.cfg.open_ports()) | {
            rule.port for rule in self.cfg.security.values()
        }

    def _owns_firewall_rule(self, rule: Any, key: _FirewallKey | None) -> bool:
        """True when this tool is responsible for `rule`.

        Two independent claims, either of which is enough:

        * the comment marker, written by every rule we create; and
        * the rule's shape falling inside the policy `security:` decides -- ICMP,
          intra-cluster traffic, or a tcp port some rule in `security:` governs.

        The marker alone is not enough, because 0.4.0 wrote its rules without one
        and dropping a CIDR from an allowlist has to actually close it on those
        VMs. The shape alone is not enough either, because deleting a whole named
        rule from `cluster.yaml` takes its port out of the managed set while its
        rules are still on the VM.
        """
        if not isinstance(rule, dict):
            return False
        if str(rule.get("comment") or "").startswith(_FIREWALL_MARKER):
            return True
        if key is None:
            return False
        proto, port, source = key
        if proto == "icmp" and port is None and source is None:
            return True
        if proto in ("tcp", "udp") and port is None and source == self.cfg.cidr:
            return True
        return proto == "tcp" and port in self._managed_firewall_ports()

    def _classify_firewall(
        self, existing: list[Any], desired: dict[_FirewallKey, str], name: str, *, quiet: bool
    ) -> tuple[list[int], set[_FirewallKey]]:
        """Split existing ingress rules into (stale positions, satisfied keys)."""
        stale: list[int] = []
        seen: set[_FirewallKey] = set()
        for rule in existing:
            if not isinstance(rule, dict) or rule.get("type") != "in":
                continue
            key = self._firewall_rule_key(rule)
            enabled = _truthy(rule.get("enable", 1))
            if not self._owns_firewall_rule(rule, key):
                # A per-VM firewall is shared with whoever else administers the
                # VM. Outside the ports `security:` governs we are a guest, so
                # report and keep. A rule that already allows what we want does
                # the job, so we never stack a duplicate on top of it.
                if enabled and key is not None and key in desired:
                    seen.add(key)
                else:
                    warn(f"leaving unowned firewall rule {rule.get('pos')} on {name}")
                    if not quiet and key is not None and key in desired:
                        info(f"disabled unowned rule on {name}; adding our own")
                continue
            # A disabled rule of ours allows nothing, so it is stale rather than
            # satisfying: delete it and write a live one in its place.
            if key is None or key not in desired or key in seen or not enabled:
                stale.append(int(rule.get("pos", 0)))
                continue
            seen.add(key)
        return stale, seen

    def _report_new_vm_firewall(self, name: str) -> None:
        """Under plan, report the firewall a *new* VM will get.

        An existing VM's firewall is compared against what is there (see
        `_reconcile_firewall`), but a VM that does not exist yet cannot be
        queried -- so the plan reports the full default it would apply: the
        DROP-in policy plus every desired ingress rule.
        """
        count = len(self._desired_firewall_rules())
        parts = ["policy"]
        if count:
            parts.append(f"{count} rule{'s' if count != 1 else ''} added")
        action(f"configure firewall on {name} ({', '.join(parts)})")

    def _reconcile_firewall(self, node: str, vmid: int, name: str) -> None:
        """Converge one VM's firewall onto the desired rule set.

        Default deny ingress, default allow egress (matching Neutron defaults).
        Proxmox firewall is stateful via conntrack, so return traffic for
        outbound connections is automatically allowed.  ARP is handled at layer 2
        and is not subject to these rules — VIP failover via gratuitous ARP works
        regardless of policy.  Only ingress is reconciled; egress rules are left
        alone.  See `_owns_firewall_rule` for what counts as ours to delete.

        Missing rules are added before stale ones are deleted, so editing an
        allowlist never leaves a port unprotected or unreachable in between.
        """
        base = f"nodes/{node}/qemu/{vmid}/firewall"
        desired = self._desired_firewall_rules()
        existing = self.client.get(f"{base}/rules")  # a read, safe under plan
        if not isinstance(existing, list):
            raise ReconcileError(
                f"Proxmox returned no firewall rule list for {name}: {existing!r}"
            )
        stale, seen = self._classify_firewall(existing, desired, name, quiet=False)

        wanted_opts = {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "dhcp": 1}
        current_opts = self.client.get(f"{base}/options")
        opts_diff = not (
            isinstance(current_opts, dict)
            and all(
                str(current_opts.get(k, "")) == str(v)
                for k, v in wanted_opts.items()
            )
        )
        to_create = [key for key in desired if key not in seen]

        if not opts_diff and not to_create and not stale:
            return

        parts: list[str] = []
        if opts_diff:
            parts.append("policy")
        if to_create:
            parts.append(f"{len(to_create)} rule{'s' if len(to_create) != 1 else ''} added")
        if stale:
            parts.append(f"{len(stale)} rule{'s' if len(stale) != 1 else ''} deleted")
        action(f"configure firewall on {name} ({', '.join(parts)})")

        if dry_run():
            return

        if opts_diff:
            self.client.mutate("PUT", f"{base}/options", data=wanted_opts)

        for key in to_create:
            proto, dport, source = key
            description = desired[key]
            data: dict[str, Any] = {
                "type": "in", "action": "ACCEPT", "enable": 1,
                "proto": proto, "comment": f"{_FIREWALL_MARKER}{description}",
            }
            if dport is not None:
                data["dport"] = dport
            if source is not None:
                data["source"] = source
            self.client.mutate("POST", f"{base}/rules", data=data)

        if not stale:
            return
        if to_create:
            # Proxmox splices new rules in at the top, so every position we
            # recorded has shifted. Re-read rather than guess the offset.
            refreshed = self.client.get(f"{base}/rules")
            if not isinstance(refreshed, list):
                raise ReconcileError(
                    f"Proxmox returned no firewall rule list for {name}: {refreshed!r}"
                )
            stale, _ = self._classify_firewall(refreshed, desired, name, quiet=True)
        for pos in sorted(stale, reverse=True):
            self.client.mutate("DELETE", f"{base}/rules/{pos}")

    def restart_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        """Proxmox `reboot`: ACPI shutdown (Talos shuts down gracefully), then a
        fresh QEMU start that applies pending cores/memory. A reboot from inside
        the guest keeps the old QEMU process and never picks those up."""
        raw = self._raw(inventory)
        self._require_preflight()
        vm = raw.vms.get(name)
        if vm is None:
            raise ReconcileError(f"cannot restart unknown Proxmox VM {name!r}")
        if not self._owns_vm(raw, vm):
            raise ReconcileError(f"refusing to restart unowned Proxmox VM {name!r}")
        action(f"restart server {name} (proxmox reboot, applies pending sizing)")
        if not dry_run():
            self.client.mutate(
                "POST", f"nodes/{vm.node}/qemu/{vm.vmid}/status/reboot", data={"timeout": 300}
            )
            # the reboot extends the grown disk's EPHEMERAL partition; the grow is
            # absorbed, so drop the pending-resize tag that made this restart happen
            if _RESIZE_TAG in vm.tags:
                cleared = replace(vm, tags=vm.tags - {_RESIZE_TAG})
                raw.vms[vm.name] = cleared
                self._set_vm_tags(vm, cleared.tags)

    def finalize_machines(self, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        self._require_preflight()
        for vm in raw.vms.values():
            if not self._owns_vm(raw, vm):
                continue
            config = self.client.get(f"nodes/{vm.node}/qemu/{vm.vmid}/config")
            if isinstance(config, dict) and isinstance(config.get("ide2"), str):
                # the boot ISO cdrom is only needed to install to disk; once the
                # cluster is healthy the node boots from scsi0, so drop the ide2
                # reference so `image remove` can delete the ISO it boots from
                action(f"detach boot ISO cdrom from {vm.name}")
                self.client.mutate(
                    "PUT",
                    f"nodes/{vm.node}/qemu/{vm.vmid}/config",
                    data={"delete": "ide2", "boot": "order=scsi0"},
                )
            filename = _cidata_name(self.cfg.name, vm.name)
            volume = self._find_iso(vm.node, self.provider.cidata_storage, filename)
            if not volume:
                continue
            action(f"detach and delete cidata for {vm.name}")
            self.client.mutate(
                "PUT", f"nodes/{vm.node}/qemu/{vm.vmid}/config", data={"delete": "ide3"}
            )
            self._delete_volume(vm.node, self.provider.cidata_storage, volume)

    def delete_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        self._require_preflight()
        vm = raw.vms.get(name)
        if vm is None:
            return
        if not self._owns_vm(raw, vm):
            raise ReconcileError(f"refusing to delete unowned Proxmox VM {name!r}")
        action(f"delete server {name}")
        if not dry_run():
            current = self.client.get(f"nodes/{vm.node}/qemu/{vm.vmid}/status/current")
            if isinstance(current, dict) and current.get("status") == "running":
                self.client.mutate("POST", f"nodes/{vm.node}/qemu/{vm.vmid}/status/stop")
            self.client.mutate("DELETE", f"nodes/{vm.node}/qemu/{vm.vmid}")
            raw.vms.pop(name, None)
            self._remove_iso_if_present(
                vm.node, self.provider.cidata_storage, _cidata_name(self.cfg.name, name)
            )
        inventory.machines.pop(name, None)

    def default_node_tags(self) -> dict[str, str]:
        return {}

    def provider_status(self) -> dict[str, Any]:
        raw = self._require_preflight()
        status: dict[str, Any] = {
            "url": self.provider.url,
            "online_nodes": sorted(name for name, node in raw.nodes.items() if node.online),
            "storage": self.provider.storage,
            "iso_storage": self.provider.iso_storage,
        }
        if self.external_network:
            status["ingress_pool"] = str(self.external_network.get("ingress_pool") or "")
        return status

    def print_environment(self) -> None:
        print(f"export PVE_API_URL={shlex.quote(self.provider.url)}")
        print(f"export PVE_API_TOKEN_ID={shlex.quote(self.secrets.token_id)}")
        print(f"export PVE_API_TOKEN_SECRET={shlex.quote(self.secrets.token_secret)}")

    def download_image(self) -> str:
        return self.ensure_boot_artifact()

    def remove_image(self, assume_yes: bool = False) -> None:
        inventory = self._require_preflight()
        schematic = factory.schematic_id(naming.BASE_EXTENSIONS)
        filename = _boot_iso_name(self.cfg.talos_version, schematic)
        legacy_file = f"{naming.legacy_image_name(self.cfg.talos_version)}.iso"
        nodes = self._iso_nodes(inventory)
        volumes = {
            (node, name): self._find_iso(node, self.provider.iso_storage, name)
            for node in nodes
            for name in (filename, legacy_file)
        }
        found = [(node, volume) for (node, _name), volume in volumes.items() if volume]
        if not found:
            info(f"image {filename} not found, nothing to remove")
            return
        referenced = self._boot_iso_in_use(inventory)
        in_use = sorted({volume for _node, volume in found} & set(referenced))
        if in_use:
            detaches = sorted(
                f"qm set {vmid} --delete {slot}"
                for volume in in_use
                for vmid, slot in referenced[volume]
            )
            raise ReconcileError(
                "refusing to remove image(s) still booted by an owned Proxmox VM: "
                + ", ".join(in_use)
                + "; detach the cdrom on those VMs before removing the image, e.g. "
                + ", ".join(detaches)
            )
        warn("other clusters on the same Talos version may share this image")
        if not assume_yes and not dry_run():
            prompt_name = ", ".join(
                sorted({volume.rsplit("/", 1)[-1] for _node, volume in found})
            )
            if input(f"type '{prompt_name}' to confirm: ").strip() != prompt_name:
                raise SystemExit("aborted")
        for node, volume in found:
            action(f"delete image {volume}")
            if not dry_run():
                self._delete_volume(node, self.provider.iso_storage, volume)

    def _boot_iso_in_use(self, inventory: ProxmoxInventory) -> dict[str, set[tuple[int, str]]]:
        """Boot volumes (``ide2`` cdroms) that owned VMs still point at.

        Every VM is created with the boot ISO on ``ide2`` and converge
        detaches it once the node has installed to disk (in ``finalize_machines``,
        alongside the cidata ``ide3``), so a VM that has not reached a healthy
        finalize still references the ISO it would boot again. Proxmox does not
        block deleting a referenced ISO, but a VM whose cdrom volume is gone
        fails to start, so ``remove_image`` refuses while any owned VM still
        boots from a volume it would delete. Returns the referenced volume ids
        keyed by the VMIDs and their slots that boot them, so a refusal can name
        the exact ``qm set <vmid> --delete <slot>`` step for each VM.
        """
        referenced: dict[str, set[tuple[int, str]]] = {}
        for _name, vm in inventory.vms.items():
            if not self._owns_vm(inventory, vm):
                continue
            config = self.client.get(f"nodes/{vm.node}/qemu/{vm.vmid}/config")
            if not isinstance(config, dict):
                continue
            for key in ("ide2", "ide0", "ide1", "ide3", "sata0", "sata1"):
                value = config.get(key)
                if isinstance(value, str):
                    referenced.setdefault(value.split(",", 1)[0].strip(), set()).add(
                        (vm.vmid, key)
                    )
        return referenced

    def destroy_summary(self, inventory: InfrastructureInventory) -> str:
        raw = self._raw(inventory)
        self._refuse_sdn_destroy()
        count = sum(self._owns_vm(raw, vm) for vm in raw.vms.values())
        owned_pool = raw.pools.get(self.pool_id)
        pool_count = int(owned_pool is not None and owned_pool.comment == self.pool_comment)
        summary = f"{count} virtual machines, {pool_count} owned resource pool"
        if self.sdn:
            summary += ", managed SDN network"
        return summary

    def destroy_resources(self, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        self._require_preflight()
        self._refuse_sdn_destroy()
        for name in sorted(list(raw.vms)):
            if self._owns_vm(raw, raw.vms[name]):
                self.delete_machine(name, inventory)
        self._destroy_pool(raw)
        if self.sdn:
            self._destroy_sdn()

    def _destroy_pool(self, raw: ProxmoxInventory) -> None:
        pool = raw.pools.get(self.pool_id)
        if pool is None:
            return
        if pool.comment != self.pool_comment:
            warn(f"leaving foreign resource pool {self.pool_id}")
            return
        if dry_run():
            foreign = [
                vm.name
                for vm in raw.vms.values()
                if vm.pool == self.pool_id and not self._owns_vm(raw, vm)
            ]
            if foreign:
                warn(f"leaving non-empty resource pool {self.pool_id}")
                return
            action(f"delete resource pool {self.pool_id}")
            return
        details = self.client.get(f"pools/{quote(self.pool_id, safe='')}")
        members = details.get("members", []) if isinstance(details, dict) else []
        if members:
            warn(f"leaving non-empty resource pool {self.pool_id}")
            return
        action(f"delete resource pool {self.pool_id}")
        if not dry_run():
            self.client.mutate("DELETE", f"pools/{quote(self.pool_id, safe='')}")

    def _destroy_sdn(self) -> None:
        """Delete the owned subnet, VNet, and zone (in that order), then apply.

        The controller is shared infrastructure and is never deleted. A zone
        or VNet holding anything foreign is kept and reported.
        """
        assert self.sdn is not None
        zone_id = self.sdn.name
        vnet_id = self.sdn.name
        state = self._sdn_state(refresh=True)
        zone = next(
            (item for item in state["zones"] if str(item.get("zone")) == zone_id), None
        )
        vnet = next(
            (item for item in state["vnets"] if str(item.get("vnet")) == vnet_id), None
        )
        if zone is None and vnet is None:
            return
        self._refuse_foreign_pending(state)
        self._refuse_shared_controller_destructive_pending(state)
        changes = 0
        vnet_removed = vnet is None
        if vnet is not None:
            if not self._owns_vnet(vnet):
                warn(f"leaving unowned SDN vnet {vnet_id}")
            else:
                foreign_subnets: list[str] = []
                for subnet in self._sdn_subnets_of(vnet):
                    cidr = str(self._sdn_effective(subnet).get("cidr") or "")
                    if cidr != self.cfg.cidr:
                        foreign_subnets.append(cidr or str(subnet.get("subnet")))
                        continue
                    action(f"delete SDN subnet {cidr}")
                    changes += 1
                    if not dry_run():
                        subnet_id = quote(str(subnet.get("subnet")), safe="")
                        self.client.mutate(
                            "DELETE", f"cluster/sdn/vnets/{vnet_id}/subnets/{subnet_id}"
                        )
                if foreign_subnets:
                    warn(
                        f"leaving SDN vnet {vnet_id} with foreign subnets: "
                        + ", ".join(sorted(foreign_subnets))
                    )
                else:
                    action(f"delete SDN vnet {vnet_id}")
                    changes += 1
                    if not dry_run():
                        self.client.mutate("DELETE", f"cluster/sdn/vnets/{vnet_id}")
                    vnet_removed = True
        if zone is not None:
            remaining = sorted(
                str(item.get("vnet"))
                for item in state["vnets"]
                if str(self._sdn_effective(item).get("zone")) == zone_id
                and str(item.get("vnet")) != vnet_id
            )
            if remaining:
                warn(
                    f"leaving SDN zone {zone_id} with foreign VNets: " + ", ".join(remaining)
                )
            elif not vnet_removed:
                warn(f"leaving SDN zone {zone_id}: its vnet was kept")
            elif not self._zone_matches_ours(self._sdn_effective(zone)):
                warn(f"leaving unowned SDN zone {zone_id}")
            else:
                action(f"delete SDN zone {zone_id}")
                changes += 1
                if not dry_run():
                    self.client.mutate("DELETE", f"cluster/sdn/zones/{zone_id}")
        if changes:
            action("apply SDN configuration")
            if not dry_run():
                self.client.mutate("PUT", "cluster/sdn")
                self._sdn_cache = None

    def _iso_nodes(self, inventory: ProxmoxInventory) -> list[str]:
        storage = inventory.storages[self.provider.iso_storage]
        nodes = list(self._compute_nodes)
        return nodes[:1] if _truthy(storage.get("shared")) else nodes

    def _owns_vm(self, inventory: ProxmoxInventory, vm: ProxmoxVM) -> bool:
        pool = inventory.pools.get(self.pool_id)
        return (
            is_owned(vm, self.cfg.name)
            and vm.pool == self.pool_id
            and pool is not None
            and pool.comment == self.pool_comment
        )

    def _guest_address(self, vm: ProxmoxVM) -> str:
        try:
            data = self.client.get(
                f"nodes/{vm.node}/qemu/{vm.vmid}/agent/network-get-interfaces"
            )
        except ReconcileError:
            return ""
        interfaces = data.get("result", []) if isinstance(data, dict) else []
        network = ipaddress.ip_network(self.cfg.cidr)
        # The guest reports every address on the private link, including the
        # Layer 2 kube-api VIP the control plane currently owns. The VIP names
        # whatever node happens to hold it, never a specific machine, so it must
        # not become a node address: without tailscale it would be picked up as
        # the talos endpoint and target the wrong owner. Skip it and keep the
        # first real address; return "" when only the VIP is left rather than
        # fall back to a floating address (see current_network for the VIP).
        cluster_vip = str(self.cfg.network.cluster.kubeapi_vip or "")
        for interface in interfaces if isinstance(interfaces, list) else []:
            addresses = interface.get("ip-addresses", []) if isinstance(interface, dict) else []
            for address in addresses if isinstance(addresses, list) else []:
                value = address.get("ip-address") if isinstance(address, dict) else None
                if not isinstance(value, str):
                    continue
                try:
                    parsed = ipaddress.ip_address(value)
                except (TypeError, ValueError):
                    continue
                if parsed.version == 4 and parsed in network and str(parsed) != cluster_vip:
                    return str(parsed)
        return ""

    def _find_iso(self, node: str, storage: str, filename: str) -> str:
        items = self.client.get(
            f"nodes/{node}/storage/{storage}/content", params={"content": "iso"}
        )
        if not isinstance(items, list):
            return ""
        suffix = f"iso/{filename}"
        for item in items:
            if isinstance(item, dict) and str(item.get("volid") or "").endswith(suffix):
                return str(item["volid"])
        return ""

    def _download_iso(self, node: str, storage: str, url: str, filename: str) -> None:
        self.client.mutate(
            "POST",
            f"nodes/{node}/storage/{storage}/download-url",
            data={"content": "iso", "url": url, "filename": filename},
            timeout=(10, 600),
        )

    def _upload_iso(self, node: str, storage: str, path: Path) -> None:
        with path.open("rb") as stream:
            self.client.mutate(
                "POST",
                f"nodes/{node}/storage/{storage}/upload",
                data={"content": "iso"},
                files={"filename": (path.name, stream, "application/octet-stream")},
                timeout=(10, 600),
            )

    def _remove_iso_if_present(self, node: str, storage: str, filename: str) -> None:
        volume = self._find_iso(node, storage, filename)
        if volume:
            self._delete_volume(node, storage, volume)

    def _delete_volume(self, node: str, storage: str, volume: str) -> None:
        self.client.mutate(
            "DELETE",
            f"nodes/{node}/storage/{storage}/content/{quote(volume, safe='')}",
        )


def _boot_iso_name(talos_version: str, schematic: str) -> str:
    return f"{naming.image_name(talos_version, schematic)}.iso"


def _cidata_name(cluster: str, hostname: str) -> str:
    digest = hashlib.sha256(f"{cluster}/{hostname}".encode()).hexdigest()[:12]
    return f"taloscluster-cidata-{digest}.iso"


def _smbios_uuid(cluster: str, hostname: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"taloscluster:{cluster}:{hostname}"))


def _storage_nodes(storage: dict[str, Any], online: set[str]) -> set[str]:
    configured = storage.get("nodes")
    if isinstance(configured, str):
        return online.intersection(item.strip() for item in configured.split(",") if item.strip())
    if isinstance(configured, list):
        return online.intersection(str(item) for item in configured)
    return set(online)


def _kv(value: Any) -> dict[str, str]:
    """Split a Proxmox property string (`virtio=MAC,bridge=vmbr0,tag=5`)."""
    if not isinstance(value, str):
        return {}
    out: dict[str, str] = {}
    for part in value.split(","):
        key, sep, val = part.partition("=")
        if sep:
            out[key] = val
    return out


def _link(bridge: str | None, tag: str | None) -> str:
    return f"bridge={bridge}" + (f",tag={tag}" if tag is not None else "")


def _memory_of(value: Any) -> int:
    """VM memory in MiB from either the legacy scalar or `current=<MiB>` form."""
    if isinstance(value, str) and "=" in value:
        return int(_kv(value).get("current") or 0)
    return int(value or 0)


_SIZE_UNITS = {"": 1 / 1024**3, "K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}


def _size_gib(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d+)([KMGT]?)", value)
    if match is None:
        return None
    return int(int(match.group(1)) * _SIZE_UNITS[match.group(2)])


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"1", "true", "yes", "on"}


def _node_set(value: Any) -> set[str]:
    """Normalize a Proxmox node list (comma/semicolon string or list) to a set."""
    if isinstance(value, str):
        return {item.strip() for item in value.replace(";", ",").split(",") if item.strip()}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()
