"""The generated machine configs must validate on every supported Talos minor.

``talosctl gen config --talos-version v1.14.0`` moves most of the v1alpha1
machine document into typed documents (``UnattendedInstallConfig``,
``KubeNodeConfig``, ``KubeletConfig``, ...) and rejects patches that set the
v1alpha1 fields those documents now own, so a config generated in the wrong
layout fails ``talosctl validate --strict`` on one minor while passing on the
other. The patch builders pick the layout from the version each config is
generated for, so this runs the REAL ``talosctl gen config`` through the real
shared and metal patch stacks -- a VM control plane, a VM worker and a metal
node on each supported minor -- and validates every result with
``talosctl validate --mode metal --strict``. Skipped where no talosctl is
installed; this is the only check that exercises the real schema.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from taloscluster.infrastructure import Endpoint
from taloscluster.metal import talos as metal_talos
from taloscluster.openstack import talos as openstack_talos
from taloscluster.talos import machineconfig

TALOSCTL = shutil.which("talosctl")

VIP = "192.168.0.10"
FIP = "203.0.113.10"
INSTALLER = "factory.talos.dev/openstack-installer/abc123:{version}"

# (talos.version, kubernetes.version) for every supported Talos minor, the
# pairing the loader's support matrix enforces
MINORS = [
    ("v1.13.9", "v1.31.0"),
    ("v1.14.0", "v1.33.0"),
]

METAL = {
    "role": "worker",
    "disk": "/dev/sda",
    "network": {"cidr": "192.168.0.0/21", "gateway": "192.168.0.1"},
    "interfaces": {"enp1s0f0": {"role": "cluster"}},
    "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "192.168.0.5/21"}}}},
}


def _secrets(talos_version: str, tmp_path) -> object:
    """A real secrets bundle for the version, via the project's own wrapper."""
    path = tmp_path / "talossecrets.yaml"
    path.write_text(machineconfig.talosctl.gen_secrets(talos_version))
    return path


def _validate(config_yaml: str, tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(config_yaml)
    result = subprocess.run(
        [TALOSCTL, "validate", "--mode", "metal", "--strict", "-c", str(path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(TALOSCTL is None, reason="talosctl not installed")
@pytest.mark.parametrize(("talos_version", "kubernetes_version"), MINORS)
def test_vm_configs_validate_on_every_supported_minor(
    make_config, tmp_path, talos_version, kubernetes_version
):
    """The full VM patch stack -- shared patches plus the OpenStack provider
    contribution, generated through real `talosctl gen config` -- validates
    strict on the minor it names, control plane and worker alike."""
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "talos": {"version": talos_version},
        "kubernetes": {"version": kubernetes_version},
    })
    endpoint = Endpoint(vip=VIP, advertised_address=FIP)
    installer = INSTALLER.format(version=talos_version)
    configs = machineconfig.build_configs(
        cfg,
        cfg.machines,
        endpoint,
        _secrets(talos_version, tmp_path),
        {ext: installer for ext in cfg.extension_sets()},
        {host: openstack_talos.contribution(m, cfg, endpoint)
         for host, m in cfg.machines.items()},
    )

    assert set(configs) == set(cfg.machines)
    for config_yaml in configs.values():
        _validate(config_yaml, tmp_path)


@pytest.mark.skipif(TALOSCTL is None, reason="talosctl not installed")
@pytest.mark.parametrize(("talos_version", "kubernetes_version"), MINORS)
def test_metal_config_validates_on_every_supported_minor(
    make_config, tmp_path, talos_version, kubernetes_version
):
    """The metal stack -- shared patches plus the cabling plan, including the
    ResolverConfig the static DNS rides -- validates strict on the minor its
    install media is built for."""
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "talos": {"version": talos_version},
        "kubernetes": {"version": kubernetes_version},
        "metal": {"site": METAL},
    }, remove=("workers",))
    endpoint = Endpoint(vip="", advertised_address=FIP)
    config_yaml = metal_talos.build_config(
        cfg.metal.groups["site"].servers["rp001"],
        cfg,
        _secrets(talos_version, tmp_path),
        INSTALLER.format(version=talos_version),
        endpoint,
    )

    _validate(config_yaml, tmp_path)
