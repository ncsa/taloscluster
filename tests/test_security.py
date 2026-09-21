"""Tests for taloscluster.openstack.security: desired-rule construction, the
``_rule_key`` normalizer that maps Neutron rule objects to comparable tuples,
and the ``reconcile`` pass against a pre-populated rule set.

``_desired_rules`` and ``_rule_key`` are pure functions over a :class:`Config`
and a rule-like object. ``reconcile`` drives a fake network API carrying a
pre-populated rule set, so no OpenStack connection is needed.
"""

from __future__ import annotations

import types

import pytest

from taloscluster import naming
from taloscluster.openstack.security import SELF, _desired_rules, _rule_key, reconcile
from taloscluster.openstack.session import Inventory
from taloscluster.output import set_dry_run

SG_ID = "sg-123"

# A security block with two talos CIDRs and two kubernetes CIDRs.
SECURITY_OVERRIDES = {
    "security": {
        "talos": {
            "home": "10.0.0.0/24",
            "vpn": "172.16.0.0/16",
        },
        "kubernetes": {
            "office": "192.168.1.0/24",
            "vpn": "172.16.0.0/16",
        },
    }
}


def _fake_rule(**kw) -> types.SimpleNamespace:
    """Build a SimpleNamespace mimicking a Neutron security group rule."""
    defaults = dict(
        direction="ingress",
        ether_type="IPv4",
        ethertype="IPv4",
        protocol="tcp",
        port_range_min=None,
        port_range_max=None,
        remote_ip_prefix=None,
        remote_group_id=None,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _desired_rules
# ---------------------------------------------------------------------------

def test_desired_rules_contains_open_rules(make_config):
    cfg = make_config(SECURITY_OVERRIDES)
    rules = _desired_rules(cfg)
    assert ("icmp", None, None, None, None) in rules
    assert ("tcp", 80, 80, None, None) in rules
    assert ("tcp", 443, 443, None, None) in rules


def test_desired_rules_talos_entries(make_config):
    cfg = make_config(SECURITY_OVERRIDES)
    rules = _desired_rules(cfg)
    # one tcp/50000 rule per security.talos entry, carrying its CIDR
    assert ("tcp", 50000, 50000, "10.0.0.0/24", None) in rules
    assert ("tcp", 50000, 50000, "172.16.0.0/16", None) in rules
    # exactly two 50000 rules
    talos_rules = [k for k in rules if k[1] == 50000]
    assert len(talos_rules) == 2


def test_desired_rules_kubernetes_entries(make_config):
    cfg = make_config(SECURITY_OVERRIDES)
    rules = _desired_rules(cfg)
    # one tcp/6443 rule per security.kubernetes entry, carrying its CIDR
    assert ("tcp", 6443, 6443, "192.168.1.0/24", None) in rules
    assert ("tcp", 6443, 6443, "172.16.0.0/16", None) in rules
    kube_rules = [k for k in rules if k[1] == 6443]
    assert len(kube_rules) == 2


def test_desired_rules_intra_sg_self_rules(make_config):
    cfg = make_config(SECURITY_OVERRIDES)
    rules = _desired_rules(cfg)
    assert ("tcp", None, None, None, SELF) in rules
    assert ("udp", None, None, None, SELF) in rules


def test_desired_rules_zero_cidr_host_normalizes_to_open(make_config):
    # a host whose cidr is 0.0.0.0/0 must normalize to None, mirroring
    # _rule_key, so it matches the existing null-form rule instead of being
    # re-created (which would 409) on the next run.
    cfg = make_config({"security": {"talos": {"any": "0.0.0.0/0"}}})
    rules = _desired_rules(cfg)
    assert ("tcp", 50000, 50000, None, None) in rules
    assert ("tcp", 50000, 50000, "0.0.0.0/0", None) not in rules


# ---------------------------------------------------------------------------
# _rule_key normalizer
# ---------------------------------------------------------------------------

def test_rule_key_egress_returns_none():
    r = _fake_rule(direction="egress")
    assert _rule_key(r, SG_ID) is None


def test_rule_key_ipv6_returns_none():
    r = _fake_rule(ethertype="IPv6", ether_type="IPv6")
    assert _rule_key(r, SG_ID) is None


def test_rule_key_ipv6_via_ether_type_only_returns_none():
    # some SDKs expose ether_type instead of ethertype
    r = _fake_rule(ether_type="IPv6")
    r.__dict__.pop("ethertype", None)
    assert _rule_key(r, SG_ID) is None


def test_rule_key_zero_cidr_normalizes_to_none():
    r = _fake_rule(protocol="tcp", port_range_min=80, port_range_max=80,
                   remote_ip_prefix="0.0.0.0/0")
    key = _rule_key(r, SG_ID)
    # 0.0.0.0/0 normalizes to None -> matches the open http rule
    assert key == ("tcp", 80, 80, None, None)


def test_rule_key_remote_group_id_maps_to_self_sentinel():
    r = _fake_rule(protocol="tcp", remote_group_id=SG_ID)
    key = _rule_key(r, SG_ID)
    assert key == ("tcp", None, None, None, SELF)


def test_rule_key_remote_group_id_unrelated_stays_distinct():
    # A foreign remote group must not collapse to open-to-all (None): that
    # would mask the real open rule on the same port and never delete the
    # foreign rule. It stays its own id so it matches neither open nor SELF.
    r = _fake_rule(protocol="tcp", remote_group_id="other-sg")
    key = _rule_key(r, SG_ID)
    assert key == ("tcp", None, None, None, "other-sg")
    assert key != ("tcp", None, None, None, None)
    assert key != ("tcp", None, None, None, SELF)


def test_rule_key_roundtrips_into_desired_rules(make_config):
    """A fake rule built to match each desired tuple normalizes back into the set."""
    cfg = make_config(SECURITY_OVERRIDES)
    desired = _desired_rules(cfg)

    # http open rule (with 0.0.0.0/0 that normalizes to None)
    http_rule = _fake_rule(protocol="tcp", port_range_min=80, port_range_max=80,
                           remote_ip_prefix="0.0.0.0/0")
    assert _rule_key(http_rule, SG_ID) in desired

    # a talos CIDR rule
    talos_rule = _fake_rule(protocol="tcp", port_range_min=50000, port_range_max=50000,
                            remote_ip_prefix="10.0.0.0/24")
    assert _rule_key(talos_rule, SG_ID) in desired

    # intra-sg tcp rule
    self_rule = _fake_rule(protocol="tcp", remote_group_id=SG_ID)
    assert _rule_key(self_rule, SG_ID) in desired

    # intra-sg udp rule
    self_udp = _fake_rule(protocol="udp", remote_group_id=SG_ID)
    assert _rule_key(self_udp, SG_ID) in desired


def test_rule_key_extra_rule_not_in_desired(make_config):
    """A rule for a port we don't want normalizes to a tuple NOT in desired."""
    cfg = make_config(SECURITY_OVERRIDES)
    desired = _desired_rules(cfg)
    extra = _fake_rule(protocol="tcp", port_range_min=22, port_range_max=22,
                       remote_ip_prefix="0.0.0.0/0")
    assert _rule_key(extra, SG_ID) not in desired


# ---------------------------------------------------------------------------
# named rules (Stage 4 schema)
# ---------------------------------------------------------------------------

def test_desired_rules_named_port_rule(make_config):
    cfg = make_config({"security": {
        "metrics": {"port": 9100, "hosts": {"vpn": "172.16.0.0/16"}},
    }})
    rules = _desired_rules(cfg)
    assert rules[("tcp", 9100, 9100, "172.16.0.0/16", None)] == "metrics from vpn"


def test_desired_rules_http_block_replaces_the_open_rule(make_config):
    cfg = make_config({"security": {"http": {"hosts": {"office": "203.0.113.0/24"}}}})
    rules = _desired_rules(cfg)
    assert ("tcp", 80, 80, None, None) not in rules
    assert ("tcp", 80, 80, "203.0.113.0/24", None) in rules
    # https is untouched and stays open
    assert ("tcp", 443, 443, None, None) in rules


def test_desired_rules_https_block_replaces_the_open_rule(make_config):
    cfg = make_config({"security": {"https": {"hosts": {"office": "203.0.113.0/24"}}}})
    rules = _desired_rules(cfg)
    assert ("tcp", 443, 443, None, None) not in rules
    assert ("tcp", 443, 443, "203.0.113.0/24", None) in rules
    assert ("tcp", 80, 80, None, None) in rules


def test_desired_rules_empty_http_hosts_closes_port_80(make_config):
    cfg = make_config({"security": {"http": {"hosts": {}}}})
    rules = _desired_rules(cfg)
    assert not [key for key in rules if key[1] == 80]


def test_desired_rules_updating_a_host_replaces_its_rule(make_config):
    before = _desired_rules(make_config({"security": {"talos": {"vpn": "10.0.0.0/24"}}}))
    after = _desired_rules(make_config({"security": {"talos": {"vpn": "10.1.0.0/24"}}}))
    assert ("tcp", 50000, 50000, "10.0.0.0/24", None) in before
    assert ("tcp", 50000, 50000, "10.0.0.0/24", None) not in after
    assert ("tcp", 50000, 50000, "10.1.0.0/24", None) in after


# ---------------------------------------------------------------------------
# metal groups on another L2
# ---------------------------------------------------------------------------

METAL = {"metal": {
    "rack": {
        "role": "worker",
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.22.0/24", "gateway": "172.29.22.1"},
        "servers": {"rp001": {}},
    },
}}


def test_desired_rules_admit_a_metal_group_on_another_l2(make_config):
    """The group's nodes sit outside the SG, so they are admitted by CIDR:
    tcp+udp for apid, kubelet and etcd, plus the KubeSpan WireGuard port."""
    cfg = make_config({"talos": {"kubespan": True}, **METAL})
    rules = _desired_rules(cfg)

    assert ("tcp", None, None, "172.29.22.0/24", None) in rules
    assert ("udp", None, None, "172.29.22.0/24", None) in rules
    assert ("udp", 51820, 51820, "172.29.22.0/24", None) in rules
    # the intra-SG rules stay as they are
    assert ("tcp", None, None, None, SELF) in rules
    assert ("udp", None, None, None, SELF) in rules


def test_desired_rules_no_metal_cidr_rules_without_another_l2(make_config):
    """A single-L2 cluster gets no CIDR-scoped all-port or KubeSpan rules, so
    the next converge of an existing security group reconciles to no change."""
    cfg = make_config()
    rules = _desired_rules(cfg)

    assert not [key for key in rules if key[1] is None and key[3] is not None]
    assert not [key for key in rules if key[1] == 51820]


# ---------------------------------------------------------------------------
# reconcile against a pre-populated rule set
# ---------------------------------------------------------------------------

class _FakeNetwork:
    """A minimal Neutron network API that stores rules and records mutations."""

    def __init__(self, rules: list):
        self._store = list(rules)
        self._seq = 1
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def security_group_rules(self, **kw) -> list:
        return list(self._store)

    def create_security_group_rule(self, **kwargs) -> None:
        self.created.append(kwargs)
        self._store.append(_fake_rule(
            id=f"r{self._seq}",
            protocol=kwargs.get("protocol"),
            port_range_min=kwargs.get("port_range_min"),
            port_range_max=kwargs.get("port_range_max"),
            remote_ip_prefix=kwargs.get("remote_ip_prefix"),
            remote_group_id=kwargs.get("remote_group_id"),
        ))
        self._seq += 1

    def delete_security_group_rule(self, rule_id) -> None:
        self.deleted.append(rule_id)
        self._store[:] = [r for r in self._store if r.id != rule_id]


def _reconcile(net, cfg, sg_id: str = SG_ID, cluster: str = "testcluster"):
    """Run reconcile with the SG pre-populated in an Inventory over a fake net."""
    conn = types.SimpleNamespace(network=net)
    inv = Inventory(conn, cluster)
    inv.put("security_groups",
            types.SimpleNamespace(id=sg_id, name=naming.secgroup_name(cluster)))
    reconcile(conn, cfg, inv)


@pytest.fixture(autouse=True)
def _no_dry_run():
    set_dry_run(False)
    yield
    set_dry_run(False)


def test_reconcile_creates_open_rule_and_removes_foreign_group_rule(make_config):
    """An unrelated remote_group_id rule must not mask the real open rule: the
    open rule is created and the foreign rule deleted, not left in place."""
    cfg = make_config(SECURITY_OVERRIDES)
    net = _FakeNetwork([
        _fake_rule(id="r1", protocol="tcp", port_range_min=80, port_range_max=80,
                   remote_group_id="other-sg"),
    ])
    _reconcile(net, cfg)
    # the real open-to-all tcp/80 rule is created (previously never created)
    assert any(
        c.get("direction") == "ingress" and c.get("protocol") == "tcp"
        and c.get("port_range_min") == 80 and c.get("port_range_max") == 80
        and "remote_ip_prefix" not in c and "remote_group_id" not in c
        for c in net.created
    )
    # the foreign remote-group rule is removed (previously never removed)
    assert net.deleted == ["r1"]


def test_reconcile_zero_cidr_host_is_idempotent_across_runs(make_config):
    """A host cidr of 0.0.0.0/0 normalizes to the null-form rule, so a second
    reconcile over the materialized rule set makes no changes (no 409)."""
    cfg = make_config({"security": {"talos": {"any": "0.0.0.0/0"}}})
    net = _FakeNetwork([_fake_rule(id="r0", protocol="tcp", port_range_min=50000,
                                   port_range_max=50000)])
    _reconcile(net, cfg)
    assert net.created, "first run should create the missing rules"
    # run again against the resulting rule set: nothing left to do
    net2 = _FakeNetwork(net._store)
    _reconcile(net2, cfg)
    assert net2.created == []
    assert net2.deleted == []
