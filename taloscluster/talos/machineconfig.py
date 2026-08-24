"""Build each node's Talos machine config, the port of talos.tf:51-158.

The four yamlencode patches become Python dicts dumped to YAML files and stacked
as `--config-patch` on `talosctl gen config`, in the same order terraform used:
  machine -> hostname -> (cluster, controlplane only) -> tailscale -> freeform.

Kept as separate patch files on purpose: hostname (HostnameConfig) and tailscale
(ExtensionServiceConfig) are their own machine-config documents, and the
hostname patch relies on the `$patch: delete` directive to drop the `auto` field
-- both only behave correctly as standalone patch docs, never dump_all'd into
one stream.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import Config, Machine, Secrets
from . import talosctl

INSTALL_DISK = "/dev/vda"
# Pinned deliberately. Both of these were previously moving targets -- the
# cert-approver at branch `main` (image tag `main`) and metrics-server at
# `releases/latest` -- so an upstream push could break a cluster nobody touched.
# The cert-approver did exactly that: it crashlooped on exit code 2, and because
# talosctl upgrade-k8s waits for every bootstrap manifest to reconcile, a broken
# add-on blocks kubernetes upgrades entirely. Bump these on purpose, not by drift.
CERT_APPROVER_VERSION = "v0.11.0"
METRICS_SERVER_VERSION = "v0.9.0"
EXTRA_MANIFESTS = [
    f"https://raw.githubusercontent.com/alex1989hu/kubelet-serving-cert-approver/{CERT_APPROVER_VERSION}/deploy/standalone-install.yaml",
    f"https://github.com/kubernetes-sigs/metrics-server/releases/download/{METRICS_SERVER_VERSION}/components.yaml",
]


@dataclass
class Endpoints:
    kubeapi_fip: str   # public floating ip -> certSANs + cluster endpoint
    kubeapi_vip: str   # private fixed ip announced by controlplanes


def _label_value(value: str) -> str:
    """Make a value safe as a kubernetes label value: spaces become `_`
    (an OpenStack project name may contain spaces, a label value may not)."""
    return str(value).replace(" ", "_")


def _node_labels(m: Machine, default_tags: dict[str, str] | None) -> dict[str, str]:
    """role/pool first, then defaults (project name), then cluster.yaml tags —
    later wins, so a user tag can override a default."""
    labels = {"ncsa/role": m.role, "ncsa/pool": m.pool}
    labels.update(default_tags or {})
    labels.update(m.tags)
    return {k: _label_value(v) for k, v in labels.items()}


def _machine_patch(m: Machine, cfg: Config, ep: Endpoints, installer_image: str,
                   default_tags: dict[str, str] | None = None) -> dict:
    interfaces = (
        [{"interface": "eth0", "dhcp": True, "vip": {"ip": ep.kubeapi_vip}}]
        if m.role == "controlplane"
        else []
    )
    return {
        "machine": {
            "network": {"interfaces": interfaces},
            "certSANs": [ep.kubeapi_fip],
            "nodeLabels": _node_labels(m, default_tags),
            "kubelet": {
                "extraArgs": {"rotate-server-certificates": True},
                # pin node ip to the private net so pod traffic never rides tailscale
                "nodeIP": {"validSubnets": [cfg.cidr]},
            },
            "install": {"disk": INSTALL_DISK, "image": installer_image, "wipe": True},
            "time": {"servers": cfg.ntp},
        }
    }


def _hostname_patch(m: Machine) -> dict:
    # force the instance name as hostname; strategic merge can't delete a field,
    # so drop `auto` with the $patch: delete directive
    return {
        "apiVersion": "v1alpha1",
        "kind": "HostnameConfig",
        "auto": {"$patch": "delete"},
        "hostname": m.name,
    }


def _cluster_patch(cfg: Config, ep: Endpoints) -> dict:
    return {
        "cluster": {
            "allowSchedulingOnControlPlanes": False,
            "extraManifests": EXTRA_MANIFESTS,
            "apiServer": {"certSANs": [ep.kubeapi_fip]},
            # keep etcd peering on the private network, off tailscale
            "etcd": {"advertisedSubnets": [cfg.cidr]},
        }
    }


def _tailscale_patch(m: Machine, cfg: Config, auth_key: str) -> dict:
    return {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": [
            f"TS_AUTHKEY={auth_key}",
            f"TS_HOSTNAME={m.name}",
            f"TS_EXTRA_ARGS=--login-server={cfg.login_server}",
        ],
    }


def _write(workdir: Path, stem: str, doc) -> Path:
    """Dump a patch dict (or raw YAML string) to its own file."""
    path = workdir / f"{stem}.yaml"
    if isinstance(doc, str):
        path.write_text(doc)
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return path


def build_configs(
    cfg: Config,
    secrets: Secrets,
    machines: dict[str, Machine],
    ep: Endpoints,
    secrets_path: Path,
    installer_images: dict[tuple[str, ...], str],
    default_tags: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {hostname -> machine-config YAML string} for every machine."""
    endpoint = f"https://{ep.kubeapi_fip}:6443"
    configs: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="taloscluster-mc-") as tmp:
        workdir = Path(tmp)
        for host, m in machines.items():
            installer_image = installer_images[m.extensions]
            patches: list[Path] = [
                _write(workdir, f"{host}-machine",
                       _machine_patch(m, cfg, ep, installer_image, default_tags)),
                _write(workdir, f"{host}-hostname", _hostname_patch(m)),
            ]
            if m.role == "controlplane":
                patches.append(_write(workdir, f"{host}-cluster", _cluster_patch(cfg, ep)))
            if secrets.tailscale_auth_key:
                patches.append(
                    _write(workdir, f"{host}-tailscale",
                           _tailscale_patch(m, cfg, secrets.tailscale_auth_key))
                )
            # freeform user patches last so they can override
            for i, raw in enumerate(m.config_patches):
                patches.append(_write(workdir, f"{host}-extra-{i}", raw))

            configs[host] = talosctl.gen_config(
                cluster=cfg.name,
                endpoint=endpoint,
                secrets_path=secrets_path,
                output_type="controlplane" if m.role == "controlplane" else "worker",
                install_image=installer_image,
                install_disk=INSTALL_DISK,
                kubernetes_version=cfg.kubernetes_version,
                talos_version=cfg.talos_version,
                patches=patches,
            )
    return configs
