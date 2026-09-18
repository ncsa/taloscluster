"""Keep the machine-access guide in lockstep with how taloscluster reaches nodes.

The guide documents the two management access paths end to end: an already-connected
Tailscale management machine (the first control plane reached by its MagicDNS name)
and direct access to real node addresses without Tailscale (the control plane reached
by its provider-reported address). These tests pin the guide to the actual endpoint
selection in ``converge._talos_endpoint`` and ``converge._resolve_cp1_address``, so a
code change to either path is caught against the documentation.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "concepts" / "machines.md"
CONVERGE = ROOT / "taloscluster" / "converge.py"
TALOSCTL = ROOT / "taloscluster" / "talos" / "talosctl.py"
PROXMOX_BACKEND = ROOT / "taloscluster" / "proxmox" / "backend.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = ROOT / "mkdocs.yml"
    assert "concepts/machines.md" in nav.read_text()


def test_guide_documents_both_access_paths_end_to_end():
    text = GUIDE.read_text()
    assert "Path A: an already-connected Tailscale management machine" in text
    assert "Path B: direct access to real node addresses without Tailscale" in text
    for heading in (
        "To use this path end to end:",
        "Verify the path with `taloscluster status`",
    ):
        assert heading in text


def test_guide_maps_the_paths_to_the_tailscale_section():
    # Which path applies is decided by the presence of the `tailscale` section.
    text = GUIDE.read_text()
    assert "decided by whether the `tailscale` section is present in `cluster.yaml`" in text
    assert "`tailscale` section (even empty) is present" in text
    assert "no `tailscale` section" in text
    assert "management talks to the first control plane by its MagicDNS name" in text
    assert "management talks to the first control plane's real node address" in text


def test_guide_tailscale_path_matches_the_endpoint_selection():
    # Without tailscale there is no MagicDNS name to resolve, so converge reaches
    # controlplane-01 by its tailscale name. The guide must say the same.
    text = GUIDE.read_text()
    assert "`<name>-controlplane-01`" in text
    assert "MagicDNS name" in text
    converge = CONVERGE.read_text()
    assert "cp-01's tailscale name" in converge
    assert "there is no MagicDNS name to resolve" in converge
    assert "cp-01's real address is used instead" in converge


def test_guide_direct_path_matches_the_real_address_fallback():
    # Without tailscale converge falls back to the provider-reported node address.
    text = GUIDE.read_text()
    assert "provider-reported address" in text
    assert "real node address" in text
    assert "this host must route to it" in CONVERGE.read_text()


def test_guide_direct_address_resolution_order_matches_resolve_cp1():
    # The direct path resolves the control plane from a managed-SDN static address,
    # then the guest agent's report, then a recorded talosconfig. Pin these to code.
    text = GUIDE.read_text()
    assert "managed-SDN static address from the network plan" in text
    assert (
        "address the guest agent reports, polling until a freshly booted "
        "node reports one" in text
    )
    assert "endpoint an earlier `talosconfig` recorded" in text
    converge = CONVERGE.read_text()
    assert "no tailscale: resolving" in converge
    assert "load_inventory().machine_address" in converge


def test_guide_direct_path_requires_routing_from_the_management_machine():
    text = GUIDE.read_text()
    assert "must already be able to route to" in text
    assert "Make the node addresses reachable" in text
    assert "TCP/50000" in text


def test_guide_tailscale_path_requires_an_already_connected_management_machine():
    # The management machine must be connected to the tailnet; taloscluster does
    # not add it. Pin that to the code comment and the guide wording.
    text = GUIDE.read_text()
    assert "already-connected Tailscale management machine" in text
    assert "taloscluster does not add that management machine automatically" in text
    assert "`100.64.0.0/10`" in text
    assert "cp-01's tailscale name" in CONVERGE.read_text()


def test_guide_allowlists_include_the_management_network():
    # Both paths need the management source in the kubernetes and talos allowlists,
    # or converge locks itself out of the firewall it just applied.
    text = GUIDE.read_text()
    assert "or converge locks itself out" in text
    assert "`kubernetes` and `talos` rules" in text
    assert "`100.64.0.0/10`" in text
    assert "UDP/41641" in text


def test_guide_verify_commands_use_the_generated_talosconfig():
    # Both paths' verify `talosctl ... version` commands must point at the
    # generated `./talosconfig`, otherwise talosctl falls back to the
    # environment or home config and may reach nothing. Pin each one.
    text = GUIDE.read_text()
    assert "talosctl --talosconfig talosconfig -n" in text
    assert "talosctl --talosconfig talosconfig -n mycluster-controlplane-01 version" in text
    assert "talosctl --talosconfig talosconfig -n 192.0.2.11 version" in text


def test_guide_verify_commands_have_no_bare_talosctl():
    # A `talosctl -n ... version` without `--talosconfig` defaults to the
    # environment or home config rather than `./talosconfig`, so the guide must
    # never hand the operator a bare talosctl verify command.
    for line in GUIDE.read_text().splitlines():
        if "version" in line and "talosctl -n" in line:
            assert "--talosconfig talosconfig" in line


def test_guide_states_vip_exclusion_once_in_path_b():
    # The kube-api VIP is excluded from node-address selection: the guide states
    # that rule in the Path B intro, backed by Talos discovery's `exclude_vip`
    # parameter and the Proxmox backend's `str(parsed) != cluster_vip` filter,
    # which report the next real address rather than a floating VIP.
    text = GUIDE.read_text()
    assert "excluded" in text
    assert "exclude_vip" in TALOSCTL.read_text()
    assert "str(parsed) != cluster_vip" in PROXMOX_BACKEND.read_text()


def test_guide_describes_duplicate_vm_name_collision():
    # Proxmox keys machines by VM name: a name shared with a cluster-managed
    # VM aborts converge/destroy, while collisions among unmanaged VMs are ignored.
    text = GUIDE.read_text()
    assert "a VM name shared with a cluster-managed machine aborts converge and destroy" in text
    assert "collisions among unmanaged VMs are ignored" in text
    assert "one managed by this cluster or two" not in text
