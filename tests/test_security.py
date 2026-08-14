"""Tests for clusterctl.openstack.security: desired-rule construction and the
``_rule_key`` normalizer that maps Neutron rule objects to comparable tuples.

No OpenStack connection is needed -- ``_desired_rules`` and ``_rule_key`` are
pure functions over a :class:`Config` and a rule-like object.
"""

from __future__ import annotations

import types

from clusterctl.openstack.security import SELF, _desired_rules, _rule_key

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


def test_rule_key_remote_group_id_unrelated_is_none():
    r = _fake_rule(protocol="tcp", remote_group_id="other-sg")
    key = _rule_key(r, SG_ID)
    assert key == ("tcp", None, None, None, None)


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
