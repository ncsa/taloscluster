"""Golden test: the metal patch stack and the Talos < 1.14 handling.

The metal machine config is generated through the same ``talosctl gen config``
pipeline as the VM providers, so this pins every document handed to it for a
bare-metal worker whose cabling plan is a PXE boot link plus one NIC carrying
both the cluster and the external role -- the csfarm shape. On a jumbo group
L2 the cluster link states its MTU and clamps the default route to 1500, and
the external VLAN child states its own (default) MTU so it never inherits the
parent's. The KubeSpan patch rides along with its endpoint filters, and the
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

from taloscluster.metal import talos as metal_talos

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


def _cfg(make_config, *, metal=None, external=EXTERNAL, talos_version=None):
    network: dict = {
        "cluster": {
            "cidr": "172.29.21.0/24", "gateway": "172.29.21.1", "kubeapi_vip": VIP,
        },
        "dns": ["192.0.2.53"],
    }
    if external is not None:
        network["external"] = external
    overrides: dict = {
        "controlplane": {"count": 1, "disk": 40},
        "network": network,
        "metal": {"phoenix": PHOENIX if metal is None else metal},
    }
    if talos_version is not None:
        overrides["talos"] = {"version": talos_version}
    return make_config(overrides, remove=("openstack",))


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
    make_config, monkeypatch, tmp_path, output=None, **kwargs
) -> tuple[dict[str, list], str]:
    cfg = _cfg(make_config, **kwargs)
    rendered = _render(monkeypatch, output or _GEN_OUTPUT)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    out = metal_talos.build_config(server, cfg, secrets_path, INSTALLER)
    assert len(rendered) == 1
    return rendered["rp001"], out


MACHINE_PATCH = {
    "machine": {
        "certSANs": [VIP],
        "nodeLabels": {"ncsa/role": "worker", "ncsa/pool": "phoenix"},
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
        "filters": {"endpoints": ["169.254.32.0/20", "203.0.113.0/24"]},
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
    """The csfarm shape: pxe boot link, one [cluster, external] NIC, jumbo L2."""
    stack, _ = _build(make_config, monkeypatch, tmp_path)

    assert stack[:5] == [
        [MACHINE_PATCH],
        [HOSTNAME_FIELD_PATCH],
        [KUBESPAN_PATCH],
        NETWORK_DOCS,
        [DEVICES_PATCH],
    ]
    # the external child's return-path pod closes the stack
    (pod_patch,) = stack[5]
    (pod,) = pod_patch["machine"]["pods"]
    assert pod_patch == {"machine": {"pods": [pod]}}
    assert pod["metadata"]["name"] == "taloscluster-metal-return-path"
    script = pod["spec"]["containers"][0]["command"][2]
    assert 'iifname "enp2s0f0.1691" ip daddr 203.0.113.0/24' in script


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
        talos_version="v1.14.0", output=passthrough,
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

    docs, devices = stack[3], stack[4][0]
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
            "addresses": [{"address": "172.29.21.5/24"}],
            "routes": [{"gateway": "172.29.21.1"}],
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
    vip = next(doc for doc in stack[4] if doc["kind"] == "Layer2VIPConfig")
    assert vip == {
        "apiVersion": "v1alpha1", "kind": "Layer2VIPConfig",
        "name": VIP, "link": "enp1s0f0",
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
    """Broken cabling plans refuse to generate a configuration."""
    cfg = _cfg(make_config, metal={
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": interfaces,
        "servers": {"rp001": {}},
    }, external=external)
    server = cfg.metal.groups["phoenix"].servers["rp001"]
    with pytest.raises(Exception, match=message):
        metal_talos.network_docs(server, cfg)


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
