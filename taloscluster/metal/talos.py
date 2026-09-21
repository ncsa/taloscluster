"""The metal provider's machine configuration.

The cabling plan (`metal.<group>.interfaces`) drives one machine's network
configuration: every link gets a classic ``machine.network.interfaces`` device
entry that keeps DHCP off -- the boot link gets nothing else -- and an
``external`` link's VLAN child is created there, while new-style link documents
carry the static addresses, the default route, the MTUs and the external
network's policy routing. The full config is generated through the same
``talosctl gen config`` pipeline as every other node's.

A machine with an ``external`` link also runs the return-path static pod: it
marks the connections entering the VLAN child so their replies leave through
the external gateway instead of the default route -- MetalLB ingress on the
machine would otherwise answer from the wrong interface.

Carried over from the prototype for Talos < 1.14 clusters: the hostname rides
the classic ``machine.network.hostname`` field instead of the ``HostnameConfig``
document, and the 1.14-era ``machine.install.grubUseUKICmdline`` key and the
generated ``HostnameConfig`` document are stripped -- ``--talos-version`` does
not gate what the client emits, and a metal machine is configured in
maintenance mode where the running Talos may be older than the cluster's.
"""

from __future__ import annotations

import ipaddress
import tempfile
from pathlib import Path

import yaml

from .. import versions
from ..config import (
    DEFAULT_MTU,
    Config,
    ConfigError,
    Machine,
    MetalInterface,
    MetalServer,
)
from ..infrastructure import Endpoint, stated_mtu
from ..proxmox.talos import (
    EXT_RETURN_MARK,
    EXT_RETURN_RULE_PRIORITY,
    EXT_RETURN_TABLE,
    EXT_ROUTE_TABLE,
    anchor_address,
)
from ..talos import machineconfig, talosctl

# The Talos release that introduced machine.install.grubUseUKICmdline and the
# HostnameConfig document. The client emits both regardless of --talos-version,
# so a cluster older than this gets the classic forms instead.
HOSTNAME_DOCUMENT_VERSION = "v1.14.0"


def _pre_1_14(cfg: Config) -> bool:
    return versions.is_older(cfg.talos_version, HOSTNAME_DOCUMENT_VERSION)


def _vip(cfg: Config) -> str:
    """The cluster endpoint address: exactly one L2 block carries the VIP."""
    vip = cfg.network.cluster.kubeapi_vip
    if not vip and cfg.network.external is not None:
        vip = cfg.network.external.kubeapi_vip
    if not vip:
        raise ConfigError(
            "cluster.yaml: kubeapi_vip must be set in network.cluster or network.external"
        )
    return vip


def _machine(server: MetalServer, cfg: Config) -> Machine:
    """The server as the shared patch builders see it: pool is the group."""
    return Machine(
        name=server.name,
        role=server.role,
        pool=server.group,
        disk=0,
        extensions=(),
        config_patches=tuple(cfg.talos_config_patches),
    )


def _machine_patch(server: MetalServer, cfg: Config, endpoint: Endpoint,
                   installer_image: str) -> dict:
    m = _machine(server, cfg)
    patch = machineconfig._machine_patch(m, cfg, endpoint, installer_image, server.disk)
    # a group on its own L2 pins the pod node IP to that L2, not the cluster's
    patch["machine"]["kubelet"]["nodeIP"]["validSubnets"] = [server.network.cidr]
    return patch


def _hostname_patch(server: MetalServer, cfg: Config) -> dict:
    """Talos < 1.14 rejects the HostnameConfig document, so the hostname rides
    the classic machine.network.hostname field there."""
    if _pre_1_14(cfg):
        return {"machine": {"network": {"hostname": server.name}}}
    return machineconfig._hostname_patch(_machine(server, cfg))


def _with_prefix(address: str, cidr: str) -> str:
    """A link address needs CIDR notation; a bare address takes the L2's prefix."""
    if "/" in address:
        return address
    return f"{address}/{ipaddress.ip_network(cidr).prefixlen}"


def _external_child(server: MetalServer, cfg: Config, ifname: str,
                    iface: MetalInterface) -> tuple[int, str]:
    """The VLAN id and link name of an external interface's VLAN child."""
    ext = cfg.network.external
    assert ext is not None  # callers refuse the external role without a block
    vlan = iface.vlan if iface.vlan is not None else ext.vlan
    if vlan is None:
        raise ConfigError(
            f"metal server {server.name}: interface {ifname} needs a VLAN id: "
            "set network.external.vlan or the interface's vlan"
        )
    return vlan, iface.link_name or f"{ifname}.{vlan}"


def _check_cabling(server: MetalServer, cfg: Config) -> dict[str, MetalInterface]:
    """The cabling plan a machine's configuration is built from, checked.

    Exactly one `cluster` link carries the node L2 (its static address and the
    default route), at most one `external` link rides the external network, and
    an `external` link needs a `network.external` block describing it.
    """
    interfaces = server.interfaces
    cluster = [n for n, i in interfaces.items() if "cluster" in i.role]
    if len(cluster) != 1:
        raise ConfigError(
            f"metal server {server.name}: exactly one interface with the "
            f"cluster role is required (got {', '.join(sorted(cluster)) or 'none'})"
        )
    external = [n for n, i in interfaces.items() if "external" in i.role]
    if len(external) > 1:
        raise ConfigError(
            f"metal server {server.name}: at most one interface with the "
            f"external role is supported (got {', '.join(sorted(external))})"
        )
    if external and cfg.network.external is None:
        raise ConfigError(
            f"metal server {server.name}: interface {external[0]} has the "
            "external role but cluster.yaml has no network.external block"
        )
    return interfaces


def network_docs(server: MetalServer, cfg: Config) -> list[dict]:
    """The new-style link documents for one machine's cabling plan.

    The `cluster` link states its static address and the default route via the
    group L2's gateway (MTU-clamped on a jumbo L2); an `external` link's VLAN
    child carries the anchor address from `network.external.anchor_cidr`, a
    static address of its own when the interface has no cluster role, and the
    routes to the external network over the return-path routing table. A
    control plane states the kubeapi VIP on the link that carries it.
    """
    docs, _ = _cabling(server, cfg)
    return docs


def device_entries(server: MetalServer, cfg: Config) -> list[dict]:
    """The classic `machine.network.interfaces` device entries for one machine.

    Every link states `dhcp: false` -- static links and the boot link alike
    must never pick up a lease -- and the `external` link's entry creates its
    VLAN child on the parent port.
    """
    _, entries = _cabling(server, cfg)
    return entries


def _cabling(server: MetalServer, cfg: Config) -> tuple[list[dict], list[dict]]:
    """(new-style link documents, classic device entries) for one machine."""
    interfaces = _check_cabling(server, cfg)
    ext = cfg.network.external
    docs: list[dict] = []
    entries: list[dict] = []
    cluster_ifname = next(
        n for n, i in interfaces.items() if "cluster" in i.role
    )
    child_name = ""
    for ifname in sorted(interfaces):
        iface = interfaces[ifname]
        entry: dict = {"interface": ifname, "dhcp": False}
        if "cluster" in iface.role:
            link: dict = {
                "apiVersion": "v1alpha1",
                "kind": "LinkConfig",
                "name": ifname,
            }
            stated = stated_mtu(server.network.mtu)
            if stated is not None:
                link["mtu"] = stated
            if iface.ip:
                link["addresses"] = [
                    {"address": _with_prefix(iface.ip, server.network.cidr)}
                ]
            if server.network.gateway:
                route: dict = {"gateway": server.network.gateway}
                if stated is not None:
                    # a gateway that drops jumbo frames sends no ICMP, so the
                    # route MTU keeps off-subnet traffic working
                    route["mtu"] = DEFAULT_MTU
                link["routes"] = [route]
            docs.append(link)
        if "external" in iface.role:
            assert ext is not None
            vlan, child_name = _external_child(server, cfg, ifname, iface)
            entry["vlans"] = [{"vlanId": vlan}]
            if "cluster" not in iface.role:
                # a dedicated external NIC states its own L2's MTU; a VLAN child
                # can never exceed the port it rides on
                parent: dict = {
                    "apiVersion": "v1alpha1",
                    "kind": "LinkConfig",
                    "name": ifname,
                }
                stated = stated_mtu(ext.mtu)
                if stated is not None:
                    parent["mtu"] = stated
                docs.append(parent)
            child: dict = {
                "apiVersion": "v1alpha1",
                "kind": "LinkConfig",
                "name": child_name,
            }
            addresses = []
            if ext.anchor_cidr:
                addresses.append(
                    {"address": anchor_address(ext.anchor_cidr, cfg.name, server.name)}
                )
            if iface.ip and "cluster" not in iface.role:
                # a dedicated external NIC: its own address rides the child
                addresses.append({"address": _with_prefix(iface.ip, ext.cidr)})
            if addresses:
                child["addresses"] = addresses
            routes = []
            if ext.cidr:
                routes.append({"destination": ext.cidr, "table": EXT_ROUTE_TABLE})
            if ext.gateway:
                routes.append({"gateway": ext.gateway, "table": EXT_ROUTE_TABLE})
            if routes:
                child["routes"] = routes
            # the child inherits the parent port's MTU; state the external L2's
            # own whenever the two differ so a jumbo parent never drags it up
            parent_mtu = server.network.mtu if "cluster" in iface.role else DEFAULT_MTU
            if ext.mtu != parent_mtu:
                child["mtu"] = ext.mtu
            docs.append(child)
        entries.append(entry)

    if child_name:
        # externally initiated connections are marked so their replies return
        # through the external gateway; the rule sends marked traffic to table 100
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
    if server.role == "controlplane":
        vip = _vip(cfg)
        on_external = bool(cfg.network.external and cfg.network.external.kubeapi_vip)
        if not on_external or child_name:
            docs.append(
                {
                    "apiVersion": "v1alpha1",
                    "kind": "Layer2VIPConfig",
                    "name": vip,
                    "link": child_name if on_external else cluster_ifname,
                }
            )
    dns = next(
        (i.dns for i in (interfaces[n] for n in sorted(interfaces)) if i.dns), None
    ) or tuple(cfg.network.dns)
    if dns:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "ResolverConfig",
                "nameservers": [{"address": d} for d in dns],
            }
        )
    return docs, entries


def _strip_pre_1_14(docs: list[dict]) -> list[dict]:
    """Drop the 1.14-era bits the client emits regardless of --talos-version."""
    for doc in docs:
        install = (doc.get("machine") or {}).get("install")
        if isinstance(install, dict):
            install.pop("grubUseUKICmdline", None)
    return [doc for doc in docs if doc.get("kind") != "HostnameConfig"]


def _external_child_link(server: MetalServer, cfg: Config) -> str:
    """The VLAN child link name of the machine's external link, or none."""
    external = [n for n, i in server.interfaces.items() if "external" in i.role]
    if not external:
        return ""
    return _external_child(server, cfg, external[0], server.interfaces[external[0]])[1]


def return_path_pod(server: MetalServer, cfg: Config, child: str,
                    kubernetes_version: str | None = None) -> dict:
    """The static pod marking the connections entering the machine's VLAN child.

    The same marking the Proxmox return-path pod installs, matched on the
    child link's stable name instead of a generated MAC: replies to
    externally initiated connections are marked so the fwmark routing rule
    sends them back through the external gateway, not the default route.
    `kubernetes_version` retags the kube-proxy image to the cluster's RUNNING
    version, the same override `build_configs` applies for the VM providers.
    """
    ext = cfg.network.external
    assert ext is not None  # an external link implies a network.external block
    mark = f"0x{EXT_RETURN_MARK:08x}"
    ingress_rule = (
        f'iifname "{child}" ip daddr {ext.cidr} '
        f"ct direction original ct mark set ct mark | {mark}"
    )
    script = f"""\
set -eu
NFT=nft

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
            "name": "taloscluster-metal-return-path",
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
                    # The version below is the TARGET; build_config passes the
                    # cluster's RUNNING version once a kubeconfig exists, so a
                    # machine joined after a version bump never pulls the
                    # target kube-proxy before the minor-by-minor upgrade.
                    "image": f"registry.k8s.io/kube-proxy:"
                             f"{kubernetes_version or cfg.kubernetes_version}",
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


def build_config(
    server: MetalServer,
    cfg: Config,
    secrets_path: Path,
    installer_image: str,
    kubernetes_version: str | None = None,
) -> str:
    """Return one metal machine's machine-config YAML string.

    The shared patch stack (machine, hostname, cluster, firewall, kubespan) is
    assembled exactly as `build_configs` does for the VM providers -- the
    firewall keyed on the machine's own L2 -- then the cabling plan's network
    patches and the cluster's freeform patches; a Talos < 1.14 cluster gets the
    classic hostname field and the 1.14-era keys stripped. `kubernetes_version`
    overrides `cfg.kubernetes_version` for the kubelet and control-plane images
    and the return-path pod's kube-proxy image: `metal apply` passes the
    running cluster's version so a machine joined after a `kubernetes.version`
    bump never starts newer than the API server -- the target is only for a
    cluster that has never been bootstrapped.
    """
    kubernetes = kubernetes_version or cfg.kubernetes_version
    host = server.name
    vip = _vip(cfg)
    endpoint = Endpoint(vip=vip, advertised_address=vip)
    m = _machine(server, cfg)
    with tempfile.TemporaryDirectory(prefix="taloscluster-metal-mc-") as tmp:
        workdir = Path(tmp)
        patches = [
            machineconfig._write(
                workdir, f"{host}-machine",
                _machine_patch(server, cfg, endpoint, installer_image),
            ),
            machineconfig._write(
                workdir, f"{host}-hostname", _hostname_patch(server, cfg)
            ),
        ]
        if server.role == "controlplane":
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-cluster",
                    machineconfig._cluster_patch(cfg, endpoint),
                )
            )
        patches.append(
            machineconfig._write(
                workdir, f"{host}-firewall",
                machineconfig._firewall_docs(cfg, node_cidr=server.network.cidr),
            )
        )
        if cfg.kubespan:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-kubespan",
                    # the WireGuard MTU follows the machine's own L2, which a
                    # group on another network carries with its own mtu
                    machineconfig._kubespan_patch(cfg, mtu=server.network.mtu),
                )
            )
        docs, entries = _cabling(server, cfg)
        patches.append(machineconfig._write(workdir, f"{host}-network", docs))
        if entries:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-interfaces",
                    {"machine": {"network": {"interfaces": entries}}},
                )
            )
        child = _external_child_link(server, cfg)
        if child:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-return-path",
                    {"machine": {"pods": [
                        return_path_pod(server, cfg, child, kubernetes)
                    ]}},
                )
            )
        for i, raw in enumerate(m.config_patches):
            patches.append(machineconfig._write(workdir, f"{host}-extra-{i}", raw))

        raw = talosctl.gen_config(
            cluster=cfg.name,
            endpoint=f"https://{vip}:6443",
            secrets_path=secrets_path,
            output_type="controlplane" if server.role == "controlplane" else "worker",
            install_image=installer_image,
            install_disk=server.disk,
            kubernetes_version=kubernetes,
            talos_version=cfg.talos_version,
            patches=patches,
        )
    generated = [doc for doc in yaml.safe_load_all(raw) if doc]
    if _pre_1_14(cfg):
        generated = _strip_pre_1_14(generated)
    return yaml.safe_dump_all(generated, sort_keys=False, explicit_start=True)
