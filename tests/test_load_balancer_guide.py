"""Keep the load-balancer and ingress guide in lockstep with the code's address contract.

The guide's whole point is the split of responsibility: core taloscluster allocates
or configures the addresses, and the operator/GitOps repository must supply the
MetalLB ``IPAddressPool``/``L2Advertisement`` and the ingress ``LoadBalancer``/``Ingress``
resources that use them. These tests pin the guide to the provider backends (the
OpenStack single ingress VIP vs. the Proxmox ``ingress_pool`` range) and to the
ArgoCD plugin's rendering of those addresses.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "load-balancer.md"
OPENSTACK_BACKEND = ROOT / "taloscluster" / "openstack" / "backend.py"
PROXMOX_BACKEND = ROOT / "taloscluster" / "proxmox" / "backend.py"
OPENSTACK_NETWORK = ROOT / "taloscluster" / "openstack" / "network.py"
ARGOCD_MANIFESTS = ROOT / "plugins" / "argocd" / "taloscluster_argocd" / "manifests.py"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = ROOT / "mkdocs.yml"
    assert "load-balancer.md" in nav.read_text()


def test_guide_splits_allocation_from_operator_resources():
    text = GUIDE.read_text()
    assert "core `taloscluster`" in text
    assert "IPAddressPool" in text
    assert "L2Advertisement" in text
    assert "your GitOps repository" in text
    assert "must supply" in text
    assert "never creates a MetalLB address pool" in text or "you install MetalLB" in text


def test_openstack_side_matches_the_backend():
    # OpenStack reports the ingress fixed VIP as the MetalLB /32 and the floating
    # IP as the advertised address; the backend must agree with the guide.
    text = GUIDE.read_text()
    assert "ingress VIP" in text
    assert "floating IP" in text
    assert "/32" in text
    openstack = OPENSTACK_BACKEND.read_text()
    assert "metallb=(refs.ingress_vip,)" in openstack.replace(" ", "")
    assert "advertised_address=refs.ingress_fip" in openstack.replace(" ", "")


def test_openstack_worker_ports_carry_the_ingress_vip():
    text = GUIDE.read_text()
    assert "allowed_address_pairs" in text
    network = OPENSTACK_NETWORK.read_text()
    assert "refs.ingress_vip" in network


def test_proxmox_side_matches_the_backend():
    text = GUIDE.read_text()
    assert "ingress_pool" in text
    assert "no single VIP" in text
    proxmox = PROXMOX_BACKEND.read_text()
    assert "metallb=(ingress_pool,)" in proxmox.replace(" ", "")


def test_guide_does_not_claim_core_creates_a_metallb_pool():
    # The guide must state the operator creates the MetalLB pool; core only
    # supplies addresses. Match the initial plan wording in the doc and config.
    text = GUIDE.read_text()
    assert "never creates a MetalLB address pool" in text
    assert "IPAddressPool" in text
    assert "you install MetalLB" in text


def test_guide_matches_the_argocd_rendering():
    text = GUIDE.read_text()
    assert "/32" in text  # the OpenStack address is a /32
    manifests = ARGOCD_MANIFESTS.read_text()
    # A bare single IP becomes a /32; a range ("-") is passed verbatim.
    assert '"/" in addr' in manifests or '"-" in addr' in manifests
    assert 'f"{addr}/32"' in manifests
