"""Golden test: the OpenStack patch stack must survive the provider-neutral refactor.

Stage 4 moved the install disk and the legacy ``eth0`` DHCP/VIP block out of the
shared generator and into the OpenStack backend's contribution. The rendered
machine configuration must not change, so this pins every document handed to
``talosctl gen config`` for a control plane and a worker. The eth0 block now
arrives as its own patch document instead of living inside the machine patch;
the keys are disjoint, so the strategic merge result is identical. The stack is
pinned for the default MTU and for a jumbo ``network.cluster.mtu``, which states
the MTU on the eth0 LinkConfig and restates the eth0 default route with an MTU
of 1500. A second golden pins the shared stack around the KubeSpan patch:
opting in with ``talos.kubespan: true`` puts it on every node -- the WireGuard
MTU (the L2 MTU minus overhead) and, when ``network.external`` exists, its
networks excluded from endpoint discovery -- while the default (and
``talos.kubespan: false``) emits none. Every node also carries the system disk
encryption patch (STATE and EPHEMERAL as LUKS2, keyed with the passphrase the
cluster's talossecrets.yaml holds), and control planes end the stack with the
metadata-policy patch and the vendored bootstrap manifests, whose embedded
content is pinned by tests/test_machineconfig.py and
tests/test_openstack_talos.py.

Update the golden only when a machine-config change is intended.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster.infrastructure import Endpoint, TalosContribution
from taloscluster.openstack import talos
from taloscluster.talos import machineconfig

INSTALLER = "factory.talos.dev/openstack-installer/abc123:v1.8.3"
VIP = "192.168.0.10"
FIP = "203.0.113.10"

MACHINE_PATCH = {
    "machine": {
        "certSANs": [FIP],
        "nodeLabels": {"ncsa/role": "ROLE", "ncsa/pool": "POOL"},
        "kubelet": {
            "extraArgs": {"rotate-server-certificates": True},
            "nodeIP": {"validSubnets": ["192.168.0.0/21"]},
        },
        "install": {"disk": "/dev/vda", "image": INSTALLER, "wipe": True},
        "time": {"servers": ["ntp.example.com"]},
    }
}
HOSTNAME_PATCH = {
    "apiVersion": "v1alpha1",
    "kind": "HostnameConfig",
    "auto": {"$patch": "delete"},
    "hostname": "@HOST@",
}
CLUSTER_PATCH = {
    "cluster": {
        "allowSchedulingOnControlPlanes": False,
        "inlineManifests": [
            {"name": "kubelet-serving-cert-approver",
             "contents": machineconfig.CERT_APPROVER_MANIFEST},
            {"name": "metrics-server",
             "contents": machineconfig.METRICS_SERVER_MANIFEST},
        ],
        "apiServer": {"certSANs": [FIP]},
        "etcd": {"advertisedSubnets": ["192.168.0.0/21"]},
    }
}
DISK_PASSPHRASE = "luks-passphrase-0123"
ENCRYPTION_PATCH = {
    "machine": {
        "systemDiskEncryption": {
            "state": {
                "provider": "luks2",
                "keys": [{"slot": 0, "static": {"passphrase": DISK_PASSPHRASE}}],
            },
            "ephemeral": {
                "provider": "luks2",
                "keys": [{"slot": 0, "static": {"passphrase": DISK_PASSPHRASE}}],
            },
        }
    }
}
TAILSCALE_PATCH = {
    "apiVersion": "v1alpha1",
    "kind": "ExtensionServiceConfig",
    "name": "tailscale",
    "environment": [
        "TS_AUTHKEY=tskey-secret",
        "TS_HOSTNAME=@HOST@",
        "TS_EXTRA_ARGS=--login-server=https://headscale.example.com",
    ],
}


def _machine_patch(role: str, pool: str) -> dict:
    patch = yaml.safe_load(yaml.safe_dump(MACHINE_PATCH))
    patch["machine"]["nodeLabels"] = {"ncsa/role": role, "ncsa/pool": pool}
    return patch


def _named(patch: dict, host: str) -> dict:
    return yaml.safe_load(yaml.safe_dump(patch).replace("@HOST@", host))


def _eth0_docs(cluster_mtu: int | None) -> list[dict]:
    """The eth0 documents; a jumbo L2 states the link MTU and restates the
    default route with a 1500 MTU (the route the DHCP lease provides)."""
    link: dict = {"apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "eth0"}
    if cluster_mtu is not None:
        link["mtu"] = cluster_mtu
        link["routes"] = [{"gateway": "192.168.0.1", "mtu": 1500}]
    return [link, {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": "eth0"}]


def _golden(cluster_mtu: int | None) -> dict[str, list]:
    eth0 = _eth0_docs(cluster_mtu)
    cp_eth0 = eth0 + [
        {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": VIP, "link": "eth0"}
    ]
    return {
        "testcluster-controlplane-01": [
            [_machine_patch("controlplane", "controlplane")],
            [_named(HOSTNAME_PATCH, "testcluster-controlplane-01")],
            [ENCRYPTION_PATCH],
            [CLUSTER_PATCH],
            "FIREWALL",
            [_named(TAILSCALE_PATCH, "testcluster-controlplane-01")],
            cp_eth0,
            [talos.metadata_policy_patch().document],
        ],
        "testcluster-worker-01": [
            [_machine_patch("worker", "worker")],
            [_named(HOSTNAME_PATCH, "testcluster-worker-01")],
            [ENCRYPTION_PATCH],
            "FIREWALL",
            [_named(TAILSCALE_PATCH, "testcluster-worker-01")],
            eth0,
        ],
    }


def _config(make_config, cluster_mtu: int | None):
    overrides: dict = {
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "tailscale": {
            "login_server": "https://headscale.example.com",
            "auth_key": "tskey-secret",
        },
    }
    if cluster_mtu is not None:
        overrides["network"] = {"cluster": {"mtu": cluster_mtu}}
    return make_config(overrides)


@pytest.mark.parametrize("cluster_mtu", [None, 9000], ids=["default-mtu", "mtu-9000"])
def test_openstack_patch_stack_matches_golden(make_config, monkeypatch, tmp_path, cluster_mtu):
    cfg = _config(make_config, cluster_mtu)
    endpoint = Endpoint(vip=VIP, advertised_address=FIP)
    rendered: dict[str, list[list[dict]]] = {}

    def fake_gen_config(**kwargs):
        host = Path(kwargs["patches"][0]).name.removesuffix("-machine.yaml")
        rendered[host] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        assert kwargs["install_disk"] == "/dev/vda"
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text(
        f"cluster:\n  id: abc\n"
        f"{machineconfig.DISK_PASSPHRASE_KEY}: {DISK_PASSPHRASE}\n"
    )

    machineconfig.build_configs(
        cfg,
        cfg.machines,
        endpoint=endpoint,
        secrets_path=secrets_path,
        installer_images={ext: INSTALLER for ext in cfg.extension_sets()},
        contributions={
            host: talos.contribution(m, cfg, endpoint) for host, m in cfg.machines.items()
        },
    )

    # the firewall stack is derived from the same security rules on every node;
    # its content is covered by tests/test_talos_firewall.py
    firewall = machineconfig._firewall_docs(cfg)
    expected = {
        host: [firewall if patch == "FIREWALL" else patch for patch in stack]
        for host, stack in _golden(cluster_mtu).items()
    }
    assert rendered == expected


def _kubespan_cfg(make_config, kubespan: bool):
    """A Proxmox cluster on a jumbo L2 with a routed external network -- the
    shape where the KubeSpan endpoint filters apply. OpenStack cannot carry
    `network.external`, so this golden runs on the Proxmox loader shape with
    the provider contribution left empty (its documents are pinned by
    tests/test_proxmox_talos.py)."""
    overrides: dict = {
        "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
        "workers": {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 50}},
        "tailscale": {
            "login_server": "https://headscale.example.com",
            "auth_key": "tskey-secret",
        },
        "network": {
            "cluster": {"mtu": 9000},
            "external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "anchor_cidr": "169.254.40.0/24",
                "kubeapi_vip": "203.0.113.10",
            },
        },
        "proxmox": {
            "url": "https://pve.example:8006",
            "storage": "vms",
            "iso_storage": "isos",
            "network": {
                "cluster": {"bridge": "vmbr0"},
                "external": {"bridge": "vmbr1"},
            },
        },
    }
    overrides["talos"] = {"kubespan": kubespan}
    return make_config(overrides, remove=("openstack",))


def _kubespan_golden(kubespan_enabled: bool) -> dict[str, list]:
    kubespan: list = []
    if kubespan_enabled:
        kubespan = [[{
            "machine": {"network": {"kubespan": {
                "enabled": True,
                "mtu": 8920,
                "filters": {"endpoints": [
                    "0.0.0.0/0", "!203.0.113.0/24", "!169.254.40.0/24",
                ]},
            }}}
        }]]
    return {
        "testcluster-controlplane-01": [
            [_machine_patch("controlplane", "controlplane")],
            [_named(HOSTNAME_PATCH, "testcluster-controlplane-01")],
            [ENCRYPTION_PATCH],
            [CLUSTER_PATCH],
            "FIREWALL",
            *kubespan,
            [_named(TAILSCALE_PATCH, "testcluster-controlplane-01")],
        ],
        "testcluster-worker-01": [
            [_machine_patch("worker", "worker")],
            [_named(HOSTNAME_PATCH, "testcluster-worker-01")],
            [ENCRYPTION_PATCH],
            "FIREWALL",
            *kubespan,
            [_named(TAILSCALE_PATCH, "testcluster-worker-01")],
        ],
    }


@pytest.mark.parametrize("kubespan", [True, False], ids=["kubespan-on", "kubespan-off"])
def test_patch_stack_kubespan_matches_golden(make_config, monkeypatch, tmp_path, kubespan):
    cfg = _kubespan_cfg(make_config, kubespan)
    endpoint = Endpoint(vip=VIP, advertised_address=FIP)
    rendered: dict[str, list[list[dict]]] = {}

    def fake_gen_config(**kwargs):
        host = Path(kwargs["patches"][0]).name.removesuffix("-machine.yaml")
        rendered[host] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text(
        f"cluster:\n  id: abc\n"
        f"{machineconfig.DISK_PASSPHRASE_KEY}: {DISK_PASSPHRASE}\n"
    )

    machineconfig.build_configs(
        cfg,
        cfg.machines,
        endpoint=endpoint,
        secrets_path=secrets_path,
        installer_images={ext: INSTALLER for ext in cfg.extension_sets()},
        contributions={
            host: TalosContribution(install_disk="/dev/vda") for host in cfg.machines
        },
    )

    # the firewall stack is derived from the same security rules on every node;
    # its content is covered by tests/test_talos_firewall.py
    firewall = machineconfig._firewall_docs(cfg)
    expected = {
        host: [firewall if patch == "FIREWALL" else patch for patch in stack]
        for host, stack in _kubespan_golden(kubespan).items()
    }
    assert rendered == expected
