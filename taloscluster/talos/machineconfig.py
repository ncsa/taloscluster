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

import hashlib
import ipaddress
import tempfile
from pathlib import Path

import yaml

from .. import naming
from ..config import Config, ConfigError, Machine, ProxmoxConfig, Secrets
from ..infrastructure import Endpoint
from . import talosctl

INSTALL_DISKS = {
    "openstack": "/dev/vda",
    "proxmox": "/dev/sda",
}
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

_EXT_ROUTE_TABLE = "100"
_EXT_RULE_PRIORITY = "1000"
_EXT_RETURN_RULE_PRIORITY = "1001"
_EXT_RETURN_MARK = 0x2000
_EXT_RETURN_TABLE = "taloscluster_return_path"


def _has_proxmox_external(cfg: Config) -> bool:
    """True when Proxmox is configured with a directly routed external NIC."""
    if not isinstance(cfg.provider, ProxmoxConfig):
        return False
    return bool(cfg.provider.network.get("external"))


def _has_proxmox_ingress(cfg: Config) -> bool:
    """True when Proxmox has an external network and MetalLB address pool."""
    if not isinstance(cfg.provider, ProxmoxConfig):
        return False
    external = cfg.provider.network.get("external") or {}
    return bool(external.get("ingress_pool"))


def _anchor_address(anchor_cidr: str, cluster: str, hostname: str) -> str:
    """Derive a deterministic link-local /32 anchor from cluster + hostname."""
    net = ipaddress.ip_network(anchor_cidr, strict=False)
    if net.prefixlen >= 31:
        num_hosts = net.num_addresses
    else:
        num_hosts = net.num_addresses - 2  # skip network + broadcast
    if num_hosts < 1:
        raise ConfigError(
            f"anchor_cidr {anchor_cidr!r} is too small to allocate addresses"
        )
    digest = hashlib.sha256(f"{cluster}/{hostname}".encode()).digest()
    offset = int.from_bytes(digest[:4], "big") % num_hosts
    skip = 0 if net.prefixlen >= 31 else 1  # skip network address
    host_int = int(net.network_address) + offset + skip
    return f"{ipaddress.ip_address(host_int)}/32"


def _anchor_addresses(
    anchor_cidr: str, cluster: str, machines: dict[str, Machine]
) -> dict[str, str]:
    """Generate anchor addresses for all machines, rejecting collisions."""
    anchors: dict[str, str] = {}
    for hostname in machines:
        addr = _anchor_address(anchor_cidr, cluster, hostname)
        if addr in anchors.values():
            raise ConfigError(
                f"anchor address collision: {addr} generated for multiple machines"
            )
        anchors[hostname] = addr
    return anchors


def _proxmox_vip(cfg: Config) -> tuple[str, str]:
    """Return (vip_address, link_name) for the Proxmox kubeapi VIP.

    When external is configured with a kubeapi_vip, the VIP lives on the
    external link with direct routing. Otherwise it lives on the private link.
    """
    assert isinstance(cfg.provider, ProxmoxConfig)
    ext = cfg.provider.network.get("external") or {}
    if ext.get("kubeapi_vip"):
        return str(ext["kubeapi_vip"]), "external"
    return str(cfg.provider.network["cluster"]["kubeapi_vip"]), "private"


def _proxmox_return_path_pod(m: Machine, cfg: Config) -> dict:
    """Build the static pod that marks externally initiated connections."""
    assert isinstance(cfg.provider, ProxmoxConfig)
    external = cfg.provider.network["external"]
    external_mac = naming.mac_address(cfg.name, m.name, 1).lower()
    mark = f"0x{_EXT_RETURN_MARK:08x}"
    ingress_rule = (
        f'iifname "$external_if" ip daddr {external["cidr"]} '
        f"ct direction original ct mark set ct mark | {mark}"
    )
    script = f"""\
set -eu
NFT=nft

external_if=
for address_file in /sys/class/net/*/address; do
  if [ "$(cat "$address_file")" = "{external_mac}" ]; then
    external_if="${{address_file%/address}}"
    external_if="${{external_if##*/}}"
    break
  fi
done
if [ -z "$external_if" ]; then
  echo "external interface with MAC {external_mac} was not found" >&2
  exit 1
fi

cleanup() {{
  "$NFT" delete table ip {_EXT_RETURN_TABLE} 2>/dev/null || true
}}
trap cleanup EXIT
cleanup

"$NFT" -f - <<EOF
table ip {_EXT_RETURN_TABLE} {{
  chain prerouting {{
    type filter hook prerouting priority -160; policy accept;
    {ingress_rule}
    ct direction reply ct mark & {mark} != 0 meta mark set meta mark | {mark}
  }}
}}
EOF

while "$NFT" list table ip {_EXT_RETURN_TABLE} >/dev/null 2>&1; do
  sleep 30
done
exit 1
"""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "taloscluster-proxmox-return-path",
            "namespace": "kube-system",
        },
        "spec": {
            "hostNetwork": True,
            "priorityClassName": "system-node-critical",
            "restartPolicy": "Always",
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "return-path",
                    # kube-proxy ships nft (it runs in nftables mode) and is
                    # already present on every node; Talos has no host nft
                    # visible to the kubelet, so hostPath mounts can't work.
                    "image": f"registry.k8s.io/kube-proxy:{cfg.kubernetes_version}",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-ec", script],
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {
                            "drop": ["ALL"],
                            "add": ["NET_ADMIN"],
                        },
                    },
                    "resources": {
                        "requests": {"cpu": "5m", "memory": "8Mi"},
                        "limits": {"memory": "32Mi"},
                    },
                }
            ],
        },
    }


def _proxmox_external_network_docs(m: Machine, cfg: Config) -> str:
    """Native Talos v1.13 multi-document network config for the external NIC.

    Generates LinkAliasConfig (select by MAC), LinkConfig, DHCPv4Config,
    RoutingRuleConfig, and Layer2VIPConfig documents.  Once any new-style link
    document is present, Talos disables default DHCP on physical links, so the
    private NIC gets explicit DHCPv4Config + LinkConfig documents too.
    """
    assert isinstance(cfg.provider, ProxmoxConfig)
    ext = cfg.provider.network["external"]
    cluster = cfg.name
    private_mac = naming.mac_address(cluster, m.name, 0)
    external_mac = naming.mac_address(cluster, m.name, 1)
    anchor = _anchor_address(ext["anchor_cidr"], cluster, m.name)
    vip, vip_link = _proxmox_vip(cfg)
    vip_on_external = vip_link == "external"
    return_path = _has_proxmox_ingress(cfg)

    docs: list[dict] = [
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkAliasConfig",
            "name": "private",
            "selector": {"match": f'mac(link.permanent_addr) == "{private_mac}"'},
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkConfig",
            "name": "private",
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "DHCPv4Config",
            "name": "private",
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkAliasConfig",
            "name": "external",
            "selector": {"match": f'mac(link.permanent_addr) == "{external_mac}"'},
        },
    ]

    ext_link: dict = {
        "apiVersion": "v1alpha1",
        "kind": "LinkConfig",
        "name": "external",
        "addresses": [{"address": anchor}],
    }
    if (m.role == "controlplane" and vip_on_external) or return_path:
        ext_link["routes"] = [
            {"destination": ext["cidr"], "table": _EXT_ROUTE_TABLE},
            {"gateway": ext["gateway"], "table": _EXT_ROUTE_TABLE},
        ]
    docs.append(ext_link)

    if m.role == "controlplane":
        if vip_on_external:
            docs.append(
                {
                    "apiVersion": "v1alpha1",
                    "kind": "RoutingRuleConfig",
                    "name": _EXT_RULE_PRIORITY,
                    "src": f"{vip}/32",
                    "table": _EXT_ROUTE_TABLE,
                }
            )
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "Layer2VIPConfig",
                "name": vip,
                "link": vip_link,
            }
        )
    if return_path:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "RoutingRuleConfig",
                "name": _EXT_RETURN_RULE_PRIORITY,
                "fwMark": _EXT_RETURN_MARK,
                "fwMask": _EXT_RETURN_MARK,
                "table": _EXT_ROUTE_TABLE,
            }
        )

    return yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False)


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


def _install_disk(cfg: Config) -> str:
    """Return the disk name exposed by the selected provider's VM bus."""
    return INSTALL_DISKS[cfg.provider_name]


def _machine_patch(m: Machine, cfg: Config, endpoint: Endpoint, installer_image: str,
                   default_tags: dict[str, str] | None = None) -> dict:
    machine: dict = {
        "certSANs": [endpoint.advertised_address],
        "nodeLabels": _node_labels(m, default_tags),
        "kubelet": {
            "extraArgs": {"rotate-server-certificates": True},
            # pin node ip to the private net so pod traffic never rides tailscale
            "nodeIP": {"validSubnets": [cfg.cidr]},
        },
        "install": {
            "disk": _install_disk(cfg),
            "image": installer_image,
            "wipe": True,
        },
        "time": {"servers": cfg.ntp},
    }
    # Native Talos v1.13 network documents replace legacy machine.network.interfaces
    # when the external NIC is configured; otherwise keep the legacy interface config.
    if not _has_proxmox_external(cfg):
        machine["network"] = {"interfaces": (
            [{"interface": "eth0", "dhcp": True, "vip": {"ip": endpoint.vip}}]
            if m.role == "controlplane"
            else []
        )}
    elif _has_proxmox_ingress(cfg):
        machine["pods"] = [_proxmox_return_path_pod(m, cfg)]
    return {"machine": machine}


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
    endpoint: Endpoint,
    secrets_path: Path,
    installer_images: dict[tuple[str, ...], str],
    default_tags: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {hostname -> machine-config YAML string} for every machine."""
    cluster_endpoint = f"https://{endpoint.advertised_address}:6443"
    configs: dict[str, str] = {}

    # Pre-generate anchor addresses and reject collisions before writing any config.
    if isinstance(cfg.provider, ProxmoxConfig) and cfg.provider.network.get("external"):
        _anchor_addresses(
            cfg.provider.network["external"]["anchor_cidr"], cfg.name, machines
        )

    with tempfile.TemporaryDirectory(prefix="taloscluster-mc-") as tmp:
        workdir = Path(tmp)
        for host, m in machines.items():
            installer_image = installer_images[m.extensions]
            patches: list[Path] = [
                _write(workdir, f"{host}-machine",
                       _machine_patch(m, cfg, endpoint, installer_image, default_tags)),
                _write(workdir, f"{host}-hostname", _hostname_patch(m)),
            ]
            if isinstance(cfg.provider, ProxmoxConfig) and cfg.provider.network.get("external"):
                patches.append(
                    _write(workdir, f"{host}-network",
                           _proxmox_external_network_docs(m, cfg))
                )
            if m.role == "controlplane":
                patches.append(
                    _write(workdir, f"{host}-cluster", _cluster_patch(cfg, endpoint))
                )
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
                endpoint=cluster_endpoint,
                secrets_path=secrets_path,
                output_type="controlplane" if m.role == "controlplane" else "worker",
                install_image=installer_image,
                install_disk=_install_disk(cfg),
                kubernetes_version=cfg.kubernetes_version,
                talos_version=cfg.talos_version,
                patches=patches,
            )
    return configs
