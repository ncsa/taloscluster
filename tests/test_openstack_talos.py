"""Tests for the OpenStack Talos contribution.

OpenStack contributes the virtio install disk, the network documents that keep
DHCP on eth0 and carry the Layer 2 API VIP there, and -- on control planes --
the bootstrap NetworkPolicy that denies pods the Nova metadata service, which
serves the machine config delivered as user_data for the instance's lifetime.
The policy patch is also run through a real `talosctl gen config` when the
binary is on PATH, the only check that validates the patch's Talos schema
shape.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from taloscluster.infrastructure import Endpoint
from taloscluster.openstack import talos

TALOSCTL = shutil.which("talosctl")

VIP = "192.168.0.10"
FIP = "203.0.113.10"
ETH0_DHCP = [
    {"apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "eth0"},
    {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": "eth0"},
]
METADATA_POLICY = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "block-cloud-metadata", "namespace": "default"},
    "spec": {
        "podSelector": {},
        "policyTypes": ["Egress"],
        "egress": [
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}}]}
        ],
    },
}


@pytest.fixture
def ep() -> Endpoint:
    return Endpoint(vip=VIP, advertised_address=FIP)


@pytest.fixture
def cfg(make_config):
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
    })


def test_contribution_uses_virtio_install_disk(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    assert talos.contribution(m, cfg, ep).install_disk == "/dev/vda"


def test_controlplane_gets_the_vip_on_eth0(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    contribution = talos.contribution(m, cfg, ep)

    assert [p.name for p in contribution.patches] == ["network", "metadata-policy"]
    assert contribution.patches[0].document == ETH0_DHCP + [
        {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": VIP, "link": "eth0"},
    ]


def test_worker_keeps_dhcp_without_a_vip(cfg, ep):
    m = cfg.machines["testcluster-worker-01"]
    contribution = talos.contribution(m, cfg, ep)
    assert [p.name for p in contribution.patches] == ["network"]
    assert contribution.patches[0].document == ETH0_DHCP


def test_controlplane_embeds_the_metadata_policy_in_the_cluster_config(cfg, ep):
    """The policy rides cluster.inlineManifests, which Talos applies with the
    bootstrap manifests; a patch document is the delivery vehicle. The
    manifests field is a list of {name, contents} (Talos's
    []v1alpha1.ClusterInlineManifest), not a name->manifest mapping."""
    m = cfg.machines["testcluster-controlplane-01"]
    patch = talos.contribution(m, cfg, ep).patches[-1]

    assert patch.name == "metadata-policy"
    assert patch.document == {
        "cluster": {
            "inlineManifests": [
                {
                    "name": "block-cloud-metadata",
                    "contents": yaml.safe_dump(METADATA_POLICY, sort_keys=False),
                }
            ]
        }
    }


def test_metadata_policy_blocks_only_the_metadata_address(cfg, ep):
    """The manifest is exact: all pods in the default namespace may egress
    anywhere except the Nova metadata service, and nothing else is touched."""
    m = cfg.machines["testcluster-controlplane-01"]
    patch = talos.contribution(m, cfg, ep).patches[-1]
    (entry,) = patch.document["cluster"]["inlineManifests"]

    assert entry["name"] == "block-cloud-metadata"
    assert yaml.safe_load(entry["contents"]) == METADATA_POLICY


@pytest.mark.skipif(TALOSCTL is None, reason="talosctl not installed")
def test_metadata_policy_patch_passes_real_talosctl_gen_config(tmp_path):
    """The patch must survive real talosctl schema validation. The suite's
    gen_config fake records patch documents without validating them, and
    `talosctl gen config` rejects a map-shaped inlineManifests (it wants
    []v1alpha1.ClusterInlineManifest), so run the real binary when present."""
    patch = tmp_path / "metadata-policy.yaml"
    patch.write_text(yaml.safe_dump(talos.metadata_policy_patch().document))
    secrets = tmp_path / "secrets.yaml"
    subprocess.run(
        [TALOSCTL, "gen", "secrets", "--talos-version", "v1.13.9", "-o", str(secrets)],
        check=True, capture_output=True,
    )
    result = subprocess.run(
        [
            TALOSCTL, "gen", "config", "testcluster", "https://192.168.0.10:6443",
            "--with-secrets", str(secrets),
            "--output-types", "controlplane", "--output", "-",
            "--install-image", "factory.talos.dev/openstack-installer/abc123:v1.8.3",
            "--install-disk", "/dev/vda",
            "--kubernetes-version", "1.31.0",
            "--talos-version", "v1.13.9",
            "--with-docs=false", "--with-examples=false",
            "--config-patch", f"@{patch}",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    # gen config emits the v1alpha1 Config plus its other config documents
    (config,) = (d for d in yaml.safe_load_all(result.stdout) if "cluster" in d)
    (manifest,) = config["cluster"]["inlineManifests"]
    assert manifest["name"] == "block-cloud-metadata"
    assert yaml.safe_load(manifest["contents"]) == METADATA_POLICY


def test_installer_platform_is_openstack():
    assert talos.INSTALLER_PLATFORM == "openstack"
