"""Keep the MTU documentation in lockstep with the behavior it documents.

The MTU section of the network configuration page carries the operational
rules a jumbo layer-2 network imposes and the tool cannot check for you:
every host on the wire must agree on the MTU because nothing discovers it
inside a subnet, changing it is a whole-cluster event, and the gateway's MTU
is outside the tool's view. These tests pin the section, the
``ping -M do -s 8972`` recipe and the numbers against the code so the page
cannot drift from what the machine configs actually state.
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.config import DEFAULT_MTU

ROOT = Path(__file__).resolve().parent.parent
NETWORK = ROOT / "docs" / "configuration" / "network.md"
MACHINES = ROOT / "docs" / "concepts" / "machines.md"
PROVIDER = ROOT / "docs" / "providers" / "proxmox.md"

JUMBO = 9000  # the jumbo MTU example the network page documents
PING_PAYLOAD = 8972  # JUMBO minus the 28 bytes of IP and ICMP headers


def test_network_page_states_the_mtu_rules():
    text = NETWORK.read_text()
    assert "## MTU" in text
    # no PMTUD inside a subnet, so every host on the wire must agree
    assert "no path MTU discovery inside a subnet" in text
    assert "carry the same MTU" in text
    # a live change moves every node, not a rolling subset
    assert "whole-cluster event" in text
    # the tool warns on bridges but cannot see the gateway
    assert "outside its view" in text


def test_ping_recipe_matches_the_documented_jumbo_example():
    text = NETWORK.read_text()
    assert f"ping -M do -s {PING_PAYLOAD}" in text
    # the payload plus the IP and ICMP headers is exactly the jumbo packet
    assert PING_PAYLOAD + 28 == JUMBO
    assert str(JUMBO) in text


def test_documented_route_clamp_matches_the_code():
    # the docs state the default route is clamped to 1500; that is DEFAULT_MTU,
    # the value the machine config restates the DHCP-learned route with
    assert DEFAULT_MTU == 1500
    assert f"clamped to {DEFAULT_MTU}" in NETWORK.read_text()


def test_machines_page_carries_the_link_mtu_summary():
    text = MACHINES.read_text()
    assert f"clamped to {DEFAULT_MTU}" in text
    assert "../configuration/network.md#mtu" in text


def test_network_page_defers_the_nic_mtu_strip_to_a_restart():
    # Regression: the network page used to say converge strips a VM NIC's
    # explicit MTU "live"; Proxmox re-plugs a running VM's NIC, which cuts the
    # node off the pod network until flannel restarts, so the code defers the
    # rewrite to the VM's next restart and the provider page documents that.
    # Neither page may claim live application again.
    network = NETWORK.read_text()
    for page in (network, PROVIDER.read_text()):
        assert "applies live" not in page
    mtu = network.split("### `network.cluster.mtu`", 1)[1].split("\n### ", 1)[0]
    assert "restart" in mtu
