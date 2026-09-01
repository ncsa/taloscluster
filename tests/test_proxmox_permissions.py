"""Proxmox effective-permission preflight."""

from __future__ import annotations

import pytest

from taloscluster.errors import ReconcileError
from taloscluster.proxmox.permissions import requirements, validate_effective_permissions


def _requirements():
    return requirements(
        iso_storage="isos",
        cidata_storage="local",
        vm_storage="vms",
        nodes=["pve001"],
        network_path="/sdn/zones/localnetwork",
        vmids=[800],
    )


def test_root_equivalent_token_with_extra_privileges_passes():
    all_required = set().union(*(requirement.privileges for requirement in _requirements()))
    all_required.add("Permissions.Modify")

    validate_effective_permissions(
        {"/": {privilege: 1 for privilege in all_required}},
        _requirements(),
    )


def test_missing_permissions_list_privilege_and_acl_path():
    with pytest.raises(ReconcileError) as exc:
        validate_effective_permissions({"/": {"Sys.Audit": 1}}, _requirements())

    message = str(exc.value)
    assert "/storage/isos" in message
    assert "Datastore.AllocateTemplate" in message
    assert "/vms" in message
    assert "VM.Allocate" in message


def test_parent_acl_path_applies_to_owned_vm_path():
    grants = {
        requirement.path: {privilege: 1 for privilege in requirement.privileges}
        for requirement in _requirements()
        if requirement.path != "/vms/800"
    }

    validate_effective_permissions(grants, _requirements())
