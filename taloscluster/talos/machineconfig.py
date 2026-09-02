"""Build each node's Talos machine config, the port of talos.tf:51-158.

The four yamlencode patches become Python dicts dumped to YAML files and stacked
as `--config-patch` on `talosctl gen config`, in the same order terraform used:
  machine -> hostname -> (cluster, controlplane only) -> tailscale -> freeform.

Kept as separate patch files on purpose: hostname (HostnameConfig) and tailscale
(ExtensionServiceConfig) are their own machine-config documents, and the
hostname patch relies on the `$patch: delete` directive to drop the `auto` field
-- both only behave correctly as standalone patch docs, never dump_all'd into
one stream.

This module is provider-neutral. Everything a specific infrastructure backend
needs -- the install disk, provider networking, provider static pods -- arrives
as a :class:`~taloscluster.infrastructure.TalosContribution` built by that
backend, and is stacked after the shared patches but before the user's freeform
patches so an explicit user override still wins.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import yaml

from ..config import Config, ConfigError, Machine, Secrets
from ..infrastructure import Endpoint, TalosContribution
from . import talosctl

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


def _machine_patch(m: Machine, cfg: Config, endpoint: Endpoint, installer_image: str,
                   install_disk: str, default_tags: dict[str, str] | None = None) -> dict:
    return {
        "machine": {
            "certSANs": [endpoint.advertised_address],
            "nodeLabels": _node_labels(m, default_tags),
            "kubelet": {
                "extraArgs": {"rotate-server-certificates": True},
                # pin node ip to the private net so pod traffic never rides tailscale
                "nodeIP": {"validSubnets": [cfg.cidr]},
            },
            "install": {
                "disk": install_disk,
                "image": installer_image,
                "wipe": True,
            },
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


def _cluster_patch(cfg: Config, endpoint: Endpoint) -> dict:
    return {
        "cluster": {
            "allowSchedulingOnControlPlanes": False,
            "extraManifests": EXTRA_MANIFESTS,
            "apiServer": {"certSANs": [endpoint.advertised_address]},
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


# A patch name becomes a filename, so it may only be a plain identifier: a
# provider is third-party code and must not be able to steer writes out of the
# temporary workdir.
_PATCH_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _patch_stem(host: str, name: str) -> str:
    if not isinstance(name, str) or not _PATCH_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"provider Talos patch name {name!r} for {host} must be lowercase "
            "letters, numbers and internal hyphens"
        )
    return f"{host}-{name}"


def _write(workdir: Path, stem: str, doc) -> Path:
    """Dump a patch dict, a list of Talos documents, or raw YAML to its own file."""
    path = workdir / f"{stem}.yaml"
    if isinstance(doc, str):
        path.write_text(doc)
    elif isinstance(doc, list):
        path.write_text(yaml.safe_dump_all(doc, sort_keys=False, default_flow_style=False))
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return path


def build_configs(
    cfg: Config,
    secrets: Secrets,
    machines: dict[str, Machine],
    endpoint: Endpoint,
    secrets_path: Path,
    installer_images: dict[tuple[str, ...], str],
    contributions: dict[str, TalosContribution],
    default_tags: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {hostname -> machine-config YAML string} for every machine.

    `contributions` carries one provider contribution per hostname; the shared
    patches are written first, the provider's next, and the user's freeform
    patches last.
    """
    cluster_endpoint = f"https://{endpoint.advertised_address}:6443"
    configs: dict[str, str] = {}
    missing = sorted(set(machines) - set(contributions))
    if missing:
        raise ConfigError(
            "no provider Talos contribution for: " + ", ".join(missing)
        )

    with tempfile.TemporaryDirectory(prefix="taloscluster-mc-") as tmp:
        workdir = Path(tmp)
        for host, m in machines.items():
            installer_image = installer_images[m.extensions]
            contribution = contributions[host]
            patches: list[Path] = [
                _write(workdir, f"{host}-machine",
                       _machine_patch(m, cfg, endpoint, installer_image,
                                      contribution.install_disk, default_tags)),
                _write(workdir, f"{host}-hostname", _hostname_patch(m)),
            ]
            if m.role == "controlplane":
                patches.append(
                    _write(workdir, f"{host}-cluster", _cluster_patch(cfg, endpoint))
                )
            if secrets.tailscale_auth_key:
                patches.append(
                    _write(workdir, f"{host}-tailscale",
                           _tailscale_patch(m, cfg, secrets.tailscale_auth_key))
                )
            # provider contributions before the user's, so an explicit user
            # patch still has the last word
            for patch in contribution.patches:
                patches.append(
                    _write(workdir, _patch_stem(host, patch.name), patch.document)
                )
            # freeform user patches last so they can override
            for i, raw in enumerate(m.config_patches):
                patches.append(_write(workdir, f"{host}-extra-{i}", raw))

            configs[host] = talosctl.gen_config(
                cluster=cfg.name,
                endpoint=cluster_endpoint,
                secrets_path=secrets_path,
                output_type="controlplane" if m.role == "controlplane" else "worker",
                install_image=installer_image,
                install_disk=contribution.install_disk,
                kubernetes_version=cfg.kubernetes_version,
                talos_version=cfg.talos_version,
                patches=patches,
            )
    return configs
