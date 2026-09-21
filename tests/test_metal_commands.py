"""The `taloscluster metal` commands, against stubbed Redfish and talosctl.

The join flow is boot -> wait -> apply -> eject -> verify: the Redfish client
is stubbed at the commands boundary for the flow tests, and the real client's
request shapes (one-time boot patch, virtual media insert/eject, power reset,
the inspect summary) are pinned against a route table instead of a BMC.
"""

from __future__ import annotations

import urllib.parse

import pytest
import requests

from taloscluster import converge
from taloscluster.config import MetalBmc
from taloscluster.errors import ReconcileError
from taloscluster.k8s import kubectl
from taloscluster.metal import commands, redfish

VIP = "172.29.21.200"
ISO_URL = "https://factory.talos.dev/image/abc123/v1.13.9/nocloud-amd64.iso"

GROUP = {
    "role": "worker",
    "redfish": True,
    "disk": "/dev/sda",
    "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1"},
    "interfaces": {
        "enp1s0f0": {"role": "pxe"},
        "enp2s0f0": {"role": "cluster"},
    },
    "bmc": {"username": "root", "password": "hunter2"},
    "servers": {
        "rp001": {
            "bmc": {"ip": "198.51.100.10"},
            "interfaces": {"enp2s0f0": {"ip": "172.29.21.5/24"}},
        },
    },
}


def _cfg(make_config, metal=None):
    return make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {
                "cluster": {
                    "cidr": "172.29.21.0/24", "gateway": "172.29.21.1",
                    "kubeapi_vip": VIP,
                },
                "dns": ["192.0.2.53"],
            },
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
            "metal": {"phoenix": GROUP if metal is None else metal},
        },
        remove=("openstack",),
    )


class FakeRedfish:
    """Records the calls the commands make, in order."""

    pre_mounted = False

    def __init__(self, bmc):
        self.bmc = bmc
        self.calls: list = []

    def eject_media(self) -> bool:
        self.calls.append("eject")
        return self.pre_mounted

    def insert_media(self, url: str) -> None:
        self.calls.append(("insert", url))

    def boot_once_cd(self) -> None:
        self.calls.append("boot-once")

    def power_on(self) -> None:
        self.calls.append("power-on")

    def summary(self) -> dict:
        return {
            "power": "On",
            "boot": {"override": "Once", "target": "Cd"},
            "nics": [{"interface": "NIC1", "mac": "02:00:00:00:00:01", "link": "LinkUp"}],
            "disks": [{"drive": "Disk0", "model": "ST600MM0009", "capacity": "558.4 GiB"}],
        }


@pytest.fixture
def fake_redfish(monkeypatch):
    """Route the commands' Redfish calls to FakeRedfish; the list collects the
    instances they created, in order."""
    made: list[FakeRedfish] = []

    def factory(bmc):
        rf = FakeRedfish(bmc)
        made.append(rf)
        return rf

    monkeypatch.setattr(commands.redfish, "Redfish", factory)
    return made


@pytest.fixture
def stub_factory(monkeypatch):
    monkeypatch.setattr(commands.factory, "schematic_id", lambda _ext: "abc123")


# -- shared lookups -----------------------------------------------------------


def test_find_server_and_cluster_ip(make_config):
    cfg = _cfg(make_config)
    server = commands._find_server(cfg, "rp001")
    assert server.group == "phoenix"
    assert commands._cluster_ip(server, cfg) == "172.29.21.5"


def test_find_server_rejects_unknown_names(make_config):
    cfg = _cfg(make_config)
    with pytest.raises(Exception, match="no metal server named 'nope'"):
        commands._find_server(cfg, "nope")


def test_cluster_ip_requires_a_static_address(make_config):
    metal = {
        **GROUP,
        "servers": {"rp001": {"interfaces": {"enp2s0f0": {}}}},
    }
    cfg = _cfg(make_config, metal)
    server = commands._find_server(cfg, "rp001")
    with pytest.raises(Exception, match="no static address"):
        commands._cluster_ip(server, cfg)


def test_bmc_skips_a_redfish_disabled_server(make_config, capsys):
    """`redfish: false` never constructs a client: None plus the notice."""
    metal = {**GROUP, "redfish": False}
    server = commands._find_server(_cfg(make_config, metal), "rp001")
    assert commands._bmc(server) is None
    assert "redfish disabled" in capsys.readouterr().out


def test_bmc_refuses_a_server_without_a_bmc_address(make_config):
    metal = {**GROUP, "servers": {"rp001": {}}}
    server = commands._find_server(_cfg(make_config, metal), "rp001")
    with pytest.raises(ReconcileError, match="no bmc.ip"):
        commands._bmc(server)


def test_iso_url_boots_the_base_extension_image(make_config, stub_factory):
    cfg = _cfg(make_config)
    assert commands._iso_url(cfg) == ISO_URL


def test_installer_image_is_the_metal_installer(make_config, stub_factory):
    cfg = _cfg(make_config)
    assert commands._installer_image(cfg) == (
        "factory.talos.dev/metal-installer/abc123:v1.13.9"
    )


# -- the commands ---------------------------------------------------------------


def test_inspect_prints_the_redfish_summary(make_config, tmp_path, fake_redfish, capsys):
    _cfg(make_config)
    commands.inspect(tmp_path, "rp001")
    out = capsys.readouterr().out
    assert "rp001" in out
    assert "power: On" in out
    assert "02:00:00:00:00:01" in out
    assert "ST600MM0009" in out


def test_inspect_skips_a_redfish_disabled_group(
    make_config, tmp_path, fake_redfish, capsys
):
    metal = {**GROUP, "redfish": False}
    _cfg(make_config, metal)
    commands.inspect(tmp_path, "rp001")
    assert fake_redfish == []
    assert "redfish disabled" in capsys.readouterr().out


def test_boot_mounts_one_time_boots_and_powers_on(
    make_config, tmp_path, fake_redfish, monkeypatch, stub_factory
):
    _cfg(make_config)
    monkeypatch.setattr(commands, "_iso_url", lambda cfg: ISO_URL)
    commands.boot(tmp_path, "rp001")
    # boot always clears the tray first, so a re-run is safe
    assert fake_redfish[-1].calls == [
        "eject", ("insert", ISO_URL), "boot-once", "power-on",
    ]


def test_boot_ejects_media_already_mounted_first(
    make_config, tmp_path, fake_redfish, monkeypatch, stub_factory
):
    _cfg(make_config)
    monkeypatch.setattr(commands, "_iso_url", lambda cfg: ISO_URL)
    monkeypatch.setattr(FakeRedfish, "pre_mounted", True)
    commands.boot(tmp_path, "rp001")
    assert fake_redfish[-1].calls == [
        "eject", ("insert", ISO_URL), "boot-once", "power-on",
    ]


def test_boot_serve_hands_the_iso_out_over_the_lan(
    make_config, tmp_path, fake_redfish, monkeypatch, stub_factory
):
    _cfg(make_config)
    monkeypatch.setattr(commands, "_iso_url", lambda cfg: ISO_URL)

    def fake_download(url, dest_dir):
        iso = dest_dir / "nocloud-amd64.iso"
        iso.write_bytes(b"ISO-DATA")
        return iso

    monkeypatch.setattr(commands, "_download_iso", fake_download)
    monkeypatch.setattr(commands, "_local_address_for", lambda _target: "127.0.0.1")

    commands.boot(tmp_path, "rp001", serve=True, foreground=False)

    mounted = fake_redfish[-1].calls[1][1]
    parsed = urllib.parse.urlparse(mounted)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.path == "/nocloud-amd64.iso"
    assert requests.get(mounted, timeout=5).content == b"ISO-DATA"
    # the boot override and power-on still happen
    assert fake_redfish[-1].calls[-2:] == ["boot-once", "power-on"]


def test_boot_serve_keeps_serving_until_interrupted(
    make_config, tmp_path, fake_redfish, monkeypatch, stub_factory
):
    _cfg(make_config)
    monkeypatch.setattr(commands, "_iso_url", lambda cfg: ISO_URL)

    def fake_download(url, dest_dir):
        iso = dest_dir / "nocloud-amd64.iso"
        iso.write_bytes(b"")
        return iso

    monkeypatch.setattr(commands, "_download_iso", fake_download)
    monkeypatch.setattr(commands, "_local_address_for", lambda _target: "127.0.0.1")

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(commands.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        commands.boot(tmp_path, "rp001", serve=True)


def test_boot_skips_a_server_that_turns_redfish_off(
    make_config, tmp_path, fake_redfish, capsys
):
    """A server may opt out of its group's redfish; the merged flag decides."""
    metal = {
        **GROUP,
        "servers": {"rp001": {"redfish": False, "bmc": {"ip": "198.51.100.10"}}},
    }
    _cfg(make_config, metal)
    commands.boot(tmp_path, "rp001")
    assert fake_redfish == []
    assert "redfish disabled" in capsys.readouterr().out


def test_wait_polls_the_maintenance_apid(make_config, tmp_path, monkeypatch):
    _cfg(make_config)
    answers = iter([False, False, True])
    monkeypatch.setattr(
        commands.talosctl, "maintenance_reachable", lambda ip: next(answers)
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    commands.wait(tmp_path, "rp001", timeout_s=60, interval_s=0)


def test_wait_times_out_when_nothing_answers(make_config, tmp_path, monkeypatch):
    _cfg(make_config)
    monkeypatch.setattr(commands.talosctl, "maintenance_reachable", lambda ip: False)
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError, match="maintenance apid"):
        commands.wait(tmp_path, "rp001", timeout_s=0, interval_s=0)


def test_apply_generates_and_pushes_the_config(
    make_config, tmp_path, monkeypatch, stub_factory
):
    _cfg(make_config)
    (tmp_path / "talossecrets.yaml").write_text("dummy")
    seen = {}

    def fake_build(server, cfg, secrets, installer, kubernetes_version=None):
        seen.update(
            installer=installer, role=server.role, kubernetes_version=kubernetes_version,
        )
        return f"# config for {server.name}\n"

    monkeypatch.setattr(commands.metal_talos, "build_config", fake_build)
    monkeypatch.setattr(
        commands.talosctl, "apply_config_insecure",
        lambda node, config: seen.update(node=node, pushed=config),
    )

    commands.apply(tmp_path, "rp001")

    path = tmp_path / ".metal" / "rp001-worker.yaml"
    assert path.read_text() == "# config for rp001\n"
    assert seen["node"] == "172.29.21.5"
    assert seen["pushed"] == "# config for rp001\n"
    assert seen["installer"] == "factory.talos.dev/metal-installer/abc123:v1.13.9"
    # no kubeconfig yet: the never-bootstrapped cluster gets the target
    assert seen["kubernetes_version"] == "v1.31.0"


def test_apply_bakes_the_running_version_of_a_bootstrapped_cluster(
    make_config, tmp_path, monkeypatch, stub_factory
):
    """A non-empty kubeconfig from an earlier converge means the cluster is
    running: the config is generated at the cluster's version, not cluster.yaml's
    raised target, so a joined machine never starts newer than the API server."""
    _cfg(make_config)
    (tmp_path / "talossecrets.yaml").write_text("dummy")
    (tmp_path / "kubeconfig").write_text("clusters: []\n")
    monkeypatch.setattr(kubectl, "server_version", lambda *_a: "v1.30.4")
    seen = {}

    def fake_build(server, cfg, secrets, installer, kubernetes_version=None):
        seen["kubernetes_version"] = kubernetes_version
        return "# config for rp001\n"

    monkeypatch.setattr(commands.metal_talos, "build_config", fake_build)
    monkeypatch.setattr(
        commands.talosctl, "apply_config_insecure", lambda node, config: None
    )

    commands.apply(tmp_path, "rp001")

    assert seen["kubernetes_version"] == "v1.30.4"


@pytest.mark.parametrize("on_disk", [None, ""])
def test_apply_bakes_the_target_before_the_cluster_is_bootstrapped(
    make_config, tmp_path, monkeypatch, stub_factory, on_disk
):
    """No kubeconfig (or an empty one) reads as never bootstrapped: there is no
    running version, so cluster.yaml's target is baked and the cluster is never
    asked for one."""
    _cfg(make_config)
    (tmp_path / "talossecrets.yaml").write_text("dummy")
    if on_disk is not None:
        (tmp_path / "kubeconfig").write_text(on_disk)
    monkeypatch.setattr(
        kubectl, "server_version",
        lambda *_a: pytest.fail("must not ask a cluster that never bootstrapped"),
    )
    seen = {}

    def fake_build(server, cfg, secrets, installer, kubernetes_version=None):
        seen["kubernetes_version"] = kubernetes_version
        return "# config for rp001\n"

    monkeypatch.setattr(commands.metal_talos, "build_config", fake_build)
    monkeypatch.setattr(
        commands.talosctl, "apply_config_insecure", lambda node, config: None
    )

    commands.apply(tmp_path, "rp001")

    assert seen["kubernetes_version"] == "v1.31.0"


def test_apply_refuses_to_guess_when_the_running_version_is_unreadable(
    make_config, tmp_path, monkeypatch, stub_factory
):
    """A kubeconfig whose cluster no longer answers aborts the apply instead of
    silently baking the target -- the same fail-closed stance as converge."""
    _cfg(make_config)
    (tmp_path / "talossecrets.yaml").write_text("dummy")
    (tmp_path / "kubeconfig").write_text("clusters: []\n")
    monkeypatch.setattr(kubectl, "server_version", lambda *_a: "")
    monkeypatch.setattr(converge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        commands.metal_talos, "build_config",
        lambda *_a, **_k: pytest.fail("no config must be generated"),
    )

    with pytest.raises(ReconcileError, match="could not determine the running"):
        commands.apply(tmp_path, "rp001")


def test_eject_reports_when_nothing_is_mounted(
    make_config, tmp_path, fake_redfish, capsys
):
    _cfg(make_config)
    commands.eject(tmp_path, "rp001")
    assert "no virtual media mounted" in capsys.readouterr().out


def test_eject_skips_a_redfish_disabled_group(
    make_config, tmp_path, fake_redfish, capsys
):
    metal = {**GROUP, "redfish": False}
    _cfg(make_config, metal)
    commands.eject(tmp_path, "rp001")
    assert fake_redfish == []
    assert "redfish disabled" in capsys.readouterr().out


def test_verify_waits_for_the_configured_node(
    make_config, tmp_path, monkeypatch, capsys
):
    _cfg(make_config)
    (tmp_path / "talosconfig").write_text("dummy")
    monkeypatch.setattr(commands.talosctl, "maintenance_reachable", lambda ip: False)
    monkeypatch.setattr(
        commands.talosctl, "reachable", lambda tc, endpoint, node: True
    )
    monkeypatch.setattr(
        commands.talosctl, "server_version", lambda tc, endpoint, node: "v1.13.9"
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    commands.verify(tmp_path, "rp001")
    assert "running v1.13.9" in capsys.readouterr().out


def test_verify_waits_while_the_node_still_runs_maintenance_mode(
    make_config, tmp_path, monkeypatch
):
    _cfg(make_config)
    (tmp_path / "talosconfig").write_text("dummy")
    # a node in maintenance mode answers insecurely even though the cluster
    # apid would accept the client too -- it must not read as configured
    monkeypatch.setattr(commands.talosctl, "maintenance_reachable", lambda ip: True)
    monkeypatch.setattr(
        commands.talosctl, "reachable", lambda tc, endpoint, node: True
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError, match="come back with its configuration"):
        commands.verify(tmp_path, "rp001", timeout_s=0, interval_s=0)


def test_verify_derives_a_client_config_from_the_secrets(
    make_config, tmp_path, monkeypatch
):
    _cfg(make_config)
    (tmp_path / "talossecrets.yaml").write_text("dummy")
    monkeypatch.setattr(commands.talosctl, "maintenance_reachable", lambda ip: False)
    monkeypatch.setattr(
        commands.talosctl, "reachable", lambda tc, endpoint, node: True
    )
    monkeypatch.setattr(
        commands.talosctl, "server_version", lambda tc, endpoint, node: "v1.13.9"
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _s: None)
    generated = {}
    monkeypatch.setattr(
        commands.talosctl, "gen_talosconfig",
        lambda cluster, endpoint, secrets, client_endpoint=None:
            generated.update(cluster=cluster, endpoint=endpoint) or "dummy",
    )
    commands.verify(tmp_path, "rp001")
    assert generated == {"cluster": "testcluster", "endpoint": "172.29.21.5"}


def test_join_runs_the_flow_in_order(make_config, tmp_path, monkeypatch):
    _cfg(make_config)
    order = []
    monkeypatch.setattr(
        commands, "boot",
        lambda root, name, *, serve=False, foreground=True: order.append(("boot", serve)),
    )
    for step in ("wait", "apply", "eject", "verify"):
        monkeypatch.setattr(
            commands, step, lambda root, name, step=step: order.append((step, None)),
        )
    commands.join(tmp_path, "rp001", serve=True)
    assert order == [("boot", True), ("wait", None), ("apply", None),
                     ("eject", None), ("verify", None)]


def test_join_without_redfish_is_wait_apply_verify(
    make_config, tmp_path, monkeypatch, fake_redfish, capsys
):
    """A redfish-off machine's BMC is never touched: no boot, no eject."""
    metal = {**GROUP, "redfish": False}
    _cfg(make_config, metal)
    order = []
    for step in ("wait", "apply", "verify"):
        monkeypatch.setattr(
            commands, step, lambda root, name, step=step: order.append(step)
        )
    commands.join(tmp_path, "rp001")
    assert order == ["wait", "apply", "verify"]
    assert fake_redfish == []
    assert "redfish disabled" in capsys.readouterr().out


def test_join_refuses_a_machine_that_is_already_configured(
    make_config, tmp_path, monkeypatch
):
    _cfg(make_config)
    (tmp_path / "talosconfig").write_text("dummy")
    monkeypatch.setattr(commands.talosctl, "maintenance_reachable", lambda ip: False)
    monkeypatch.setattr(
        commands.talosctl, "reachable", lambda tc, endpoint, node: True
    )
    with pytest.raises(ReconcileError, match="already answers apid"):
        commands.join(tmp_path, "rp001")


def test_join_without_a_talosconfig_has_nothing_to_refuse(
    make_config, tmp_path, monkeypatch
):
    _cfg(make_config)
    joined = []
    monkeypatch.setattr(commands, "boot", lambda root, name, **kw: joined.append(name))
    for step in ("wait", "apply", "eject", "verify"):
        monkeypatch.setattr(commands, step, lambda root, name: None)
    commands.join(tmp_path, "rp001")
    assert joined == ["rp001"]


# -- the Redfish client ----------------------------------------------------------


class StubResponse:
    def __init__(self, payload=None, status=200, text=""):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def close(self) -> None:
        pass


SYSTEM_PATH = "/redfish/v1/Systems/1"


def _routes(**extra):
    routes = {
        ("GET", "/redfish/v1/Systems"): StubResponse(
            {"Members": [{"@odata.id": SYSTEM_PATH}]}
        ),
        ("GET", SYSTEM_PATH): StubResponse(
            {
                "PowerState": "Off",
                "Boot": {
                    "BootSourceOverrideEnabled": "Disabled",
                    "BootSourceOverrideTarget": "None",
                    "BootSourceOverrideTarget@Redfish.AllowableValues":
                        ["None", "Pxe", "Cd", "Hdd"],
                },
                "Actions": {
                    "#ComputerSystem.Reset": {
                        "target": f"{SYSTEM_PATH}/Actions/ComputerSystem.Reset"
                    },
                },
                "EthernetInterfaces": {
                    "@odata.id": f"{SYSTEM_PATH}/EthernetInterfaces"
                },
                "Storage": {"@odata.id": f"{SYSTEM_PATH}/Storage"},
                "VirtualMedia": {"@odata.id": f"{SYSTEM_PATH}/VirtualMedia"},
            }
        ),
        ("PATCH", SYSTEM_PATH): StubResponse(),
        ("PATCH", f"{SYSTEM_PATH}/VirtualMedia/CD"): StubResponse(),
        ("POST", f"{SYSTEM_PATH}/Actions/ComputerSystem.Reset"): StubResponse(),
        ("POST", f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia"): StubResponse(),
        ("POST", f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.EjectMedia"): StubResponse(),
        ("GET", f"{SYSTEM_PATH}/EthernetInterfaces"): StubResponse(
            {"Members": [{"@odata.id": f"{SYSTEM_PATH}/EthernetInterfaces/NIC1"}]}
        ),
        ("GET", f"{SYSTEM_PATH}/EthernetInterfaces/NIC1"): StubResponse(
            {"Id": "NIC1", "MACAddress": "02:00:00:00:00:01", "LinkStatus": "LinkUp"}
        ),
        ("GET", f"{SYSTEM_PATH}/Storage"): StubResponse(
            {"Members": [{"@odata.id": f"{SYSTEM_PATH}/Storage/RAID"}]}
        ),
        ("GET", f"{SYSTEM_PATH}/Storage/RAID"): StubResponse(
            {"Drives": [{"@odata.id": f"{SYSTEM_PATH}/Storage/RAID/Drives/Disk0"}]}
        ),
        ("GET", f"{SYSTEM_PATH}/Storage/RAID/Drives/Disk0"): StubResponse(
            {"Id": "Disk0", "Model": "ST600MM0009", "CapacityBytes": 599550590976}
        ),
        ("GET", f"{SYSTEM_PATH}/VirtualMedia"): StubResponse(
            {"Members": [{"@odata.id": f"{SYSTEM_PATH}/VirtualMedia/CD"}]}
        ),
        ("GET", f"{SYSTEM_PATH}/VirtualMedia/CD"): StubResponse(
            {
                "Id": "CD",
                "Name": "Virtual CD",
                "Inserted": False,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {
                        "target":
                            f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia",
                    },
                    "#VirtualMedia.EjectMedia": {
                        "target":
                            f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.EjectMedia",
                    },
                },
            }
        ),
    }
    routes.update(extra)
    return routes


@pytest.fixture
def client(monkeypatch):
    """A Redfish client whose HTTP layer answers from the route table above.

    Returns (client, recorded requests, routes); a test reshapes the BMC by
    editing `routes` before the call it is exercising.
    """
    seen: list = []
    routes = _routes()
    rf = redfish.Redfish(MetalBmc(ip="198.51.100.10", username="u", password="p"))
    rf._base = "https://198.51.100.10"

    def fake_request(method, path, body=None):
        seen.append((method, path, body))
        return routes.get((method, path.rstrip("/")), StubResponse(status=404, text="nope"))

    monkeypatch.setattr(rf, "_request", fake_request)
    return rf, seen, routes


def test_summary_reports_power_boot_nics_and_disks(client):
    rf, _, _ = client
    summary = rf.summary()
    assert summary["power"] == "Off"
    assert summary["boot"] == {"override": "Disabled", "target": "None"}
    assert summary["nics"] == [
        {"interface": "NIC1", "mac": "02:00:00:00:00:01", "link": "LinkUp"}
    ]
    # 599550590976 bytes, human-readable
    assert summary["disks"] == [
        {"drive": "Disk0", "model": "ST600MM0009", "capacity": "558.4 GiB"}
    ]


def test_boot_once_cd_patches_a_one_time_override(client):
    rf, seen, _ = client
    rf.boot_once_cd()
    method, path, body = seen[-1]
    assert (method, path) == ("PATCH", SYSTEM_PATH)
    assert body == {
        "Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "Cd"}
    }


def test_boot_once_cd_refuses_when_the_controller_cannot(client):
    rf, _, routes = client
    routes[("GET", SYSTEM_PATH)] = StubResponse(
        {
            "Boot": {
                "BootSourceOverrideTarget@Redfish.AllowableValues": ["Pxe", "Hdd"],
            }
        }
    )
    with pytest.raises(redfish.RedfishError, match="one-time CD boot"):
        rf.boot_once_cd()


def test_insert_media_uses_the_insert_action(client):
    rf, seen, _ = client
    rf.insert_media(ISO_URL)
    method, path, body = seen[-1]
    assert (method, path) == (
        "POST", f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia",
    )
    assert body == {"Image": ISO_URL, "Inserted": True, "WriteProtected": True}


def test_insert_media_falls_back_to_a_patch_without_an_action(client):
    rf, seen, routes = client
    device = routes[("GET", f"{SYSTEM_PATH}/VirtualMedia/CD")].json()
    del device["Actions"]
    routes[("GET", f"{SYSTEM_PATH}/VirtualMedia/CD")] = StubResponse(device)
    rf.insert_media(ISO_URL)
    method, path, body = seen[-1]
    assert (method, path) == ("PATCH", f"{SYSTEM_PATH}/VirtualMedia/CD")
    assert body == {"Image": ISO_URL, "Inserted": True, "WriteProtected": True}


def test_insert_media_prefers_the_cd_device(client):
    rf, seen, routes = client
    collection = routes[("GET", f"{SYSTEM_PATH}/VirtualMedia")].json()
    collection["Members"].append({"@odata.id": f"{SYSTEM_PATH}/VirtualMedia/USB"})
    routes[("GET", f"{SYSTEM_PATH}/VirtualMedia")] = StubResponse(collection)
    routes[("GET", f"{SYSTEM_PATH}/VirtualMedia/USB")] = StubResponse(
        {"Id": "USB", "Name": "Virtual USB", "Inserted": False}
    )
    rf.insert_media(ISO_URL)
    assert seen[-1][1] == f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.InsertMedia"


def test_insert_media_refuses_when_nothing_is_mountable(client):
    rf, _, routes = client
    routes[("GET", f"{SYSTEM_PATH}/VirtualMedia")] = StubResponse({"Members": []})
    with pytest.raises(redfish.RedfishError, match="no virtual media"):
        rf.insert_media(ISO_URL)


def test_eject_media_ejects_the_inserted_device(client):
    rf, seen, routes = client
    assert rf.eject_media() is False  # nothing inserted
    device = routes[("GET", f"{SYSTEM_PATH}/VirtualMedia/CD")].json()
    device["Inserted"] = True
    rf.eject_media()
    assert seen[-1][:2] == (
        "POST", f"{SYSTEM_PATH}/VirtualMedia/CD/Actions/VirtualMedia.EjectMedia",
    )


def test_power_on_powers_on_when_off_and_restarts_when_running(client):
    rf, seen, _ = client
    rf.power_on()  # the system doc above reports Off
    assert seen[-1][1:] == (f"{SYSTEM_PATH}/Actions/ComputerSystem.Reset",
                            {"ResetType": "On"})


def test_power_on_forces_a_restart_when_already_running(client):
    rf, seen, routes = client
    routes[("GET", SYSTEM_PATH)] = StubResponse(
        {
            "PowerState": "On",
            "Actions": {
                "#ComputerSystem.Reset": {
                    "target": f"{SYSTEM_PATH}/Actions/ComputerSystem.Reset"
                }
            },
        }
    )
    rf.power_on()
    assert seen[-1][2] == {"ResetType": "ForceRestart"}


def test_power_on_needs_a_reset_action(client):
    rf, _, routes = client
    routes[("GET", SYSTEM_PATH)] = StubResponse({"PowerState": "Off"})
    with pytest.raises(redfish.RedfishError, match="no power-control action"):
        rf.power_on()


def test_failed_requests_raise_redfish_errors(client):
    rf, _, routes = client
    del routes[("GET", SYSTEM_PATH)]
    with pytest.raises(redfish.RedfishError, match="HTTP 404"):
        rf.power_state()


def test_base_falls_back_to_http_when_https_is_refused(monkeypatch):
    rf = redfish.Redfish(MetalBmc(ip="198.51.100.10", username="u", password="p"))

    def fake_get(url, **kw):
        if url.startswith("https://"):
            raise requests.exceptions.ConnectionError("refused")
        return StubResponse({"v1": "/redfish/v1/"})

    monkeypatch.setattr(requests, "get", fake_get)
    assert rf.base == "http://198.51.100.10"
