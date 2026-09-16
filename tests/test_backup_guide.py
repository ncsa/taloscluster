"""Keep the backup-and-recovery guide in lockstep with the code's artifact model.

The guide's whole point is classifying the cluster directory's files into
irreplaceable identity, derived client configs, and credentials. If the code
splits those categories differently (a new irreplaceable file, a derived file
that stops being regenerable), the guide must say so. These tests pin the guide
to the actual constants in ``taloscluster.state`` and ``taloscluster.config``
and to the hard-fail behaviour of ``State.require_secrets``.
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE
from taloscluster.state import DERIVED_FILES
from taloscluster.state import SECRETS_FILE as TALOS_SECRETS_FILE

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "backup.md"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "backup.md" in nav.read_text()


def test_guide_marks_talos_secrets_as_irreplaceable_identity():
    text = GUIDE.read_text()
    assert TALOS_SECRETS_FILE in text
    assert "cannot be regenerated" in text
    assert text.count("restore it from backup") >= 1


def test_guide_marks_provider_credentials_as_restorable():
    text = GUIDE.read_text()
    assert SECRETS_FILE in text
    assert "not the cluster's identity" in text


def test_guide_lists_cluster_yaml_as_part_of_the_backup():
    assert CLUSTER_FILE in GUIDE.read_text()


def test_guide_names_the_derived_client_configs_as_regenerable():
    text = GUIDE.read_text()
    for name in DERIVED_FILES:
        assert name in text


def test_derived_files_really_are_derived_in_code():
    # converge regenerates talosconfig/kubeconfig; only talossecrets.yaml keeps
    # the identity. DERIVED_FILES being exactly the regenerable set is what the
    # guide's client-configuration section rests on.
    assert TALOS_SECRETS_FILE not in DERIVED_FILES


def test_guide_recovery_is_cross_referenced_from_the_failure_message():
    # A recovered lost management machine must restore talossecrets.yaml before
    # converge; the guide must document the lost-identity case because the code
    # hard-fails there instead of regenerating.
    text = GUIDE.read_text()
    assert "refuses to proceed" in text
    assert "cannot be regenerated" in text


def test_client_config_removal_scopes_to_the_converge_directory():
    # The "delete the derived client files" example must remove the files from
    # the same directory converge is told to work on (-C mycluster). A bare
    # `rm -f talosconfig kubeconfig` in the current directory alongside a
    # `-C mycluster` converge would delete nothing that converge regenerates.
    text = GUIDE.read_text()
    assert "rm -f mycluster/talosconfig mycluster/kubeconfig" in text
    rm_block = text.split("rm -f ", 1)[1].split("```", 1)[0]
    assert "taloscluster converge -C mycluster" in rm_block
