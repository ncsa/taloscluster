"""Proxmox discovery, preflight ordering, ownership, and dry-run reconciliation."""

from __future__ import annotations

import json

import pytest

from taloscluster import naming
from taloscluster.config import ProxmoxSecrets, Secrets
from taloscluster.errors import ReconcileError
from taloscluster.infrastructure import Endpoint
from taloscluster.output import set_dry_run
from taloscluster.proxmox import cidata
from taloscluster.proxmox import talos as proxmox_talos
from taloscluster.proxmox.backend import ProxmoxBackend, _boot_iso_name, _memory_mib
from taloscluster.proxmox.permissions import requirements
from taloscluster.talos import factory


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


def _permissions(manage_sdn=False):
    required = requirements(
        iso_storage="isos",
        cidata_storage="local",
        vm_storage="vms",
        nodes=["pve001", "pve002"],
        network_path="/sdn/zones/localnetwork",
        manage_sdn=manage_sdn,
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
        "cluster/firewall/options": {"enable": 1},
        "nodes/pve001/qemu/800/config": {
            "net0": "virtio=02:00:00:00:00:00,bridge=vmbr0,firewall=1",
        },
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
        ("GET", "cluster/firewall/options"),
        ("GET", "nodes/pve001/qemu/800/config"),
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


def test_ensure_boot_artifact_uses_download_url(proxmox_cfg, monkeypatch):
    data = _data()
    # no existing ISO on either node — _find_iso returns ""
    data["nodes/pve001/storage/isos/content"] = []
    data["nodes/pve002/storage/isos/content"] = []
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    backend.load_inventory()
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")

    result = backend.ensure_boot_artifact()

    expected_filename = _boot_iso_name(proxmox_cfg.talos_version)
    downloads = [
        (path, payload) for method, path, payload in client.mutations
        if method == "POST" and path.endswith("/download-url")
    ]
    assert len(downloads) == 2  # one per non-shared node
    for _path, payload in downloads:
        assert payload["content"] == "iso"
        assert payload["filename"] == expected_filename
        assert payload["url"] == (
            f"https://factory.talos.dev/image/abc123/"
            f"{proxmox_cfg.talos_version}/nocloud-amd64.iso"
        )
    assert result.startswith(f"isos:iso/{expected_filename}")


def test_ensure_boot_artifact_downloads_once_for_shared_storage(proxmox_cfg, monkeypatch):
    data = _data()
    data["nodes/pve001/storage/isos/content"] = []
    data["nodes/pve002/storage/isos/content"] = []
    data["storage"][1]["shared"] = 1  # isos is shared
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    backend.load_inventory()
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")

    backend.ensure_boot_artifact()

    downloads = [
        path for method, path, _payload in client.mutations
        if method == "POST" and path.endswith("/download-url")
    ]
    assert len(downloads) == 1  # shared storage: only first node


def test_ensure_boot_artifact_skips_when_iso_exists(proxmox_cfg, monkeypatch):
    data = _data()
    filename = _boot_iso_name(proxmox_cfg.talos_version)
    volid = f"isos:iso/{filename}"
    data["nodes/pve001/storage/isos/content"] = [{"volid": volid}]
    data["nodes/pve002/storage/isos/content"] = [{"volid": volid}]
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    backend.load_inventory()
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")

    result = backend.ensure_boot_artifact()

    downloads = [
        path for method, path, _payload in client.mutations
        if method == "POST" and path.endswith("/download-url")
    ]
    assert downloads == []
    assert result == volid


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


def test_warns_when_cluster_firewall_disabled(proxmox_cfg, capsys):
    data = _data()
    data["cluster/firewall/options"] = {"enable": 0}
    _backend(proxmox_cfg, FakeClient(data)).load_inventory()
    assert "cluster firewall is not enabled" in capsys.readouterr().err


def test_warns_when_vm_nic_missing_firewall_flag(proxmox_cfg, capsys):
    data = _data()
    data["nodes/pve001/qemu/800/config"] = {
        "net0": "virtio=02:00:00:00:00:00,bridge=vmbr0",
    }
    _backend(proxmox_cfg, FakeClient(data)).load_inventory()
    err = capsys.readouterr().err
    assert "net0 does not have firewall=1" in err


def test_no_firewall_warning_when_fully_enabled(proxmox_cfg, capsys):
    _backend(proxmox_cfg, FakeClient(_data())).load_inventory()
    err = capsys.readouterr().err
    assert "firewall" not in err.lower()


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


# ---------------------------------------------------------------------------
# Stage 3: external NIC + firewall
# ---------------------------------------------------------------------------

def _external_cfg(make_config):
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
                    "cluster": {"bridge": "vmbr0"},
                    "external": {
                        "bridge": "vmbr1",
                        "vlan": 1691,
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                        "ingress_pool": "203.0.113.20-203.0.113.40",
                    },
                },
            },
        },
        remove=("openstack",),
    )


def test_current_network_returns_external_vip(make_config):
    cfg = _external_cfg(make_config)
    client = FakeClient(_data())
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()
    refs = backend.current_network(inventory)
    assert refs.kubernetes.vip == "203.0.113.10"
    assert refs.kubernetes.advertised_address == "203.0.113.10"


def test_provider_status_includes_ingress_pool(make_config):
    cfg = _external_cfg(make_config)
    client = FakeClient(_data())
    backend = _backend(cfg, client)
    backend.load_inventory()
    status = backend.provider_status()
    assert status["ingress_pool"] == "203.0.113.20-203.0.113.40"


def test_vm_create_adds_external_nic_with_firewall(make_config, monkeypatch):
    cfg = _external_cfg(make_config)
    data = _data()
    data["cluster/nextid"] = 801
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()
    monkeypatch.setattr(cidata, "build", lambda _src, dst, _h, _c: dst.write_text(""))
    monkeypatch.setattr(ProxmoxBackend, "_upload_iso", lambda _self, _n, _s, _p: None)

    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )

    creates = [
        payload for method, path, payload in client.mutations
        if method == "POST" and path.endswith("/qemu")
    ]
    assert len(creates) == 1
    payload = creates[0]
    # both NICs have firewall=1
    assert "firewall=1" in payload["net0"]
    assert "firewall=1" in payload["net1"]
    assert "vmbr1" in payload["net1"]
    assert "tag=1691" in payload["net1"]


def test_firewall_matches_openstack_security_group(make_config, monkeypatch):
    cfg = make_config(
        {
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "nodes": ["pve001", "pve002"],
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"},
                },
            },
            "security": {
                "kubernetes": {"tailscale": "100.64.0.0/10", "office": "203.0.113.0/24"},
                "talos": {"tailscale": "100.64.0.0/10"},
            },
        },
        remove=("openstack",),
    )
    data = _data()
    data["cluster/nextid"] = 801
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()
    monkeypatch.setattr(cidata, "build", lambda _src, dst, _h, _c: dst.write_text(""))
    monkeypatch.setattr(ProxmoxBackend, "_upload_iso", lambda _self, _n, _s, _p: None)

    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )

    # firewall options
    fw_opts = next(
        payload for method, path, payload in client.mutations
        if method == "PUT" and path.endswith("/firewall/options")
    )
    assert fw_opts == {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "dhcp": 1}

    # firewall rules, for the VM this run created
    rules = [
        payload for method, path, payload in client.mutations
        if method == "POST" and path == "nodes/pve002/qemu/801/firewall/rules"
    ]
    # ICMP + 80 + 443 + 50000(talos) + 6443x2(kubernetes) + intra tcp + intra udp = 8
    assert len(rules) == 8

    by_port = {(r.get("proto"), r.get("dport"), r.get("source")): r for r in rules}

    # ICMP from anywhere
    assert by_port[("icmp", None, None)]["action"] == "ACCEPT"
    # HTTP/HTTPS from anywhere
    assert by_port[("tcp", 80, None)]["action"] == "ACCEPT"
    assert by_port[("tcp", 443, None)]["action"] == "ACCEPT"
    # Talos API from allowlist
    assert by_port[("tcp", 50000, "100.64.0.0/10")]["action"] == "ACCEPT"
    # Kubernetes API from allowlist
    assert by_port[("tcp", 6443, "100.64.0.0/10")]["action"] == "ACCEPT"
    assert by_port[("tcp", 6443, "203.0.113.0/24")]["action"] == "ACCEPT"
    # Intra-cluster TCP and UDP
    assert by_port[("tcp", None, cfg.cidr)]["action"] == "ACCEPT"
    assert by_port[("udp", None, cfg.cidr)]["action"] == "ACCEPT"

    # all rules are ingress
    assert all(r["type"] == "in" for r in rules)


def test_firewall_enabled_without_external_section(proxmox_cfg, monkeypatch):
    """Stage 2 (no external) also gets the firewall, matching OpenStack SGs."""
    data = _data()
    data["cluster/nextid"] = 801
    client = FakeClient(data)
    backend = _backend(proxmox_cfg, client)
    inventory = backend.load_inventory()
    monkeypatch.setattr(cidata, "build", lambda _src, dst, _h, _c: dst.write_text(""))
    monkeypatch.setattr(ProxmoxBackend, "_upload_iso", lambda _self, _n, _s, _p: None)

    backend.reconcile_machines(
        proxmox_cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in proxmox_cfg.machines},
    )

    creates = [
        payload for method, path, payload in client.mutations
        if method == "POST" and path.endswith("/qemu")
    ]
    assert len(creates) == 1
    assert "firewall=1" in creates[0]["net0"]

    fw_calls = [
        (method, path) for method, path, _payload in client.mutations
        if "firewall" in path
    ]
    assert fw_calls  # firewall was configured even without external


# ---------------------------------------------------------------------------
# Stage 4: firewall reconciliation against the named security schema
# ---------------------------------------------------------------------------

def _firewall_cfg(make_config, security):
    return make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "nodes": ["pve001", "pve002"],
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"},
                },
            },
            "security": security,
        },
        remove=("openstack",),
    )


def _owned(**rule):
    """A firewall rule carrying this tool's ownership marker."""
    return {"type": "in", "action": "ACCEPT", "enable": 1,
            "comment": "taloscluster: rule", **rule}


def _reconcile_existing(cfg, existing_rules):
    """Reconcile the pre-existing owned VM 800 against `existing_rules`."""
    data = _data()
    data["nodes/pve001/qemu/800/firewall/rules"] = existing_rules
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()
    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )
    created = [
        payload for method, path, payload in client.mutations
        if method == "POST" and path == "nodes/pve001/qemu/800/firewall/rules"
    ]
    deleted = [
        path for method, path, _payload in client.mutations
        if method == "DELETE" and "/firewall/rules/" in path
    ]
    return created, deleted


def test_firewall_reconcile_adds_only_missing_rules(make_config):
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    existing = [
        _owned(pos=0, proto="icmp"),
        _owned(pos=1, proto="tcp", dport=50000, source="172.16.0.0/16"),
    ]

    created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []
    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("icmp", None, None) not in keys
    assert ("tcp", 50000, "172.16.0.0/16") not in keys
    assert ("tcp", 80, None) in keys
    assert ("tcp", 443, None) in keys


def test_firewall_reconcile_removes_stale_rules(make_config):
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    existing = [
        # an allowlist entry that has since been removed from cluster.yaml
        _owned(pos=0, proto="tcp", dport=50000, source="10.0.0.0/24"),
        _owned(pos=1, proto="icmp"),
    ]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/0"]


def test_firewall_reconcile_removes_duplicates_and_leaves_no_extras(make_config):
    cfg = _firewall_cfg(make_config, {})
    existing = [_owned(pos=0, proto="icmp"), _owned(pos=1, proto="icmp")]

    created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/1"]
    assert ("icmp", None, None) not in {
        (r["proto"], r.get("dport"), r.get("source")) for r in created
    }


def test_firewall_reconcile_is_idempotent(make_config):
    cfg = _firewall_cfg(make_config, {"kubernetes": {"vpn": "172.16.0.0/16"}})
    desired = _backend(cfg, FakeClient(_data()))._desired_firewall_rules()
    existing = []
    for pos, (proto, dport, source) in enumerate(desired):
        rule = _owned(pos=pos, proto=proto)
        if dport is not None:
            rule["dport"] = dport
        if source is not None:
            rule["source"] = source
        existing.append(rule)

    created, deleted = _reconcile_existing(cfg, existing)

    assert created == []
    assert deleted == []


def test_firewall_reconcile_covers_arbitrary_named_ports(make_config):
    cfg = _firewall_cfg(make_config, {
        "metrics": {"port": 9100, "hosts": {"vpn": "172.16.0.0/16"}},
    })

    created, _deleted = _reconcile_existing(cfg, [])

    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("tcp", 9100, "172.16.0.0/16") in keys


def test_firewall_http_block_restricts_port_80(make_config):
    cfg = _firewall_cfg(make_config, {"http": {"hosts": {"office": "203.0.113.0/24"}}})

    created, _deleted = _reconcile_existing(cfg, [])

    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("tcp", 80, None) not in keys
    assert ("tcp", 80, "203.0.113.0/24") in keys
    assert ("tcp", 443, None) in keys


def test_firewall_reconcile_leaves_egress_rules_alone(make_config):
    cfg = _firewall_cfg(make_config, {})
    existing = [_owned(pos=0, type="out", proto="tcp")]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []


def test_firewall_reconcile_makes_no_mutations_in_dry_run(make_config):
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    data = _data()
    data["nodes/pve001/qemu/800/firewall/rules"] = [
        {"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
         "proto": "tcp", "dport": 50000, "source": "10.0.0.0/24"},
    ]
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()
    set_dry_run(True)

    backend.reconcile_machines(cfg.machines, inventory, "isos:iso/talos.iso", {})

    assert not [m for m in client.mutations if "firewall" in m[1]]


def test_firewall_reconcile_removes_an_owned_port_range_rule(make_config):
    """A marked rule we could never write today is removed, not crashed on."""
    cfg = _firewall_cfg(make_config, {})
    existing = [_owned(pos=0, proto="tcp", dport="8000:8100")]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/0"]


def test_anchor_collision_check_runs_once_per_backend(make_config, monkeypatch):
    """The whole-cluster anchor check is O(1) per converge, not O(N) machines."""
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0"},
                    "external": {
                        "bridge": "vmbr1",
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    backend = _backend(cfg, FakeClient(_data()))
    calls = []
    monkeypatch.setattr(
        proxmox_talos, "anchor_addresses",
        lambda cidr, cluster, machines: calls.append(cluster) or {},
    )

    for machine in cfg.machines.values():
        backend.talos_contribution(machine, Endpoint(vip="203.0.113.10"))

    assert len(calls) == 1


def test_firewall_reconcile_never_deletes_an_operator_rule(make_config, capsys):
    """A per-VM firewall is shared; an unmarked rule is reported, not removed."""
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    # an admin's own SSH allowance, added through the Proxmox UI
    existing = [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
                 "proto": "tcp", "dport": 22, "source": "198.51.100.0/24"}]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []
    assert "leaving unowned firewall rule 0" in capsys.readouterr().err


def test_firewall_reconcile_keeps_an_operator_port_range_rule(make_config):
    cfg = _firewall_cfg(make_config, {})
    existing = [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
                 "proto": "tcp", "dport": "8000:8100"}]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []


def test_firewall_reconcile_does_not_duplicate_an_operator_rule(make_config):
    """An unowned rule that already allows what we want is left to do the job."""
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    existing = [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
                 "proto": "tcp", "dport": 50000, "source": "172.16.0.0/16"}]

    created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []
    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("tcp", 50000, "172.16.0.0/16") not in keys


def test_firewall_rules_are_created_with_the_ownership_marker(make_config):
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})

    created, _deleted = _reconcile_existing(cfg, [])

    assert created
    assert all(r["comment"].startswith("taloscluster: ") for r in created)
    assert any(r["comment"] == "taloscluster: talos from vpn" for r in created)


def test_firewall_reconcile_refuses_a_non_list_rule_response(make_config):
    """A malformed response must not read as 'this VM has no rules'."""
    cfg = _firewall_cfg(make_config, {})
    data = _data()
    data["nodes/pve001/qemu/800/firewall/rules"] = {"error": "boom"}
    backend = _backend(cfg, FakeClient(data))
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="no firewall rule list"):
        backend.reconcile_machines(
            cfg.machines, inventory, "isos:iso/talos.iso",
            {name: "config" for name in cfg.machines},
        )


def test_firewall_removes_a_stale_legacy_rule_written_without_the_marker(make_config):
    """0.4.0 wrote unmarked rules; dropping a CIDR must still close its port."""
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    # written by 0.4.0 for an allowlist entry since replaced
    existing = [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
                 "proto": "tcp", "dport": 50000, "source": "10.0.0.0/24"}]

    created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/0"]
    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("tcp", 50000, "172.16.0.0/16") in keys


def test_firewall_removes_rules_for_a_port_no_longer_in_the_config(make_config):
    """Deleting a whole named rule takes its port out of the managed set."""
    cfg = _firewall_cfg(make_config, {})
    # our own rule for a `metrics` block that has since been deleted
    existing = [_owned(pos=0, proto="tcp", dport=9100, source="172.16.0.0/16")]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/0"]


def test_firewall_keeps_an_operator_rule_on_an_unmanaged_port(make_config):
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    existing = [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1,
                 "proto": "tcp", "dport": 22, "source": "198.51.100.0/24"}]

    _created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == []


def test_firewall_replaces_a_disabled_rule_of_ours(make_config):
    """A disabled rule allows nothing, so it is stale rather than satisfying."""
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "172.16.0.0/16"}})
    existing = [_owned(pos=0, proto="tcp", dport=50000,
                       source="172.16.0.0/16", enable=0)]

    created, deleted = _reconcile_existing(cfg, existing)

    assert deleted == ["nodes/pve001/qemu/800/firewall/rules/0"]
    keys = {(r["proto"], r.get("dport"), r.get("source")) for r in created}
    assert ("tcp", 50000, "172.16.0.0/16") in keys
    assert all(r["enable"] == 1 for r in created)


def test_firewall_adds_before_deleting(make_config):
    """Swapping an allowlist CIDR must never leave the port closed in between."""
    cfg = _firewall_cfg(make_config, {"talos": {"vpn": "10.1.0.0/24"}})
    data = _data()
    data["nodes/pve001/qemu/800/firewall/rules"] = [
        _owned(pos=0, proto="tcp", dport=50000, source="10.0.0.0/24"),
    ]
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )

    order = [
        method for method, path, _payload in client.mutations
        if "/firewall/rules" in path
    ]
    assert order, "no firewall rule mutations"
    assert order.index("POST") < order.index("DELETE")


def test_firewall_options_are_not_rewritten_when_already_correct(make_config):
    cfg = _firewall_cfg(make_config, {})
    data = _data()
    data["nodes/pve001/qemu/800/firewall/options"] = {
        "enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "dhcp": 1,
    }
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )

    assert not [
        m for m in client.mutations if m[0] == "PUT" and m[1].endswith("/firewall/options")
    ]


def test_firewall_options_are_set_when_they_drift(make_config):
    cfg = _firewall_cfg(make_config, {})
    data = _data()
    data["nodes/pve001/qemu/800/firewall/options"] = {
        "enable": 1, "policy_in": "ACCEPT", "policy_out": "ACCEPT", "dhcp": 1,
    }
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_machines(
        cfg.machines, inventory, "isos:iso/talos.iso",
        {name: "config" for name in cfg.machines},
    )

    put = next(
        payload for method, path, payload in client.mutations
        if method == "PUT" and path.endswith("/firewall/options")
    )
    assert put["policy_in"] == "DROP"


# ---------------------------------------------------------------------------
# managed SDN (EVPN)
# ---------------------------------------------------------------------------

# SDN ids default to the cluster name; "testcluster" exceeds the 8-char limit
SDN_CLUSTER = "testc"
ZONE_ID = SDN_CLUSTER
VNET_ID = SDN_CLUSTER
SDN_ALIAS = naming.sdn_alias(SDN_CLUSTER)


@pytest.fixture
def sdn_cfg(make_config):
    return make_config(
        {
            "name": SDN_CLUSTER,
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "nodes": ["pve001", "pve002"],
                "network": {"cluster": {"sdn": {}, "kubeapi_vip": "192.168.0.9"}},
            },
        },
        remove=("openstack",),
    )


def _sdn_data():
    base = _data(permissions=_permissions(manage_sdn=True))
    # rebase the canonical inventory onto the short SDN cluster name
    data = json.loads(json.dumps(base).replace("testcluster", SDN_CLUSTER))
    data["cluster/sdn/zones"] = []
    data["cluster/sdn/vnets"] = []
    data["cluster/sdn/controllers"] = []
    data[f"cluster/sdn/vnets/{VNET_ID}/subnets"] = []
    data["cluster/status"] = [
        {"type": "node", "name": "pve001", "ip": "10.10.0.1"},
        {"type": "node", "name": "pve002", "ip": "10.10.0.2"},
    ]
    data["nodes/pve001/network"] = [{"iface": VNET_ID}]
    data["nodes/pve002/network"] = [{"iface": VNET_ID}]
    return data


def _sdn_converged_data(sdn):
    """Inventory echoing our applied objects, with Proxmox-style defaults."""
    data = _sdn_data()
    data["cluster/sdn/controllers"] = [
        {"controller": "evpnctl", "type": "evpn", "asn": 65000, "peers": "10.10.0.1,10.10.0.2"}
    ]
    data["cluster/sdn/zones"] = [
        {
            "zone": ZONE_ID,
            "type": "evpn",
            "controller": "evpnctl",
            "vrf-vxlan": sdn.vrf_tag,
            # node lists echo in arbitrary order, booleans echo as ints,
            # unconfigured fields echo their effective defaults
            "exitnodes": "pve002,pve001",
            "exitnodes-primary": "pve001",
            "advertise-subnets": 1,
            "disable-arp-nd-suppression": 1,
            "mtu": 1450,
            "ipam": "pve",
        }
    ]
    data["cluster/sdn/vnets"] = [
        {"vnet": VNET_ID, "zone": ZONE_ID, "tag": sdn.tag, "alias": SDN_ALIAS}
    ]
    data[f"cluster/sdn/vnets/{VNET_ID}/subnets"] = [
        {
            "subnet": f"{ZONE_ID}-192.168.0.0-21",
            "cidr": "192.168.0.0/21",
            "gateway": "192.168.0.1",
            "snat": 1,
        }
    ]
    return data


def test_sdn_first_converge_stages_in_dependency_order_then_applies(sdn_cfg):
    client = FakeClient(_sdn_data())
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("POST", "cluster/sdn/controllers"),
        ("POST", "cluster/sdn/zones"),
        ("POST", "cluster/sdn/vnets"),
        ("POST", f"cluster/sdn/vnets/{VNET_ID}/subnets"),
        ("PUT", "cluster/sdn"),
    ]
    controller = client.mutations[0][2]
    assert controller == {
        "controller": "evpnctl",
        "type": "evpn",
        "asn": 65000,
        "peers": "10.10.0.1,10.10.0.2",
    }
    zone = client.mutations[1][2]
    assert zone["zone"] == ZONE_ID and zone["type"] == "evpn"
    assert zone["controller"] == "evpnctl"
    assert zone["vrf-vxlan"] == backend.sdn.vrf_tag
    assert zone["exitnodes"] == "pve001,pve002"
    assert zone["exitnodes-primary"] == "pve001"  # SNAT needs a primary
    assert zone["advertise-subnets"] == 1
    assert zone["disable-arp-nd-suppression"] == 1
    vnet = client.mutations[2][2]
    assert vnet == {
        "vnet": VNET_ID,
        "zone": ZONE_ID,
        "tag": backend.sdn.tag,
        "alias": SDN_ALIAS,
    }
    subnet = client.mutations[3][2]
    assert subnet == {
        "subnet": "192.168.0.0/21",
        "type": "subnet",
        "gateway": "192.168.0.1",
        "snat": 1,
    }


def test_sdn_second_converge_makes_no_mutations(sdn_cfg):
    client = FakeClient(_sdn_converged_data(_backend(sdn_cfg, FakeClient({})).sdn))
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert client.mutations == []


def test_sdn_plan_makes_only_reads(sdn_cfg, capsys):
    client = FakeClient(_sdn_data())
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()
    set_dry_run(True)

    backend.reconcile_network(sdn_cfg.machines, inventory)

    output = capsys.readouterr().out
    assert f"[dry-run] create SDN zone {ZONE_ID}" in output
    assert f"[dry-run] create SDN vnet {VNET_ID}" in output
    assert "[dry-run] apply SDN configuration" in output
    assert all(method == "GET" for method, _path in client.calls)


def test_sdn_net0_attaches_to_derived_vnet(sdn_cfg):
    backend = _backend(sdn_cfg, FakeClient(_sdn_data()))
    assert backend.cluster_link == VNET_ID


def test_sdn_current_network_resolves_static_addresses_without_guest_agent(sdn_cfg):
    backend = _backend(sdn_cfg, FakeClient(_sdn_data()))
    inventory = backend.load_inventory()

    network = backend.current_network(inventory)

    assert network.kubernetes.vip == "192.168.0.9"
    assert network.machine_address("testc-controlplane-01") == "192.168.0.11"
    assert network.machine_address("testc-controlplane-02") == "192.168.0.12"


def test_sdn_foreign_pending_changes_refuse_before_any_mutation(sdn_cfg):
    data = _sdn_data()
    data["cluster/sdn/zones"] = [{"zone": "otherz1", "type": "vlan", "state": "new"}]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="unapplied Proxmox SDN changes"):
        backend.reconcile_network(sdn_cfg.machines, inventory)

    assert client.mutations == []


def test_sdn_own_pending_changes_resume_with_a_single_apply(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    for key in ("cluster/sdn/zones", "cluster/sdn/vnets"):
        data[key][0]["state"] = "new"
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("PUT", "cluster/sdn")
    ]


def test_sdn_refuses_zone_containing_foreign_vnet(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/vnets"] = [
        {"vnet": "other1", "zone": ZONE_ID, "tag": 999, "alias": "someone elses"}
    ]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="foreign VNets"):
        backend.reconcile_network(sdn_cfg.machines, inventory)
    assert client.mutations == []


def test_sdn_refuses_empty_zone_with_matching_id_but_foreign_settings(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/zones"][0]["controller"] = "someone"
    data["cluster/sdn/vnets"] = []
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="empty unowned SDN zone"):
        backend.reconcile_network(sdn_cfg.machines, inventory)
    assert client.mutations == []


def test_sdn_adopts_own_interrupted_empty_zone(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/vnets"] = []
    data[f"cluster/sdn/vnets/{VNET_ID}/subnets"] = []
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("POST", "cluster/sdn/vnets"),
        ("POST", f"cluster/sdn/vnets/{VNET_ID}/subnets"),
        ("PUT", "cluster/sdn"),
    ]


def test_sdn_refuses_vni_collision_with_foreign_zone(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_data()
    data["cluster/sdn/zones"] = [
        {"zone": "otherz1", "type": "evpn", "vrf-vxlan": backend_probe.sdn.vrf_tag}
    ]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="already used by zone"):
        backend.reconcile_network(sdn_cfg.machines, inventory)
    assert client.mutations == []


def test_sdn_existing_controller_is_used_untouched_with_asn_warning(sdn_cfg, capsys):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/controllers"][0]["asn"] = 65001
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert client.mutations == []
    assert "has ASN 65001" in capsys.readouterr().err


def test_sdn_wrong_type_controller_is_an_error(sdn_cfg):
    data = _sdn_data()
    data["cluster/sdn/controllers"] = [{"controller": "evpnctl", "type": "bgp", "asn": 65000}]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="not an EVPN controller"):
        backend.reconcile_network(sdn_cfg.machines, inventory)
    assert client.mutations == []


def test_sdn_reads_happen_only_after_permission_preflight(sdn_cfg):
    client = FakeClient(_data(permissions=_permissions()))  # no SDN.Allocate/Audit

    with pytest.raises(ReconcileError, match="missing required effective permissions"):
        _backend(sdn_cfg, client).load_inventory()

    paths = [path for _method, path in client.calls]
    assert all(not path.startswith("cluster/sdn") for path in paths)
    assert all(method == "GET" for method, _path in client.calls)


def test_sdn_renumber_guard_refuses_to_reconfigure_a_running_node(sdn_cfg):
    data = _sdn_converged_data(_backend(sdn_cfg, FakeClient({})).sdn)
    data["nodes/pve001/qemu/800/agent/network-get-interfaces"] = {
        "result": [
            {"name": "eth0", "ip-addresses": [{"ip-address": "192.168.1.23"}]}
        ]
    }
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="static addresses"):
        backend.reconcile_network(sdn_cfg.machines, inventory)

    set_dry_run(True)
    backend.reconcile_network(sdn_cfg.machines, inventory)  # plan only reports
    assert client.mutations == []


def test_sdn_destroy_removes_subnet_vnet_zone_and_applies_once(sdn_cfg):
    data = _sdn_converged_data(_backend(sdn_cfg, FakeClient({})).sdn)
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.destroy_resources(inventory)

    sdn_mutations = [
        (method, path)
        for method, path, _payload in client.mutations
        if path.startswith("cluster/sdn")
    ]
    assert sdn_mutations == [
        ("DELETE", f"cluster/sdn/vnets/{VNET_ID}/subnets/{ZONE_ID}-192.168.0.0-21"),
        ("DELETE", f"cluster/sdn/vnets/{VNET_ID}"),
        ("DELETE", f"cluster/sdn/zones/{ZONE_ID}"),
        ("PUT", "cluster/sdn"),
    ]
    assert all(
        "controllers" not in path for _method, path, _payload in client.mutations
    )


def test_sdn_destroy_keeps_zone_holding_a_foreign_vnet(sdn_cfg, capsys):
    data = _sdn_converged_data(_backend(sdn_cfg, FakeClient({})).sdn)
    data["cluster/sdn/vnets"].append(
        {"vnet": "other1", "zone": ZONE_ID, "tag": 999, "alias": "someone elses"}
    )
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.destroy_resources(inventory)

    sdn_mutations = [
        (method, path)
        for method, path, _payload in client.mutations
        if path.startswith("cluster/sdn")
    ]
    assert ("DELETE", f"cluster/sdn/zones/{ZONE_ID}") not in sdn_mutations
    assert ("DELETE", f"cluster/sdn/vnets/{VNET_ID}") in sdn_mutations
    assert f"leaving SDN zone {ZONE_ID}" in capsys.readouterr().err


def test_sdn_destroy_summary_mentions_the_managed_network(sdn_cfg):
    backend = _backend(sdn_cfg, FakeClient(_sdn_data()))
    inventory = backend.load_inventory()
    assert "managed SDN network" in backend.destroy_summary(inventory)


def test_sdn_default_exit_nodes_include_offline_cluster_nodes(make_config):
    cfg = make_config(
        {
            "name": SDN_CLUSTER,
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "network": {"cluster": {"sdn": {}, "kubeapi_vip": "192.168.0.9"}},
            },
        },
        remove=("openstack",),
    )
    data = _sdn_data()
    # an offline node must stay in the default exit-node set, or a down node
    # would drift the zone on every converge and flip the SNAT primary
    data["nodes"].append({"node": "pve003", "status": "offline"})
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(cfg.machines, inventory)

    zone = next(
        payload
        for method, path, payload in client.mutations
        if (method, path) == ("POST", "cluster/sdn/zones")
    )
    assert zone["exitnodes"] == "pve001,pve002,pve003"
    assert zone["exitnodes-primary"] == "pve001"


def test_sdn_foreign_pending_subnet_refuses(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/vnets"].append(
        {"vnet": "other1", "zone": "otherz1", "tag": 999, "alias": "someone elses"}
    )
    data["cluster/sdn/vnets/other1/subnets"] = [
        {"subnet": "otherz1-10.9.0.0-24", "cidr": "10.9.0.0/24", "state": "new"}
    ]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="unapplied Proxmox SDN changes"):
        backend.reconcile_network(sdn_cfg.machines, inventory)
    assert client.mutations == []


def test_sdn_pending_subnet_resumes_with_a_single_apply(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data[f"cluster/sdn/vnets/{VNET_ID}/subnets"][0]["state"] = "new"
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("PUT", "cluster/sdn")
    ]


def test_sdn_pending_controller_resumes_with_a_single_apply(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    data["cluster/sdn/controllers"][0]["state"] = "new"
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("PUT", "cluster/sdn")
    ]


def test_sdn_tolerates_missing_subnet_endpoint_on_never_applied_vnet(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    # zone + vnet staged by an interrupted run but never applied
    data["cluster/sdn/zones"][0]["state"] = "new"
    data["cluster/sdn/vnets"][0]["state"] = "new"

    class RaisingClient(FakeClient):
        def get(self, path, **kwargs):
            if path.endswith("/subnets"):
                self.calls.append(("GET", path))
                raise ReconcileError("404 no such vnet")
            return super().get(path, **kwargs)

    client = RaisingClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert [(method, path) for method, path, _payload in client.mutations] == [
        ("POST", f"cluster/sdn/vnets/{VNET_ID}/subnets"),
        ("PUT", "cluster/sdn"),
    ]


def test_sdn_offline_default_primary_exit_node_warns(make_config, capsys):
    cfg = make_config(
        {
            "name": SDN_CLUSTER,
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "network": {"cluster": {"sdn": {}, "kubeapi_vip": "192.168.0.9"}},
            },
        },
        remove=("openstack",),
    )
    data = _sdn_data()
    # sorts first, so it becomes the default primary while being offline
    data["nodes"].append({"node": "aaa1", "status": "offline"})
    client = FakeClient(data)
    backend = _backend(cfg, client)
    inventory = backend.load_inventory()

    backend.reconcile_network(cfg.machines, inventory)

    assert "primary EVPN exit node aaa1 is offline" in capsys.readouterr().err
    zone = next(
        payload
        for method, path, payload in client.mutations
        if (method, path) == ("POST", "cluster/sdn/zones")
    )
    assert zone["exitnodes-primary"] == "aaa1"


def test_sdn_stale_zone_node_restriction_still_enforced(sdn_cfg, capsys):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    # cluster.yaml no longer sets sdn.nodes, but the applied zone still
    # restricts membership; compute node pve002 is outside it
    data["cluster/sdn/zones"][0]["nodes"] = "pve001"
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="outside the SDN zone"):
        backend.reconcile_network(sdn_cfg.machines, inventory)

    assert "keeps a node restriction" in capsys.readouterr().err
    assert client.mutations == []


def test_sdn_bridge_verify_retries_before_failing(sdn_cfg, monkeypatch):
    data = _sdn_data()
    data["nodes/pve002/network"] = []  # bridge never shows up on pve002
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()
    sleeps: list[int] = []
    monkeypatch.setattr(
        "taloscluster.proxmox.backend.time.sleep", lambda s: sleeps.append(s)
    )

    with pytest.raises(ReconcileError, match="missing after apply on: pve002"):
        backend.reconcile_network(sdn_cfg.machines, inventory)

    assert len(sleeps) == 4  # the apply reload is async; retried before failing


def test_sdn_destroy_tolerates_missing_subnet_endpoint_on_pending_vnet(sdn_cfg):
    backend_probe = _backend(sdn_cfg, FakeClient({}))
    data = _sdn_converged_data(backend_probe.sdn)
    # interrupted converge: zone + vnet staged, never applied
    data["cluster/sdn/zones"][0]["state"] = "new"
    data["cluster/sdn/vnets"][0]["state"] = "new"

    class RaisingClient(FakeClient):
        def get(self, path, **kwargs):
            if path.endswith("/subnets"):
                self.calls.append(("GET", path))
                raise ReconcileError("404 no such vnet")
            return super().get(path, **kwargs)

    client = RaisingClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    backend.destroy_resources(inventory)

    sdn_mutations = [
        (method, path)
        for method, path, _payload in client.mutations
        if path.startswith("cluster/sdn")
    ]
    assert sdn_mutations == [
        ("DELETE", f"cluster/sdn/vnets/{VNET_ID}"),
        ("DELETE", f"cluster/sdn/zones/{ZONE_ID}"),
        ("PUT", "cluster/sdn"),
    ]


def test_sdn_offline_configured_exit_node_warns(sdn_cfg, capsys):
    data = _sdn_data()
    # pve002 is a configured exit node (default = proxmox.nodes) but offline;
    # proxmox.nodes only requires... make it a non-required node instead
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()
    backend._inventory.nodes["pve002"] = type(backend._inventory.nodes["pve002"])(
        name="pve002", online=False
    )

    backend.reconcile_network(sdn_cfg.machines, inventory)

    assert "offline EVPN exit nodes: pve002" in capsys.readouterr().err


def test_sdn_foreign_same_named_vnet_fails_before_staging_anything(sdn_cfg):
    # the ids are now the cluster name, so operator collisions are plausible;
    # nothing may be staged (not even the controller) before the refusal
    data = _sdn_data()
    data["cluster/sdn/vnets"] = [
        {"vnet": VNET_ID, "zone": "otherz1", "tag": 999, "alias": "someone elses"}
    ]
    client = FakeClient(data)
    backend = _backend(sdn_cfg, client)
    inventory = backend.load_inventory()

    with pytest.raises(ReconcileError, match="refusing to adopt unowned SDN vnet"):
        backend.reconcile_network(sdn_cfg.machines, inventory)

    assert client.mutations == []


def test_sdn_name_override_becomes_the_vnet_bridge(make_config):
    cfg = make_config(
        {
            "name": SDN_CLUSTER,
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"sdn": {"name": "grid"}, "kubeapi_vip": "192.168.0.9"}
                },
            },
        },
        remove=("openstack",),
    )
    assert _backend(cfg, FakeClient({})).cluster_link == "grid"
