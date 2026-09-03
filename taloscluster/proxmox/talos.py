"""Proxmox's contribution to a node's Talos machine configuration.

Everything provider-specific about Proxmox networking lives here: MAC-selected
link aliases, the external NIC's anchor address, the dedicated external routing
table, the Layer 2 API VIP, and the conntrack return-path static pod. The shared
generator in ``taloscluster.talos.machineconfig`` only composes what this module
returns.
"""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Any

from .. import naming
from ..config import Config, ConfigError, Machine, ProxmoxConfig, ProxmoxSdn, proxmox_sdn
from ..infrastructure import Endpoint, TalosContribution, TalosPatch

# Proxmox VMs boot from a virtio-scsi disk.
INSTALL_DISK = "/dev/sda"
INSTALLER_PLATFORM = "nocloud"

EXT_ROUTE_TABLE = "100"
EXT_RULE_PRIORITY = "1000"
EXT_RETURN_RULE_PRIORITY = "1001"
EXT_RETURN_MARK = 0x2000
EXT_RETURN_TABLE = "taloscluster_return_path"


def _provider(cfg: Config) -> ProxmoxConfig:
    assert isinstance(cfg.provider, ProxmoxConfig)
    return cfg.provider


def external_network(cfg: Config) -> dict[str, Any]:
    """The directly routed external NIC settings, or an empty mapping."""
    value = _provider(cfg).network.get("external")
    return value if isinstance(value, dict) else {}


def has_ingress(cfg: Config) -> bool:
    """True when an external NIC carries a MetalLB address pool."""
    return bool(external_network(cfg).get("ingress_pool"))


def sdn(cfg: Config) -> ProxmoxSdn | None:
    """The managed-SDN settings, or None when using an existing bridge/VNet."""
    return proxmox_sdn(cfg.name, _provider(cfg))


def _private_link_docs(m: Machine, cfg: Config) -> list[dict]:
    """LinkAliasConfig + address documents for the private NIC.

    On an existing bridge the private NIC keeps DHCP. On a managed EVPN
    network there is no DHCP: the node gets its deterministic static address
    and a default route via the anycast gateway.
    """
    private_mac = naming.mac_address(cfg.name, m.name, 0)
    docs: list[dict] = [
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkAliasConfig",
            "name": "private",
            "selector": {"match": f'mac(link.permanent_addr) == "{private_mac}"'},
        }
    ]
    link: dict = {
        "apiVersion": "v1alpha1",
        "kind": "LinkConfig",
        "name": "private",
    }
    managed = sdn(cfg)
    if managed:
        address = naming.node_address(
            cfg.cidr, m.name, m.role, m.pool, tuple(cfg.workers)
        )
        link["addresses"] = [{"address": str(address)}]
        link["routes"] = [{"gateway": str(naming.sdn_gateway(cfg.cidr))}]
        docs.append(link)
    else:
        docs.append(link)
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "DHCPv4Config",
                "name": "private",
            }
        )
    return docs


def anchor_address(anchor_cidr: str, cluster: str, hostname: str) -> str:
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


def anchor_addresses(
    anchor_cidr: str, cluster: str, machines: dict[str, Machine]
) -> dict[str, str]:
    """Generate anchor addresses for all machines, rejecting collisions."""
    anchors: dict[str, str] = {}
    for hostname in machines:
        addr = anchor_address(anchor_cidr, cluster, hostname)
        if addr in anchors.values():
            raise ConfigError(
                f"anchor address collision: {addr} generated for multiple machines"
            )
        anchors[hostname] = addr
    return anchors


def vip(cfg: Config) -> tuple[str, str]:
    """Return (vip_address, link_name) for the Proxmox kubeapi VIP.

    When external is configured with a kubeapi_vip, the VIP lives on the
    external link with direct routing. Otherwise it lives on the private link.
    """
    ext = external_network(cfg)
    if ext.get("kubeapi_vip"):
        return str(ext["kubeapi_vip"]), "external"
    return str(_provider(cfg).network["cluster"]["kubeapi_vip"]), "private"


def return_path_pod(m: Machine, cfg: Config) -> dict:
    """Build the static pod that marks externally initiated connections."""
    external = external_network(cfg)
    external_mac = naming.mac_address(cfg.name, m.name, 1).lower()
    mark = f"0x{EXT_RETURN_MARK:08x}"
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
  "$NFT" delete table ip {EXT_RETURN_TABLE} 2>/dev/null || true
}}
trap cleanup EXIT
cleanup

"$NFT" -f - <<EOF
table ip {EXT_RETURN_TABLE} {{
  chain prerouting {{
    type filter hook prerouting priority -160; policy accept;
    {ingress_rule}
    ct direction reply ct mark & {mark} != 0 meta mark set meta mark | {mark}
  }}
}}
EOF

while "$NFT" list table ip {EXT_RETURN_TABLE} >/dev/null 2>&1; do
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


def external_network_docs(m: Machine, cfg: Config) -> list[dict]:
    """Native Talos v1.13 multi-document network config for the external NIC.

    Generates LinkAliasConfig (select by MAC), LinkConfig, DHCPv4Config,
    RoutingRuleConfig, and Layer2VIPConfig documents.  Once any new-style link
    document is present, Talos disables default DHCP on physical links, so the
    private NIC gets explicit DHCPv4Config + LinkConfig documents too.
    """
    ext = external_network(cfg)
    cluster = cfg.name
    external_mac = naming.mac_address(cluster, m.name, 1)
    anchor = anchor_address(ext["anchor_cidr"], cluster, m.name)
    vip_address, vip_link = vip(cfg)
    vip_on_external = vip_link == "external"
    return_path = has_ingress(cfg)

    docs: list[dict] = _private_link_docs(m, cfg)
    docs.append(
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkAliasConfig",
            "name": "external",
            "selector": {"match": f'mac(link.permanent_addr) == "{external_mac}"'},
        }
    )

    ext_link: dict = {
        "apiVersion": "v1alpha1",
        "kind": "LinkConfig",
        "name": "external",
        "addresses": [{"address": anchor}],
    }
    if (m.role == "controlplane" and vip_on_external) or return_path:
        ext_link["routes"] = [
            {"destination": ext["cidr"], "table": EXT_ROUTE_TABLE},
            {"gateway": ext["gateway"], "table": EXT_ROUTE_TABLE},
        ]
    docs.append(ext_link)

    if m.role == "controlplane":
        if vip_on_external:
            docs.append(
                {
                    "apiVersion": "v1alpha1",
                    "kind": "RoutingRuleConfig",
                    "name": EXT_RULE_PRIORITY,
                    "src": f"{vip_address}/32",
                    "table": EXT_ROUTE_TABLE,
                }
            )
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "Layer2VIPConfig",
                "name": vip_address,
                "link": vip_link,
            }
        )
    if return_path:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "RoutingRuleConfig",
                "name": EXT_RETURN_RULE_PRIORITY,
                "fwMark": EXT_RETURN_MARK,
                "fwMask": EXT_RETURN_MARK,
                "table": EXT_ROUTE_TABLE,
            }
        )
    return docs


def contribution(m: Machine, cfg: Config, endpoint: Endpoint) -> TalosContribution:
    """Proxmox install disk plus its network and return-path patches."""
    ext = external_network(cfg)
    managed = sdn(cfg)
    if not ext:
        if managed:
            # A managed EVPN network has no DHCP: static private address plus
            # a Layer 2 VIP for control planes, all as new-style documents.
            docs = _private_link_docs(m, cfg)
            if m.role == "controlplane":
                docs.append(
                    {
                        "apiVersion": "v1alpha1",
                        "kind": "Layer2VIPConfig",
                        "name": endpoint.vip,
                        "link": "private",
                    }
                )
            patches = [TalosPatch("network", docs), _nameservers_patch(cfg)]
            return TalosContribution(install_disk=INSTALL_DISK, patches=tuple(patches))
        # No external NIC: the API VIP rides the private link as a legacy
        # machine.network interface, exactly like OpenStack.
        interfaces = (
            [{"interface": "eth0", "dhcp": True, "vip": {"ip": endpoint.vip}}]
            if m.role == "controlplane"
            else []
        )
        return TalosContribution(
            install_disk=INSTALL_DISK,
            patches=(
                TalosPatch("network", {"machine": {"network": {"interfaces": interfaces}}}),
            ),
        )

    patches = [TalosPatch("network", external_network_docs(m, cfg))]
    if managed:
        patches.append(_nameservers_patch(cfg))
    if has_ingress(cfg):
        patches.append(
            TalosPatch("return-path", {"machine": {"pods": [return_path_pod(m, cfg)]}})
        )
    return TalosContribution(install_disk=INSTALL_DISK, patches=tuple(patches))


def _nameservers_patch(cfg: Config) -> TalosPatch:
    """Static addressing has no DHCP-provided DNS, so name the servers explicitly."""
    return TalosPatch(
        "nameservers",
        {"machine": {"network": {"nameservers": list(cfg.dns)}}},
    )
