"""Tests for taloscluster.openstack.security: desired-rule construction, the
``_rule_key`` normalizer that maps Neutron rule objects to comparable tuples,
the egress complement around the metadata service, and the ``reconcile`` pass
against a pre-populated rule set.

``_desired_rules`` and ``_rule_key`` are pure functions over a :class:`Config`
and a rule-like object. ``reconcile`` drives a fake network API carrying a
pre-populated rule set, so no OpenStack connection is needed.
"""

from __future__ import annotations

import ipaddress
import types

import pytest

from taloscluster import naming
from taloscluster.openstack.security import (
    METADATA_V4,
    METADATA_V6,
    SELF,
    _desired_egress_rules,
    _desired_rules,
    _egress_key,
    _rule_key,
    reconcile,
)
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


def _default_egress_rule(ether: str, id: str) -> types.SimpleNamespace:
    """The allow-all egress rule Neutron seeds into every security group."""
    return _fake_rule(id=id, direction="egress", ether_type=ether,
                      ethertype=ether, protocol=None)


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
        "network": {"cidr": "192.168.16.0/24", "gateway": "192.168.16.1"},
        "servers": {
            "srv01": {"interfaces": {"enp1s0f0": {"role": "cluster", "ip": "192.168.16.5/24"}}},
        },
    },
}}


def test_desired_rules_admit_a_metal_group_on_another_l2(make_config):
    """The group's nodes sit outside the SG, so they are admitted by CIDR:
    tcp+udp for apid, kubelet and etcd, plus the KubeSpan WireGuard port."""
    cfg = make_config({"talos": {"kubespan": True}, **METAL})
    rules = _desired_rules(cfg)

    assert ("tcp", None, None, "192.168.16.0/24", None) in rules
    assert ("udp", None, None, "192.168.16.0/24", None) in rules
    assert ("udp", 51820, 51820, "192.168.16.0/24", None) in rules
    # the intra-SG rules stay as they are
    assert ("tcp", None, None, None, SELF) in rules
    assert ("udp", None, None, None, SELF) in rules


def test_desired_rules_admit_a_server_that_replaces_its_groups_l2(make_config):
    """A server overriding its group's network is admitted by its own CIDR,
    beside the group's."""
    rack = METAL["metal"]["rack"]
    cfg = make_config({"talos": {"kubespan": True}, "metal": {"rack": {
        **rack,
        "servers": {
            **rack["servers"],
            "srv02": {
                "network": {"cidr": "192.168.17.0/24", "gateway": "192.168.17.1"},
                "interfaces": {"enp1s0f0": {"role": "cluster", "ip": "192.168.17.5/24"}},
            },
        },
    }}})
    rules = _desired_rules(cfg)

    assert ("tcp", None, None, "192.168.17.0/24", None) in rules
    assert ("udp", None, None, "192.168.17.0/24", None) in rules
    assert ("udp", 51820, 51820, "192.168.17.0/24", None) in rules


def test_desired_rules_no_metal_cidr_rules_without_another_l2(make_config):
    """A single-L2 cluster gets no CIDR-scoped all-port or KubeSpan rules, so
    the next converge of an existing security group reconciles to no change."""
    cfg = make_config()
    rules = _desired_rules(cfg)

    assert not [key for key in rules if key[1] is None and key[3] is not None]
    assert not [key for key in rules if key[1] == 51820]


# ---------------------------------------------------------------------------
# desired egress rules (the metadata-service block)
# ---------------------------------------------------------------------------

def test_desired_egress_rules_exclude_the_metadata_address(make_config):
    rules = _desired_egress_rules(make_config())
    assert len(rules) == 42  # 32 IPv4 blocks around a /32, 10 IPv6 around a /10
    for (ether, proto, pmin, pmax, remote_ip, group) in rules:
        assert proto is None and pmin is None and pmax is None and group is None
        assert ether in ("IPv4", "IPv6") and remote_ip is not None
    assert not any(METADATA_V4 in k for k in rules)
    assert not any(METADATA_V6 in k for k in rules)
    # the default allow-all egress rules must never be a desired key
    assert ("IPv4", None, None, None, None, None) not in rules
    assert ("IPv6", None, None, None, None, None) not in rules


def test_ipv4_egress_complement_covers_everything_but_the_metadata_address(make_config):
    """Collapsing the 32 blocks with the metadata /32 back together must yield
    exactly 0.0.0.0/0, and no block may contain the metadata address."""
    blocks = [ipaddress.ip_network(k[4]) for k in _desired_egress_rules(make_config())
              if k[0] == "IPv4"]
    assert len(blocks) == 32
    metadata = ipaddress.ip_network(METADATA_V4)
    assert all(metadata not in b for b in blocks)
    assert list(ipaddress.collapse_addresses([*blocks, metadata])) == [
        ipaddress.ip_network("0.0.0.0/0")
    ]


def test_ipv6_egress_complement_covers_everything_but_link_local(make_config):
    """The same complement around fe80::/10, so the link-local metadata
    address Neutron also answers on stays denied."""
    blocks = [ipaddress.ip_network(k[4]) for k in _desired_egress_rules(make_config())
              if k[0] == "IPv6"]
    assert len(blocks) == 10
    link_local = ipaddress.ip_network(METADATA_V6)
    assert all(link_local not in b for b in blocks)
    # fe80::a9fe:a9fe is the link-local metadata form
    assert all(ipaddress.ip_address("fe80::a9fe:a9fe") not in b for b in blocks)
    assert list(ipaddress.collapse_addresses([*blocks, link_local])) == [
        ipaddress.ip_network("::/0")
    ]


def test_egress_key_normalizes_an_egress_rule(make_config):
    rules = _desired_egress_rules(make_config())
    (ether, proto, pmin, pmax, cidr, _) = next(iter(rules))
    r = _fake_rule(direction="egress", ethertype=ether, ether_type=ether,
                   protocol=proto, port_range_min=pmin, port_range_max=pmax,
                   remote_ip_prefix=cidr)
    assert _egress_key(r, SG_ID) in rules


def test_egress_key_returns_none_for_ingress_rules():
    assert _egress_key(_fake_rule(direction="ingress"), SG_ID) is None


def test_egress_key_materialized_wildcard_stays_an_extra():
    """A re-materialized default allow-all rule (0.0.0.0/0 spelled out) still
    normalizes to the null-form extra, never to a desired CIDR."""
    r = _fake_rule(direction="egress", protocol=None,
                   remote_ip_prefix="0.0.0.0/0")
    assert _egress_key(r, SG_ID) == ("IPv4", None, None, None, None, None)


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
            direction=kwargs.get("direction", "ingress"),
            ethertype=kwargs.get("ethertype", "IPv4"),
            ether_type=kwargs.get("ethertype", "IPv4"),
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


class _NeutronNetwork(_FakeNetwork):
    """A fake that canonicalizes remote_ip_prefix on storage the way Neutron
    does: a bare address is kept with the implicit /32 (or /128) prefix."""

    def create_security_group_rule(self, **kwargs) -> None:
        prefix = kwargs.get("remote_ip_prefix")
        if prefix:
            kwargs["remote_ip_prefix"] = str(ipaddress.ip_network(prefix))
        super().create_security_group_rule(**kwargs)


def test_reconcile_bare_ip_host_is_idempotent_across_runs(make_config):
    """A host given as a bare IP is desired as the /32 Neutron stores it as, so
    a second reconcile over the normalized rule set makes no changes (no 409
    SecurityGroupRuleExists) instead of deleting and re-creating the rule."""
    cfg = make_config({"security": {"talos": {"vpn": "198.51.100.7"}}})
    net = _NeutronNetwork([])
    _reconcile(net, cfg)
    assert any(c.get("remote_ip_prefix") == "198.51.100.7/32" for c in net.created)
    # run again against the Neutron-normalized rule set: nothing left to do
    net2 = _NeutronNetwork(net._store)
    _reconcile(net2, cfg)
    assert net2.created == []
    assert net2.deleted == []


def test_reconcile_removes_the_default_egress_rules_and_installs_the_block(make_config):
    """Neutron seeds two allow-all egress rules; reconcile deletes both and
    creates the 42 CIDR rules around the metadata address instead."""
    cfg = make_config()
    net = _FakeNetwork([
        _default_egress_rule("IPv4", "d1"),
        _default_egress_rule("IPv6", "d2"),
    ])
    _reconcile(net, cfg)
    assert sorted(net.deleted) == ["d1", "d2"]
    egress = [c for c in net.created if c.get("direction") == "egress"]
    assert len(egress) == 42
    assert len([c for c in egress if c["ethertype"] == "IPv4"]) == 32
    assert len([c for c in egress if c["ethertype"] == "IPv6"]) == 10
    for c in egress:
        assert c["protocol"] is None
        assert c["remote_ip_prefix"] not in (None, METADATA_V4, METADATA_V6)
    # the ingress diff is unchanged: every created ingress rule names a protocol
    assert not any(c.get("direction") == "ingress" and c.get("protocol") is None
                   for c in net.created)


def test_reconcile_egress_is_idempotent_across_runs(make_config):
    """A second reconcile over the converged rule set makes no changes: the
    created egress rules normalize back into the desired set."""
    cfg = make_config()
    net = _FakeNetwork([
        _default_egress_rule("IPv4", "d1"),
        _default_egress_rule("IPv6", "d2"),
    ])
    _reconcile(net, cfg)
    net2 = _FakeNetwork(net._store)
    _reconcile(net2, cfg)
    assert net2.created == []
    assert net2.deleted == []


def test_reconcile_picks_the_block_up_on_an_existing_cluster(make_config):
    """An existing cluster's SG (ingress-only, defaults still seeded) gets the
    egress block on its next converge without disturbing converged ingress
    rules."""
    cfg = make_config(SECURITY_OVERRIDES)
    net = _FakeNetwork([
        _default_egress_rule("IPv4", "d1"),
        _default_egress_rule("IPv6", "d2"),
        _fake_rule(id="i1", protocol="tcp", port_range_min=50000,
                   port_range_max=50000, remote_ip_prefix="10.0.0.0/24"),
    ])
    _reconcile(net, cfg)
    assert sorted(net.deleted) == ["d1", "d2"]
    # the converged talos rule is neither duplicated nor re-created
    assert not any(
        c.get("direction") == "ingress"
        and c.get("remote_ip_prefix") == "10.0.0.0/24"
        for c in net.created
    )


def test_metadata_true_keeps_the_default_egress_rules(make_config, capsys):
    """`openstack.metadata: true` leaves Neutron's allow-all pair in place and
    creates no egress rules, with a warning that the address is reachable."""
    cfg = make_config({"openstack": {"metadata": True}})
    net = _FakeNetwork([
        _default_egress_rule("IPv4", "d1"),
        _default_egress_rule("IPv6", "d2"),
    ])
    _reconcile(net, cfg)
    assert net.deleted == []
    assert not any(c.get("direction") == "egress" for c in net.created)
    assert "openstack.metadata is true" in capsys.readouterr().err


def test_metadata_true_restores_allow_all_on_a_blocked_group(make_config):
    """Switching to `metadata: true` on a group that carries the block deletes
    the 42 CIDR rules and puts the allow-all pair back."""
    blocked = _FakeNetwork([
        _default_egress_rule("IPv4", "d1"),
        _default_egress_rule("IPv6", "d2"),
    ])
    _reconcile(blocked, make_config())
    net = _FakeNetwork(blocked._store)
    _reconcile(net, make_config({"openstack": {"metadata": True}}))
    assert len(net.deleted) == 42
    egress = [c for c in net.created if c.get("direction") == "egress"]
    assert sorted(c["ethertype"] for c in egress) == ["IPv4", "IPv6"]
    assert all(c.get("remote_ip_prefix") is None and c["protocol"] is None for c in egress)


def test_metadata_must_be_a_boolean(make_config):
    from taloscluster.errors import ConfigError

    with pytest.raises(ConfigError, match="openstack.metadata must be true or false"):
        make_config({"openstack": {"metadata": "yes"}})
