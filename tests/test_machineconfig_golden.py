"""Golden test: the OpenStack patch stack must survive the provider-neutral refactor.

Stage 4 moved the install disk and the legacy ``eth0`` DHCP/VIP block out of the
shared generator and into the OpenStack backend's contribution. The rendered
machine configuration must not change, so this pins every document handed to
``talosctl gen config`` for a control plane and a worker. The eth0 block now
arrives as its own patch document instead of living inside the machine patch;
the keys are disjoint, so the strategic merge result is identical.

Update the golden only when a machine-config change is intended.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster.config import Secrets
from taloscluster.infrastructure import Endpoint
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
        "extraManifests": machineconfig.EXTRA_MANIFESTS,
        "apiServer": {"certSANs": [FIP]},
        "etcd": {"advertisedSubnets": ["192.168.0.0/21"]},
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


GOLDEN = {
    "testcluster-controlplane-01": [
        [_machine_patch("controlplane", "controlplane")],
        [_named(HOSTNAME_PATCH, "testcluster-controlplane-01")],
        [CLUSTER_PATCH],
        [_named(TAILSCALE_PATCH, "testcluster-controlplane-01")],
        [{"machine": {"network": {"interfaces": [
            {"interface": "eth0", "dhcp": True, "vip": {"ip": VIP}},
        ]}}}],
    ],
    "testcluster-worker-01": [
        [_machine_patch("worker", "worker")],
        [_named(HOSTNAME_PATCH, "testcluster-worker-01")],
        [_named(TAILSCALE_PATCH, "testcluster-worker-01")],
        [{"machine": {"network": {"interfaces": []}}}],
    ],
}


@pytest.fixture
def cfg(make_config):
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "tailscale": {"login_server": "https://headscale.example.com"},
    })


def test_openstack_patch_stack_matches_golden(cfg, monkeypatch, tmp_path):
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
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg,
        Secrets(
            openstack_credential_id="id",
            openstack_credential_secret="secret",
            tailscale_auth_key="tskey-secret",
        ),
        cfg.machines,
        endpoint=endpoint,
        secrets_path=secrets_path,
        installer_images={ext: INSTALLER for ext in cfg.extension_sets()},
        contributions={
            host: talos.contribution(m, cfg, endpoint) for host, m in cfg.machines.items()
        },
    )

    assert rendered == GOLDEN
