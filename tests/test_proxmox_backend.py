"""Proxmox discovery, preflight ordering, ownership, and dry-run reconciliation."""

from __future__ import annotations

import pytest

from taloscluster.config import ProxmoxSecrets, Secrets
from taloscluster.errors import ReconcileError
from taloscluster.output import set_dry_run
from taloscluster.proxmox import cidata
from taloscluster.proxmox.backend import ProxmoxBackend, _boot_iso_name, _memory_mib
from taloscluster.proxmox.permissions import requirements


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.mutations = []  # (method, path, data)

    def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        return self.data.get(path, [])

    def mutate(self, method, path, **kwargs):
        self.calls.append((method, path))
        self.mutations.append((method, path, kwargs.get("data")))
        return None


@pytest.fixture(autouse=True)
def reset_dry_run():
    set_dry_run(False)
    yield
    set_dry_run(False)


@pytest.fixture
def proxmox_cfg(make_config):
    return make_config(
        {
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "nodes": ["pve001", "pve002"],
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )


def _permissions():
    required = requirements(
        iso_storage="isos",
        cidata_storage="local",
        vm_storage="vms",
        nodes=["pve001", "pve002"],
        network_path="/sdn/zones/localnetwork",
    )
    privileges = set().union(*(item.privileges for item in required))
    return {"/": {privilege: 1 for privilege in privileges}}


def _data(permissions=None):
    return {
        "nodes": [
            {"node": "pve001", "status": "online", "mem": 0, "maxmem": 32 * 1024**3},
            {"node": "pve002", "status": "online", "mem": 0, "maxmem": 32 * 1024**3},
        ],
        "storage": [
            {"storage": "vms", "content": "images"},
            {"storage": "isos", "content": "iso"},
            {"storage": "local", "content": "iso"},
        ],
        "pools": [
            {
                "poolid": "taloscluster-testcluster",
                "comment": "managed-by=taloscluster cluster=testcluster",
            }
        ],
        "cluster/resources": [
            {
                "type": "qemu",
                "vmid": 800,
                "name": "testcluster-controlplane-01",
                "node": "pve001",
                "status": "running",
                "pool": "taloscluster-testcluster",
                "tags": "taloscluster;cluster_testcluster;role_controlplane;pool_controlplane",
            },
            {
                "type": "qemu",
                "vmid": 999,
                "name": "foreign",
                "node": "pve002",
                "status": "running",
                "tags": "unmanaged",
            },
        ],
        "access/permissions": _permissions() if permissions is None else permissions,
    }


def _backend(cfg, client):
    secrets = Secrets(provider=ProxmoxSecrets("user@pve!provider", "secret"))
    return ProxmoxBackend(cfg, secrets, client=client)


def test_inventory_uses_bulk_reads_and_only_exposes_owned_vms(proxmox_cfg):
    client = FakeClient(_data())

    inventory = _backend(proxmox_cfg, client).load_inventory()

    assert sorted(inventory.machines) == ["testcluster-controlplane-01"]
    assert inventory.resources["vms"] == ["testcluster-controlplane-01"]
    assert client.calls == [
        ("GET", "nodes"),
        ("GET", "storage"),
        ("GET", "pools"),
        ("GET", "cluster/resources"),
        ("GET", "access/permissions"),
        ("GET", "nodes/pve001/qemu/800/agent/network-get-interfaces"),
    ]


def test_inventory_uses_guest_agent_private_address(proxmox_cfg):
    data = _data()
    data["nodes/pve001/qemu/800/agent/network-get-interfaces"] = {
        "result": [
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address": "127.0.0.1", "ip-address-type": "ipv4"},
                    {"ip-address": "192.168.1.23", "ip-address-type": "ipv4"},
                ],
            }
        ]
    }

    inventory = _backend(proxmox_cfg, FakeClient(data)).load_inventory()

    assert inventory.machine_address("testcluster-controlplane-01") == "192.168.1.23"


def test_missing_permission_fails_before_any_mutation(proxmox_cfg):
    client = FakeClient(_data(permissions={"/": {"Sys.Audit": 1}}))

    with pytest.raises(ReconcileError, match="missing required effective permissions"):
        _backend(proxmox_cfg, client).load_inventory()

    assert all(method == "GET" for method, _path in client.calls)


def test_plan_reports_missing_vm_without_mutating_proxmox(proxmox_cfg, capsys):
    client = FakeClient(_data())
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()
    set_dry_run(True)

    backend.reconcile_machines(
        proxmox_cfg.machines,
        inventory,
        "isos:iso/talos.iso",
        {},
    )

    output = capsys.readouterr().out
    assert "[dry-run] create server testcluster-controlplane-02" in output
    assert "(4 cores, 8GB RAM, 40GB disk)" in output
    assert all(method == "GET" for method, _path in client.calls)


def test_cluster_memory_gb_converts_to_proxmox_api_mib():
    assert _memory_mib(8) == 8192


def test_boot_iso_uses_shared_tailscale_image_name():
    assert _boot_iso_name("v1.12.2") == "talos-v1.12.2-tailscale.iso"


def test_same_named_unowned_vm_is_never_adopted(proxmox_cfg):
    data = _data()
    data["cluster/resources"][0]["tags"] = "unmanaged"
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="refusing to adopt unowned"):
        backend.reconcile_machines(
            proxmox_cfg.machines,
            inventory,
            "isos:iso/talos.iso",
            {name: "config" for name in proxmox_cfg.machines},
        )

    assert all(method == "GET" for method, _path in client.calls)


def test_owned_tags_in_foreign_pool_are_not_sufficient_for_adoption(proxmox_cfg):
    data = _data()
    data["pools"][0]["comment"] = "owned by somebody else"
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    assert inventory.machines == {}
    with pytest.raises(ReconcileError, match="refusing to adopt unowned"):
        backend.reconcile_machines(
            proxmox_cfg.machines,
            inventory,
            "isos:iso/talos.iso",
            {name: "config" for name in proxmox_cfg.machines},
        )
    assert all(method == "GET" for method, _path in client.calls)


def test_owned_stopped_vm_is_started_on_resume(proxmox_cfg):
    data = _data()
    data["cluster/resources"][0]["status"] = "stopped"
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()
    cp1 = proxmox_cfg.machines["testcluster-controlplane-01"]

    backend.reconcile_machines(
        {cp1.name: cp1},
        inventory,
        "isos:iso/talos.iso",
        {},
    )

    assert ("POST", "nodes/pve001/qemu/800/status/start") in client.calls


def test_running_vm_is_stopped_before_delete(proxmox_cfg):
    data = _data()
    data["nodes/pve001/qemu/800/status/current"] = {"status": "running"}
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    backend.delete_machine("testcluster-controlplane-01", inventory)

    calls = client.calls
    stop = ("POST", "nodes/pve001/qemu/800/status/stop")
    delete = ("DELETE", "nodes/pve001/qemu/800")
    assert stop in calls
    assert calls.index(stop) < calls.index(delete)


def test_stopped_vm_is_deleted_without_stop(proxmox_cfg):
    data = _data()
    data["nodes/pve001/qemu/800/status/current"] = {"status": "stopped"}
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    backend.delete_machine("testcluster-controlplane-01", inventory)

    assert ("POST", "nodes/pve001/qemu/800/status/stop") not in client.calls
    assert ("DELETE", "nodes/pve001/qemu/800") in client.calls


def test_vm_stopped_by_reset_skips_stop(proxmox_cfg):
    # Cached inventory says "running" but the VM was shut down by talosctl reset.
    data = _data()
    data["nodes/pve001/qemu/800/status/current"] = {"status": "stopped"}
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    backend.delete_machine("testcluster-controlplane-01", inventory)

    assert ("POST", "nodes/pve001/qemu/800/status/stop") not in client.calls
    assert ("DELETE", "nodes/pve001/qemu/800") in client.calls


def test_failed_refresh_clears_previous_permission_preflight(proxmox_cfg):
    data = _data()
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    backend.load_inventory()
    data["access/permissions"] = {"/": {"Sys.Audit": 1}}

    with pytest.raises(ReconcileError, match="missing required effective permissions"):
        backend.load_inventory()

    assert backend._preflight_complete is False


def test_cidata_storage_must_be_node_local(proxmox_cfg):
    data = _data()
    data["storage"][2]["shared"] = 1

    with pytest.raises(ReconcileError, match="cidata_storage must be node-local"):
        _backend(proxmox_cfg, FakeClient(data)).load_inventory()


def test_nonshared_boot_iso_is_cached_on_each_compute_node(proxmox_cfg):
    client = FakeClient(_data())
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    assert backend._iso_nodes(inventory.provider_data) == ["pve001", "pve002"]

    inventory.provider_data.storages["isos"]["shared"] = 1
    assert backend._iso_nodes(inventory.provider_data) == ["pve001"]


def test_vm_create_uses_uefi_q35_with_efi_disk(proxmox_cfg, monkeypatch, tmp_path):
    data = _data()
    data["cluster/nextid"] = 801
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()

    # Avoid real ISO creation and upload
    monkeypatch.setattr(cidata, "build", lambda _src, dst, _h, _c: dst.write_text(""))
    monkeypatch.setattr(ProxmoxBackend, "_upload_iso", lambda _self, _n, _s, _p: None)

    backend.reconcile_machines(
        proxmox_cfg.machines,
        inventory,
        "isos:iso/talos.iso",
        {name: "config" for name in proxmox_cfg.machines},
    )

    creates = [
        (method, path, payload)
        for method, path, payload in client.mutations
        if method == "POST" and path.endswith("/qemu")
    ]
    assert len(creates) == 1
    payload = creates[0][2]
    assert payload["bios"] == "ovmf"
    assert payload["machine"] == "q35"
    assert "efidisk0" in payload
    assert payload["efidisk0"].startswith("vms:1,efitype=4m")
    assert payload["scsihw"] == "virtio-scsi-single"
    assert payload["boot"] == "order=scsi0;ide2"
