"""The security allowlists are rendered as a Talos ingress firewall on every node.

Metal nodes get the same firewall, keyed on their group's own L2: the
intra-cluster rules admit every L2 the cluster's nodes sit on, and KubeSpan's
WireGuard port is opened from the L2s a node does not sit on itself.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from taloscluster.infrastructure import Endpoint, TalosContribution
from taloscluster.talos import machineconfig

SECURITY = {
    "kubernetes": {"tailscale": "100.64.0.0/10", "office vpn": "203.0.113.0/24"},
    "talos": {"tailscale": "100.64.0.0/10"},
    "https": {"hosts": {"office vpn": "203.0.113.0/24"}},
    "metrics": {"port": 9100, "hosts": {}},
}


def _rules(docs: list[dict]) -> dict[str, dict]:
    return {d["name"]: d for d in docs if d["kind"] == "NetworkRuleConfig"}


def test_firewall_documents_mirror_the_security_rules(make_config):
    cfg = make_config({
        "security": SECURITY,
        "tailscale": {"login_server": "https://headscale.example.com"},
    })
    docs = machineconfig._firewall_docs(cfg)

    assert docs[0] == {
        "apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block",
    }
    rules = _rules(docs)
    assert rules["cluster-tcp"]["portSelector"] == {"ports": ["1-65535"], "protocol": "tcp"}
    assert rules["cluster-tcp"]["ingress"] == [{"subnet": cfg.network.cluster.cidr}]
    assert rules["cluster-udp"]["ingress"] == [{"subnet": cfg.network.cluster.cidr}]
    assert rules["dhcp-client"]["portSelector"] == {"ports": [68], "protocol": "udp"}
    assert rules["tailscale"]["portSelector"] == {"ports": [41641], "protocol": "udp"}
    # http stays open because nothing claims 80; https is claimed and restricted
    assert rules["open-tcp-80"]["ingress"] == [{"subnet": "0.0.0.0/0"}]
    assert "open-tcp-443" not in rules
    assert rules["https"]["portSelector"] == {"ports": [443], "protocol": "tcp"}
    assert rules["https"]["ingress"] == [{"subnet": "203.0.113.0/24"}]
    assert rules["kubernetes"]["portSelector"] == {"ports": [6443], "protocol": "tcp"}
    assert {s["subnet"] for s in rules["kubernetes"]["ingress"]} >= {
        "100.64.0.0/10", "203.0.113.0/24",
    }
    assert rules["talos"]["portSelector"] == {"ports": [50000], "protocol": "tcp"}
    # a rule without hosts closes its port: block already does that, no document
    assert "metrics" not in rules


def test_no_tailscale_rule_without_a_tailscale_section(make_config):
    cfg = make_config(remove=("tailscale",))
    assert "tailscale" not in _rules(machineconfig._firewall_docs(cfg))


METAL = {
    "rack": {
        "role": "worker",
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.22.0/24", "gateway": "172.29.22.1"},
        "servers": {"rp001": {}},
    },
}


def test_firewall_documents_admit_a_metal_group_on_another_l2(make_config):
    """A metal group's L2 joins the intra-cluster allow-all rules, and the
    KubeSpan WireGuard port is opened from that L2 explicitly."""
    cfg = make_config({"metal": METAL})
    rules = _rules(machineconfig._firewall_docs(cfg))

    for name in ("cluster-tcp", "cluster-udp"):
        assert {"subnet": "172.29.22.0/24"} in rules[name]["ingress"]
    assert rules["kubespan"]["portSelector"] == {"ports": [51820], "protocol": "udp"}
    assert rules["kubespan"]["ingress"] == [{"subnet": "172.29.22.0/24"}]


def test_metal_node_firewall_is_keyed_on_its_own_l2(make_config):
    """A metal node's own stack, keyed on its group L2, still admits the
    cluster L2: apid, kubelet and etcd arrive from the VM nodes' addresses."""
    cfg = make_config({"metal": METAL})
    rules = _rules(machineconfig._firewall_docs(cfg, node_cidr="172.29.22.0/24"))

    for name in ("cluster-tcp", "cluster-udp"):
        assert {"subnet": cfg.network.cluster.cidr} in rules[name]["ingress"]
        assert {"subnet": "172.29.22.0/24"} in rules[name]["ingress"]
    # the group L2's own UDP already rides the allow-all rule; the KubeSpan
    # port is opened for the peers off it
    assert rules["kubespan"]["ingress"] == [{"subnet": cfg.network.cluster.cidr}]


def test_no_kubespan_rule_without_another_l2(make_config):
    """A single-L2 cluster gets no KubeSpan document, with or without a metal
    group on the cluster network itself."""
    plain = _rules(machineconfig._firewall_docs(make_config()))
    assert "kubespan" not in plain

    same_l2 = make_config({"metal": {
        "rack": {
            "role": "worker",
            "disk": "/dev/sda",
            "servers": {"rp001": {}},
        },
    }})
    rules = _rules(machineconfig._firewall_docs(same_l2))
    assert "kubespan" not in rules
    assert rules["cluster-tcp"]["ingress"] == [{"subnet": same_l2.network.cluster.cidr}]


def test_build_configs_stacks_the_firewall_patch_on_every_node(
    make_config, monkeypatch, tmp_path
):
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 40}},
    })
    stacks: dict[str, list[str]] = {}

    def fake_gen_config(**kwargs):
        host = Path(kwargs["patches"][0]).name.removesuffix("-machine.yaml")
        stacks[host] = [Path(p).name for p in kwargs["patches"]]
        for p in kwargs["patches"]:
            if p.name.endswith("-firewall.yaml"):
                kinds = [d["kind"] for d in yaml.safe_load_all(Path(p).read_text())]
                assert kinds[0] == "NetworkDefaultActionConfig"
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    contributions = {h: TalosContribution(install_disk="/dev/vda") for h in cfg.machines}
    machineconfig.build_configs(
        cfg, cfg.machines,
        Endpoint(vip="192.0.2.10", advertised_address="203.0.113.10"), tmp_path / "s",
        {m.extensions: "installer" for m in cfg.machines.values()}, contributions,
    )
    assert len(stacks) == 2
    for host, names in stacks.items():
        assert f"{host}-firewall.yaml" in names
        if host.endswith("controlplane-01"):
            # after the shared cluster patch, before provider and user patches
            assert names.index(f"{host}-cluster.yaml") < names.index(f"{host}-firewall.yaml")
