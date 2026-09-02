"""Proxmox compute backend using existing bridges or VNets."""

from __future__ import annotations

import hashlib
import ipaddress
import shlex
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import naming
from ..config import Config, Machine, ProxmoxConfig, ProxmoxSecrets, Secrets
from ..errors import ConfigError, ReconcileError
from ..infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
)
from ..output import action, dry_run, info, warn
from ..talos import factory
from . import cidata
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


def _memory_mib(memory_gb: int) -> int:
    """Convert the whole-GB cluster.yaml value to Proxmox's MiB API unit."""
    return memory_gb * _MIB_PER_GB


class ProxmoxBackend:
    name = "proxmox"

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
        self._inventory: ProxmoxInventory | None = None
        self._preflight_complete = False
        self._compute_nodes: tuple[str, ...] = ()

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
    def external_network(self) -> dict[str, Any]:
        value = self.provider.network.get("external")
        return value if isinstance(value, dict) else {}

    def _raw_inventory(self, *, refresh: bool = False) -> ProxmoxInventory:
        if refresh or self._inventory is None:
            self._preflight_complete = False
            self._inventory = load(self.client)
            self._validate_environment(self._inventory)
            self._validate_permissions(self._inventory)
        return self._inventory

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
        network_path = (
            f"/sdn/vnets/{self.cluster_network['vnet']}"
            if self.cluster_network.get("vnet")
            else "/sdn/zones/localnetwork"
        )
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
        filename = _boot_iso_name(self.cfg.talos_version)
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
        return self.current_network(inventory)

    def current_network(self, inventory: InfrastructureInventory) -> NetworkResult:
        ext = self.external_network
        vip = str(ext.get("kubeapi_vip") or self.cluster_network.get("kubeapi_vip") or "")
        return NetworkResult(kubernetes=Endpoint(vip=vip, advertised_address=vip))

    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> None:
        raw = self._raw(inventory)
        self._require_preflight()

        missing: list[Machine] = []
        stopped: list[ProxmoxVM] = []
        for name, machine in machines.items():
            existing = raw.vms.get(name)
            if existing is None:
                missing.append(machine)
                continue
            if not self._owns_vm(raw, existing):
                raise ReconcileError(
                    f"refusing to adopt unowned Proxmox VM named {name!r}"
                )
            if existing.status == "running":
                info(f"server {name} exists")
            else:
                stopped.append(existing)

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
                continue
            self._create_vm(
                raw,
                machine,
                placements[machine.name],
                boot_artifact,
                configs[machine.name],
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
                f"bridge={self.cluster_network.get('bridge') or self.cluster_network.get('vnet')},"
                f"firewall=1"
            )
            if self.cluster_network.get("vlan") is not None:
                net0 += f",tag={int(self.cluster_network['vlan'])}"
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
            self._configure_firewall(node, vmid)
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

    def _configure_firewall(self, node: str, vmid: int) -> None:
        """Apply firewall rules equivalent to the OpenStack security group.

        Default deny ingress, default allow egress (matching Neutron defaults).
        Proxmox firewall is stateful via conntrack, so return traffic for
        outbound connections is automatically allowed.  ARP is handled at layer 2
        and is not subject to these rules — VIP failover via gratuitous ARP works
        regardless of policy.
        """
        base = f"nodes/{node}/qemu/{vmid}/firewall"
        self.client.mutate(
            "PUT", f"{base}/options",
            data={"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "dhcp": 1},
        )
        rules: list[dict[str, Any]] = [
            {"type": "in", "action": "ACCEPT", "enable": 1, "proto": "icmp"},
            {"type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": 80},
            {"type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": 443},
        ]
        for cidr in self.cfg.security_talos.values():
            rules.append({
                "type": "in", "action": "ACCEPT", "enable": 1,
                "proto": "tcp", "dport": 50000, "source": cidr,
            })
        for cidr in self.cfg.security_kubernetes.values():
            rules.append({
                "type": "in", "action": "ACCEPT", "enable": 1,
                "proto": "tcp", "dport": 6443, "source": cidr,
            })
        rules.append({
            "type": "in", "action": "ACCEPT", "enable": 1,
            "proto": "tcp", "source": self.cfg.cidr,
        })
        rules.append({
            "type": "in", "action": "ACCEPT", "enable": 1,
            "proto": "udp", "source": self.cfg.cidr,
        })
        for rule in rules:
            self.client.mutate("POST", f"{base}/rules", data=rule)

    def finalize_machines(self, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        self._require_preflight()
        for vm in raw.vms.values():
            if not self._owns_vm(raw, vm):
                continue
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
        filename = _boot_iso_name(self.cfg.talos_version)
        volumes = {
            node: self._find_iso(node, self.provider.iso_storage, filename)
            for node in self._iso_nodes(inventory)
        }
        if not any(volumes.values()):
            info(f"image {filename} not found, nothing to remove")
            return
        warn("other clusters on the same Talos version may share this image")
        if not assume_yes and not dry_run():
            if input(f"type '{filename}' to confirm: ").strip() != filename:
                raise SystemExit("aborted")
        action(f"delete image {filename}")
        if not dry_run():
            for node, volume in volumes.items():
                if volume:
                    self._delete_volume(node, self.provider.iso_storage, volume)

    def destroy_summary(self, inventory: InfrastructureInventory) -> str:
        raw = self._raw(inventory)
        count = sum(self._owns_vm(raw, vm) for vm in raw.vms.values())
        owned_pool = raw.pools.get(self.pool_id)
        pool_count = int(owned_pool is not None and owned_pool.comment == self.pool_comment)
        return f"{count} virtual machines, {pool_count} owned resource pool"

    def destroy_resources(self, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        self._require_preflight()
        for name in sorted(list(raw.vms)):
            if self._owns_vm(raw, raw.vms[name]):
                self.delete_machine(name, inventory)
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
                if parsed.version == 4 and parsed in network:
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


def _boot_iso_name(talos_version: str) -> str:
    return f"{naming.image_name(talos_version)}.iso"


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


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"1", "true", "yes", "on"}
