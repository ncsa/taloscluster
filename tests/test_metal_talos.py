"""Golden test: the metal patch stack and the Talos < 1.14 handling.

The metal machine config is generated through the same ``talosctl gen config``
pipeline as the VM providers, so this pins every document handed to it for a
bare-metal worker whose cabling plan is a PXE boot link plus one NIC carrying
both the cluster and the external role -- the csfarm shape. On a jumbo group
L2 the cluster link states its MTU and clamps the default route to 1500, and
the external VLAN child states its own (default) MTU so it never inherits the
parent's. The stack carries the shared ingress firewall keyed on the group's
L2, and the KubeSpan patch rides along with its endpoint filters; the
external child brings the return-path static pod that marks ingress
connections for the policy-routing rule.

The Talos < 1.14 handling is pinned end to end: the hostname patch is the
classic ``machine.network.hostname`` and the generated config loses the
``machine.install.grubUseUKICmdline`` key and the ``HostnameConfig`` document
the client emits regardless of ``--talos-version``; a 1.14 cluster keeps the
document form and the generated output passes through untouched.

Update the golden only when a machine-config change is intended.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster.config import ConfigError
from taloscluster.infrastructure import Endpoint
from taloscluster.metal import talos as metal_talos
from taloscluster.talos import machineconfig

INSTALLER = "factory.talos.dev/metal-installer/abc123:v1.13.9"
VIP = "172.29.21.200"
ANCHOR = "169.254.37.35/32"  # sha256(testcluster/rp001) in 169.254.32.0/20

EXTERNAL = {
    "cidr": "203.0.113.0/24",
    "gateway": "203.0.113.1",
    "vlan": 1691,
    "anchor_cidr": "169.254.32.0/20",
}

# the csfarm shape: PXE boot link, one [cluster, external] NIC on a jumbo L2
PHOENIX = {
    "role": "worker",
    "disk": "/dev/sda",
    "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1", "mtu": 9000},
    "interfaces": {
        "enp1s0f0": {"role": "pxe"},
        "enp2s0f0": {"role": ["cluster", "external"], "dns": ["198.51.100.53"]},
    },
    "servers": {"rp001": {"interfaces": {"enp2s0f0": {"ip": "172.29.21.5/24"}}}},
}


def _cfg(make_config, *, metal=None, external=EXTERNAL, talos_version=None,
         kubernetes_version=None, tailscale=None, vip=VIP, tags=None):
    cluster: dict = {
        "cidr": "172.29.21.0/24", "gateway": "172.29.21.1", "mtu": 9000,
    }
    if vip is not None:
        cluster["kubeapi_vip"] = vip
    network: dict = {
        "cluster": cluster,
        "dns": ["192.0.2.53"],
    }
    if external is not None:
        network["external"] = external
    proxmox_network: dict = {"cluster": {"bridge": "vmbr0"}}
    if external is not None:
        proxmox_network["external"] = {"bridge": "br-ext"}
    overrides: dict = {
        "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
        "network": network,
        "proxmox": {
            "url": "https://pve.example:8006",
            "storage": "vms",
            "iso_storage": "isos",
            "network": proxmox_network,
        },
        "metal": {"phoenix": PHOENIX if metal is None else metal},
    }
    if tailscale is not None:
        overrides["tailscale"] = tailscale
    if tags is not None:
        overrides["tags"] = tags
    if kubernetes_version is not None:
        overrides["kubernetes"] = {"version": kubernetes_version}
    # the golden stack carries the KubeSpan patch, so the config opts in
    talos: dict = {"kubespan": True}
    if talos_version is not None:
        talos["version"] = talos_version
    overrides["talos"] = talos
    return make_config(overrides, remove=("openstack",))


def _endpoint(cfg) -> Endpoint:
    """The endpoint a Proxmox cluster's network phase resolves: the configured
    VIP, advertised as it is (exactly one of the two sections carries it)."""
    ext = cfg.network.external
    vip = cfg.network.cluster.kubeapi_vip or (ext.kubeapi_vip if ext else "")
    return Endpoint(vip=vip, advertised_address=vip)


def _render(monkeypatch, output: str) -> dict[str, list]:
    rendered: dict[str, list] = {}

    def fake_gen_config(**kwargs):
        host = Path(kwargs["patches"][0]).name.removesuffix("-machine.yaml")
        rendered[host] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        return output

    monkeypatch.setattr(metal_talos.talosctl, "gen_config", fake_gen_config)
    return rendered


def _build(
    make_config, monkeypatch, tmp_path, output=None, *, endpoint=None,
    default_tags=None, **kwargs
) -> tuple[dict[str, list], str]:
    cfg = _cfg(make_config, **kwargs)
    rendered = _render(monkeypatch, output or _GEN_OUTPUT)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    out = metal_talos.build_config(
        server, cfg, secrets_path, INSTALLER, endpoint or _endpoint(cfg),
        default_tags=default_tags,
    )
    assert len(rendered) == 1
    return rendered["rp001"], out


MACHINE_PATCH = {
    "machine": {
        "certSANs": [VIP],
        "nodeLabels": {
            "ncsa/role": "worker", "ncsa/pool": "phoenix",
            "ncsa/project": "bbdb", "team": "platform",
        },
        "kubelet": {
            "extraArgs": {"rotate-server-certificates": True},
            "nodeIP": {"validSubnets": ["172.29.21.0/24"]},
        },
        "install": {"disk": "/dev/sda", "image": INSTALLER, "wipe": True},
        "time": {"servers": ["ntp.example.com"]},
    }
}

HOSTNAME_FIELD_PATCH = {"machine": {"network": {"hostname": "rp001"}}}

HOSTNAME_DOCUMENT_PATCH = {
    "apiVersion": "v1alpha1",
    "kind": "HostnameConfig",
    "auto": {"$patch": "delete"},
    "hostname": "rp001",
}

KUBESPAN_PATCH = {
    "machine": {"network": {"kubespan": {
        "enabled": True,
        "mtu": 8920,
        "filters": {"endpoints": [
            "0.0.0.0/0", "!203.0.113.0/24", "!169.254.32.0/20",
        ]},
    }}}
}

NETWORK_DOCS = [
    {
        "apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "enp2s0f0",
        "mtu": 9000,
        "addresses": [{"address": "172.29.21.5/24"}],
        "routes": [{"gateway": "172.29.21.1", "mtu": 1500}],
    },
    {
        "apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "enp2s0f0.1691",
        "mtu": 1500,
        "addresses": [{"address": ANCHOR}],
        "routes": [
            {"destination": "203.0.113.0/24", "table": "100"},
            {"gateway": "203.0.113.1", "table": "100"},
        ],
    },
    {
        "apiVersion": "v1alpha1", "kind": "RoutingRuleConfig",
        "name": "1001", "fwMark": 8192, "fwMask": 8192, "table": "100",
    },
    {
        "apiVersion": "v1alpha1", "kind": "ResolverConfig",
        "nameservers": [{"address": "198.51.100.53"}],
    },
]

DEVICES_PATCH = {
    "machine": {"network": {"interfaces": [
        {"interface": "enp1s0f0", "dhcp": False},
        {"interface": "enp2s0f0", "dhcp": False, "vlans": [{"vlanId": 1691}]},
    ]}}
}

# the group sits on the cluster L2, so the firewall is the standard stack with
# no KubeSpan rule; the mixed-L2 shape is pinned by the test below
FIREWALL_DOCS = [
    {"apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block"},
    {
        "apiVersion": "v1alpha1", "kind": "NetworkRuleConfig", "name": "cluster-tcp",
        "portSelector": {"ports": ["1-65535"], "protocol": "tcp"},
        "ingress": [{"subnet": "172.29.21.0/24"}],
    },
    {
        "apiVersion": "v1alpha1", "kind": "NetworkRuleConfig", "name": "cluster-udp",
        "portSelector": {"ports": ["1-65535"], "protocol": "udp"},
        "ingress": [{"subnet": "172.29.21.0/24"}],
    },
    {
        "apiVersion": "v1alpha1", "kind": "NetworkRuleConfig", "name": "dhcp-client",
        "portSelector": {"ports": [68], "protocol": "udp"},
        "ingress": [{"subnet": "0.0.0.0/0"}],
    },
    {
        "apiVersion": "v1alpha1", "kind": "NetworkRuleConfig", "name": "open-tcp-80",
        "portSelector": {"ports": [80], "protocol": "tcp"},
        "ingress": [{"subnet": "0.0.0.0/0"}],
    },
    {
        "apiVersion": "v1alpha1", "kind": "NetworkRuleConfig", "name": "open-tcp-443",
        "portSelector": {"ports": [443], "protocol": "tcp"},
        "ingress": [{"subnet": "0.0.0.0/0"}],
    },
]

# what `talosctl gen config` (a 1.14 client) emits for a v1.13 target: the
# machine document carries grubUseUKICmdline and a HostnameConfig document
# follows; the classic machine.network.hostname arrives via the patches
_GEN_OUTPUT = yaml.safe_dump_all(
    [
        {
            "machine": {
                "network": {"hostname": "rp001"},
                "install": {
                    "disk": "/dev/sda",
                    "image": INSTALLER,
                    "wipe": False,
                    "grubUseUKICmdline": True,
                },
            }
        },
        {"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "stable"},
    ],
    sort_keys=False,
    explicit_start=True,
)

STRIPPED_OUTPUT = [
    {
        "machine": {
            "network": {"hostname": "rp001"},
            "install": {"disk": "/dev/sda", "image": INSTALLER, "wipe": False},
        }
    }
]


def test_metal_patch_stack_matches_golden(make_config, monkeypatch, tmp_path):
    """The csfarm shape: pxe boot link, one [cluster, external] NIC, jumbo L2.
    The machine patch carries the cluster's `tags:` and the provider defaults
    as node labels, the same labels the VM machines' patches carry."""
    stack, _ = _build(
        make_config, monkeypatch, tmp_path,
        tags={"team": "platform"}, default_tags={"ncsa/project": "bbdb"},
    )

    assert stack[:6] == [
        [MACHINE_PATCH],
        [HOSTNAME_FIELD_PATCH],
        FIREWALL_DOCS,
        [KUBESPAN_PATCH],
        NETWORK_DOCS,
        [DEVICES_PATCH],
    ]
    # the external child's return-path pod closes the stack
    (pod_patch,) = stack[6]
    (pod,) = pod_patch["machine"]["pods"]
    assert pod_patch == {"machine": {"pods": [pod]}}
    assert pod["metadata"]["name"] == "taloscluster-metal-return-path"
    script = pod["spec"]["containers"][0]["command"][2]
    assert 'iifname "enp2s0f0.1691" ip daddr 203.0.113.0/24' in script


def test_metal_firewall_admits_the_cluster_l2_and_kubespan(
    make_config, monkeypatch, tmp_path
):
    """A group on another L2: the firewall admits the cluster L2 beside its
    own and opens KubeSpan's UDP/51820 for the VM nodes' WireGuard handshakes."""
    other_l2 = {
        "role": "worker",
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.31.0/24", "gateway": "172.29.31.1"},
        "interfaces": {"enp1s0f0": {"role": "cluster"}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.31.5/24"}}}},
    }
    stack, _ = _build(make_config, monkeypatch, tmp_path, metal=other_l2, external=None)

    firewall = stack[2]
    rules = {d["name"]: d for d in firewall if d["kind"] == "NetworkRuleConfig"}
    for name in ("cluster-tcp", "cluster-udp"):
        assert {"subnet": "172.29.21.0/24"} in rules[name]["ingress"]
        assert {"subnet": "172.29.31.0/24"} in rules[name]["ingress"]
    assert rules["kubespan"]["portSelector"] == {"ports": [51820], "protocol": "udp"}
    assert rules["kubespan"]["ingress"] == [{"subnet": "172.29.21.0/24"}]


def test_metal_control_plane_on_another_l2_matches_golden(
    make_config, monkeypatch, tmp_path
):
    """A control plane whose group sits on another L2: the pod node IP and the
    etcd advertisement are keyed on the group's L2 -- the only one the machine
    owns an address on -- not on the cluster L2, where it has none."""
    other_l2 = {
        "role": "controlplane",
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.31.0/24", "gateway": "172.29.31.1"},
        "interfaces": {"enp1s0f0": {"role": "cluster"}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.31.5/24"}}}},
    }
    stack, _ = _build(make_config, monkeypatch, tmp_path, metal=other_l2, external=None)

    assert stack[0][0] == {
        "machine": {
            "certSANs": [VIP],
            "nodeLabels": {"ncsa/role": "controlplane", "ncsa/pool": "phoenix"},
            "kubelet": {
                "extraArgs": {"rotate-server-certificates": True},
                "nodeIP": {"validSubnets": ["172.29.31.0/24"]},
            },
            "install": {"disk": "/dev/sda", "image": INSTALLER, "wipe": True},
            "time": {"servers": ["ntp.example.com"]},
        }
    }
    assert stack[2][0] == {
        "cluster": {
            "allowSchedulingOnControlPlanes": False,
            "extraManifests": machineconfig.EXTRA_MANIFESTS,
            "apiServer": {"certSANs": [VIP]},
            "etcd": {"advertisedSubnets": ["172.29.31.0/24"]},
        }
    }


TAILSCALE_PATCH = {
    "apiVersion": "v1alpha1",
    "kind": "ExtensionServiceConfig",
    "name": "tailscale",
    "environment": [
        "TS_AUTHKEY=tskey-secret",
        "TS_HOSTNAME=rp001",
        "TS_EXTRA_ARGS=--login-server=https://headscale.example.com",
    ],
}


def test_metal_tailscale_cluster_tells_the_node_to_join_the_tailnet(
    make_config, monkeypatch, tmp_path
):
    """A tailscale cluster's metal node gets the ExtensionServiceConfig the VM
    machines get: its installer bakes the extension, so without the patch the
    node would carry a dormant tailscale service and never join the tailnet."""
    cfg = _cfg(make_config, tailscale={
        "login_server": "https://headscale.example.com",
        "auth_key": "tskey-secret",
    })
    seen: dict = {}

    def fake_gen_config(**kwargs):
        seen["names"] = [Path(p).name for p in kwargs["patches"]]
        seen["patches"] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        return _GEN_OUTPUT

    monkeypatch.setattr(metal_talos.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    metal_talos.build_config(server, cfg, secrets_path, INSTALLER, _endpoint(cfg))

    # the tailscale document rides the shared stack between kubespan and the
    # cabling plan, exactly where build_configs puts it for the VM machines
    assert seen["names"] == [
        "rp001-machine.yaml",
        "rp001-hostname.yaml",
        "rp001-firewall.yaml",
        "rp001-kubespan.yaml",
        "rp001-tailscale.yaml",
        "rp001-network.yaml",
        "rp001-interfaces.yaml",
        "rp001-return-path.yaml",
    ]
    (tailscale,) = next(
        docs for name, docs in zip(seen["names"], seen["patches"], strict=True)
        if name == "rp001-tailscale.yaml"
    )
    assert tailscale == TAILSCALE_PATCH


def test_metal_no_tailscale_patch_without_a_key(make_config, monkeypatch, tmp_path):
    """No pre-auth key: the extension stays idle and no patch is emitted."""
    stack, _ = _build(make_config, monkeypatch, tmp_path)

    assert all(
        doc.get("kind") != "ExtensionServiceConfig"
        for group in stack
        for doc in group
    )


def test_metal_secrets_yaml_tailscale_key_opts_in(
    make_config, monkeypatch, tmp_path
):
    """secrets.yaml is an included file, so its `tailscale:` section opts the
    cluster in: the extension is baked and the patch is emitted, the same rule
    build_configs applies to the VM machines."""
    (tmp_path / "secrets.yaml").write_text(yaml.safe_dump(
        {"tailscale": {"auth_key": "tskey-secret"}}
    ))
    stack, _ = _build(make_config, monkeypatch, tmp_path)

    assert any(
        doc.get("kind") == "ExtensionServiceConfig"
        for group in stack
        for doc in group
    )


def test_metal_config_strips_pre_1_14_output(make_config, monkeypatch, tmp_path):
    """The generated config loses grubUseUKICmdline and the HostnameConfig doc."""
    _, out = _build(make_config, monkeypatch, tmp_path)

    assert list(yaml.safe_load_all(out)) == STRIPPED_OUTPUT


def test_metal_config_on_talos_1_14_uses_the_hostname_document(
    make_config, monkeypatch, tmp_path
):
    """A 1.14 cluster: the HostnameConfig document patch, nothing stripped."""
    passthrough = yaml.safe_dump(HOSTNAME_FIELD_PATCH, sort_keys=False)
    stack, out = _build(
        make_config, monkeypatch, tmp_path,
        talos_version="v1.14.0", kubernetes_version="v1.33.0", output=passthrough,
    )

    assert stack[1] == [HOSTNAME_DOCUMENT_PATCH]
    assert yaml.safe_load(out) == HOSTNAME_FIELD_PATCH


def test_metal_return_path_pod_matches_the_vlan_child(make_config):
    """The marking rule matches the external VLAN child by its stable name."""
    cfg = _cfg(make_config)
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    child = metal_talos._external_child_link(server, cfg)

    pod = metal_talos.return_path_pod(server, cfg, child)

    container = pod["spec"]["containers"][0]
    script = container["command"][2]
    assert child == "enp2s0f0.1691"
    assert pod["metadata"]["name"] == "taloscluster-metal-return-path"
    assert pod["metadata"]["namespace"] == "kube-system"
    assert pod["spec"]["hostNetwork"] is True
    assert container["image"] == f"registry.k8s.io/kube-proxy:{cfg.kubernetes_version}"
    assert container["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["NET_ADMIN"],
    }
    assert 'iifname "enp2s0f0.1691" ip daddr 203.0.113.0/24' in script
    assert "ct direction original ct mark set" in script
    assert "ct direction reply" in script
    assert "0x00002000" in script


def test_metal_config_bakes_the_running_version_when_one_is_passed(
    make_config, monkeypatch, tmp_path
):
    """`metal apply` reads a bootstrapped cluster's running version through
    converge's helper and passes it here: it must reach `talosctl gen config`
    (the kubelet and control-plane images) and the return-path pod's kube-proxy
    image, so a machine joined after a bump never joins newer than the API
    server. Without the override the target is baked, as before."""
    cfg = _cfg(make_config)
    seen: dict = {}

    def fake_gen_config(**kwargs):
        seen["kubernetes_version"] = kwargs["kubernetes_version"]
        seen["patches"] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        return _GEN_OUTPUT

    monkeypatch.setattr(metal_talos.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]

    metal_talos.build_config(
        server, cfg, secrets_path, INSTALLER, _endpoint(cfg),
        kubernetes_version="v1.30.4",
    )

    assert seen["kubernetes_version"] == "v1.30.4"
    pods = [
        pod
        for group in seen["patches"]
        for doc in group
        for pod in (doc.get("machine", {}).get("pods") or [])
    ]
    (pod,) = pods
    assert pod["spec"]["containers"][0]["image"] == "registry.k8s.io/kube-proxy:v1.30.4"

    seen.clear()
    metal_talos.build_config(server, cfg, secrets_path, INSTALLER, _endpoint(cfg))
    assert seen["kubernetes_version"] == cfg.kubernetes_version


def test_metal_interface_overrides_name_and_tag_the_vlan_child(
    make_config, monkeypatch, tmp_path
):
    """`link_name` and `vlan` rename and re-tag the external child link."""
    metal = {
        "role": "worker",
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1", "mtu": 9000},
        "interfaces": {
            "enp1s0f0": {"role": "pxe"},
            "enp2s0f0": {
                "role": ["cluster", "external"],
                "link_name": "ext0",
                "vlan": 1600,
            },
        },
        "servers": {"rp001": {"interfaces": {"enp2s0f0": {"ip": "172.29.21.5/24"}}}},
    }
    stack, _ = _build(make_config, monkeypatch, tmp_path, metal=metal)

    docs, devices = stack[4], stack[5][0]
    assert [d["name"] for d in docs if d["kind"] == "LinkConfig"] == [
        "enp2s0f0", "ext0",
    ]
    assert devices["machine"]["network"]["interfaces"][1]["vlans"] == [{"vlanId": 1600}]


def test_metal_cluster_link_only_carries_no_vlan(make_config):
    """A machine without the external role: no VLAN child, no return-path rule."""
    cfg = _cfg(make_config, metal={
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": {"enp1s0f0": {"role": "cluster"}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.21.5"}}}},
    }, external=None)

    server = cfg.metal.groups["phoenix"].servers["rp001"]
    assert metal_talos.network_docs(server, cfg) == [
        {
            "apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "enp1s0f0",
            "mtu": 9000,
            "addresses": [{"address": "172.29.21.5/24"}],
            "routes": [{"gateway": "172.29.21.1", "mtu": 1500}],
        },
        {
            "apiVersion": "v1alpha1", "kind": "ResolverConfig",
            "nameservers": [{"address": "192.0.2.53"}],
        },
    ]
    assert metal_talos.device_entries(server, cfg) == [
        {"interface": "enp1s0f0", "dhcp": False}
    ]


def test_metal_bare_address_takes_the_l2_prefix(make_config):
    """An address without its prefix length gets the link network's."""
    cfg = _cfg(make_config, metal={
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": {"enp1s0f0": {"role": "cluster"}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.21.5"}}}},
    }, external=None)

    server = cfg.metal.groups["phoenix"].servers["rp001"]
    assert metal_talos.network_docs(server, cfg)[0]["addresses"] == [
        {"address": "172.29.21.5/24"}
    ]


def test_metal_control_plane_states_the_vip_on_its_link(
    make_config, monkeypatch, tmp_path
):
    """A metal control plane holds the kubeapi VIP with a Layer2VIPConfig."""
    metal = {
        "role": "controlplane",
        "disk": "/dev/sda",
        "interfaces": {"enp1s0f0": {"role": "cluster"}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.21.5"}}}},
    }
    stack, _ = _build(make_config, monkeypatch, tmp_path, metal=metal, external=None)

    # a control plane also gets the cluster patch (extraManifests, etcd subnets)
    assert "cluster" in stack[2][0]
    # no external link, so no VLAN child and no return-path pod
    assert all(
        "pods" not in patch.get("machine", {})
        for group in stack
        for patch in ([group] if isinstance(group, dict) else group)
    )
    vip = next(doc for doc in stack[5] if doc.get("kind") == "Layer2VIPConfig")
    assert vip == {
        "apiVersion": "v1alpha1", "kind": "Layer2VIPConfig",
        "name": VIP, "link": "enp1s0f0",
    }


def test_metal_control_plane_with_external_vip_needs_an_external_link(
    make_config, monkeypatch, tmp_path
):
    """A kubeapi_vip on network.external needs an external link to ride: a
    control plane without one could never hold the VIP, so the machine
    configuration refuses to build instead of quietly omitting it."""
    with pytest.raises(ConfigError, match="no external link"):
        _build(
            make_config, monkeypatch, tmp_path,
            metal={
                "role": "controlplane",
                "disk": "/dev/sda",
                "interfaces": {"enp1s0f0": {"role": "cluster"}},
                "servers": {"rp001": {"interfaces": {
                    "enp1s0f0": {"ip": "172.29.21.5"},
                }}},
            },
            external={**EXTERNAL, "kubeapi_vip": "203.0.113.79"},
            vip=None,
        )


# The metal-on-OpenStack shape: the openstack section stays (no VM pool keys,
# no kubeapi_vip anywhere in cluster.yaml), the metal group sits on the tenant
# network the provider creates, and the endpoint is what the network phase
# resolved -- the reserved kubeapi port's fixed ip with the floating ip in
# front of it, exactly what converge passes for the VM machines.
OPENSTACK_TENANT_VIP = "192.168.0.10"
OPENSTACK_FLOATING_IP = "203.0.113.79"


def _openstack_cfg(make_config, role="worker"):
    return make_config({
        "metal": {
            "phoenix": {
                "role": role,
                "disk": "/dev/sda",
                "interfaces": {"enp1s0f0": {"role": "cluster"}},
                "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "192.168.0.5"}}}},
            },
        },
    })


def test_metal_control_plane_without_a_vip_is_refused(make_config, monkeypatch, tmp_path):
    """A control plane with no kubeapi VIP resolved could never hold the API
    address, so the configuration refuses instead of omitting the document."""
    cfg = _openstack_cfg(make_config, role="controlplane")
    rendered = _render(monkeypatch, _GEN_OUTPUT)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    with pytest.raises(ConfigError, match="resolved no kubeapi VIP"):
        metal_talos.build_config(
            server, cfg, secrets_path, INSTALLER,
            Endpoint(vip="", advertised_address=OPENSTACK_FLOATING_IP),
        )
    assert rendered == {}


def test_metal_on_openstack_uses_the_provider_endpoint(
    make_config, monkeypatch, tmp_path
):
    """A metal machine beside OpenStack VMs is generated against the endpoint
    the network phase resolved, like the VM configs: the floating ip advertises
    the endpoint (the certSANs included), and a control plane holds the
    tenant-network VIP on its cluster link."""
    cfg = _openstack_cfg(make_config, role="controlplane")
    seen: dict = {}

    def fake_gen_config(**kwargs):
        seen["endpoint"] = kwargs["endpoint"]
        seen["patches"] = [
            list(yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        return _GEN_OUTPUT

    monkeypatch.setattr(metal_talos.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    metal_talos.build_config(
        server, cfg, secrets_path, INSTALLER,
        Endpoint(vip=OPENSTACK_TENANT_VIP, advertised_address=OPENSTACK_FLOATING_IP),
    )

    # the floating ip is the endpoint and the only certSAN, as for the VMs
    assert seen["endpoint"] == f"https://{OPENSTACK_FLOATING_IP}:6443"
    assert seen["patches"][0] == [{
        "machine": {
            "certSANs": [OPENSTACK_FLOATING_IP],
            "nodeLabels": {"ncsa/role": "controlplane", "ncsa/pool": "phoenix"},
            "kubelet": {
                "extraArgs": {"rotate-server-certificates": True},
                "nodeIP": {"validSubnets": ["192.168.0.0/21"]},
            },
            "install": {"disk": "/dev/sda", "image": INSTALLER, "wipe": True},
            "time": {"servers": ["ntp.example.com"]},
        },
    }]
    assert seen["patches"][2][0]["cluster"]["apiServer"] == {
        "certSANs": [OPENSTACK_FLOATING_IP],
    }
    # the control plane holds the tenant-network VIP on its cluster link
    vip = next(
        doc for group in seen["patches"] for doc in group
        if doc.get("kind") == "Layer2VIPConfig"
    )
    assert vip == {
        "apiVersion": "v1alpha1", "kind": "Layer2VIPConfig",
        "name": OPENSTACK_TENANT_VIP, "link": "enp1s0f0",
    }


@pytest.mark.parametrize(
    ("interfaces", "external", "message"),
    [
        (
            {"enp1s0f0": {"role": "pxe"}},
            EXTERNAL,
            "exactly one interface with the cluster role is required",
        ),
        (
            {
                "enp1s0f0": {"role": "cluster"},
                "enp2s0f0": {"role": "cluster"},
            },
            EXTERNAL,
            "exactly one interface with the cluster role is required",
        ),
        (
            {
                "enp1s0f0": {"role": "cluster"},
                "enp2s0f0": {"role": "external"},
                "enp3s0f0": {"role": "external"},
            },
            EXTERNAL,
            "at most one interface with the external role is supported",
        ),
        (
            {"enp1s0f0": {"role": ["cluster", "external"]}},
            None,
            "has the external role but cluster.yaml has no network.external block",
        ),
    ],
)
def test_metal_cabling_plan_is_checked(make_config, interfaces, external, message):
    """Broken cabling plans refuse to load, so plan never passes a metal
    section the machine configuration could never be generated for."""
    with pytest.raises(ConfigError, match=message):
        _cfg(make_config, metal={
            "role": "worker",
            "disk": "/dev/sda",
            "interfaces": interfaces,
            "servers": {"rp001": {}},
        }, external=external)


def test_metal_external_interface_needs_a_vlan(make_config):
    """An external link without any VLAN id cannot build its child link."""
    cfg = _cfg(make_config, metal={
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": {"enp1s0f0": {"role": ["cluster", "external"]}},
        "servers": {"rp001": {"interfaces": {"enp1s0f0": {"ip": "172.29.21.5"}}}},
    }, external={
        "cidr": "203.0.113.0/24",
        "gateway": "203.0.113.1",
        "anchor_cidr": "169.254.32.0/20",
    })
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    with pytest.raises(Exception, match="needs a VLAN id"):
        metal_talos.network_docs(server, cfg)


def test_metal_installer_drops_the_vm_only_extensions(make_config):
    """qemu-guest-agent talks to a QEMU host over a virtio serial port bare metal
    does not have, so its service never reaches "up" and the machine blocks in
    startAllServices. The metal installer must not carry it."""
    cfg = make_config({})
    resolved = cfg._resolve_extensions({}, metal=True)

    assert "siderolabs/qemu-guest-agent" not in resolved


def test_vm_pools_keep_the_qemu_guest_agent(make_config):
    """The VM half is untouched: OpenStack uses the agent for graceful shutdown
    and guest reporting."""
    cfg = make_config({})

    assert "siderolabs/qemu-guest-agent" in cfg._resolve_extensions({})


def test_metal_still_honours_an_explicitly_requested_extension(make_config):
    """Only the base set is trimmed -- an extension the cluster asks for by name
    is still installed, even a VM-only one somebody has a reason to want."""
    cfg = make_config({"talos": {"extensions": ["siderolabs/qemu-guest-agent"]}})

    assert "siderolabs/qemu-guest-agent" in cfg._resolve_extensions({}, metal=True)


def test_metal_base_extensions_is_the_base_set_without_the_vm_only_ones():
    from taloscluster.naming import BASE_EXTENSIONS, METAL_BASE_EXTENSIONS

    assert "siderolabs/qemu-guest-agent" in BASE_EXTENSIONS
    assert "siderolabs/qemu-guest-agent" not in METAL_BASE_EXTENSIONS
    assert "siderolabs/tailscale" in METAL_BASE_EXTENSIONS
