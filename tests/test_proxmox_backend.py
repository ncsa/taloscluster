"""Proxmox discovery, preflight ordering, ownership, and dry-run reconciliation."""

from __future__ import annotations

import pytest

from taloscluster.config import ProxmoxSecrets, Secrets
from taloscluster.errors import ReconcileError
from taloscluster.output import set_dry_run
from taloscluster.proxmox import cidata
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

    # firewall rules
    rules = [
        payload for method, path, payload in client.mutations
        if method == "POST" and path.endswith("/firewall/rules")
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
