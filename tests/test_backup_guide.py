"""Keep the backup-and-recovery guide published and its commands correct.

The guide classifies the cluster directory's files into irreplaceable identity,
derived client configs, and credentials, and its prose may be reworded freely.
What must not drift is that it stays published, that ``talossecrets.yaml``
really is outside the regenerable set the guide's client-configuration section
rests on, and that its cleanup commands operate on the same directory converge
is pointed at.
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.state import DERIVED_FILES
from taloscluster.state import SECRETS_FILE as TALOS_SECRETS_FILE

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "backup.md"


def test_guide_exists_and_is_linked_from_nav():
    assert GUIDE.is_file()
    nav = Path(__file__).resolve().parent.parent / "mkdocs.yml"
    assert "backup.md" in nav.read_text()


def test_derived_files_really_are_derived_in_code():
    # converge regenerates talosconfig/kubeconfig; only talossecrets.yaml keeps
    # the identity. DERIVED_FILES being exactly the regenerable set is what the
    # guide's client-configuration section rests on.
    assert TALOS_SECRETS_FILE not in DERIVED_FILES


def test_client_config_removal_scopes_to_the_converge_directory():
    # The "delete the derived client files" example must remove the files from
    # the same directory converge is told to work on (-C mycluster). A bare
    # `rm -f talosconfig kubeconfig` in the current directory alongside a
    # `-C mycluster` converge would delete nothing that converge regenerates.
    text = GUIDE.read_text()
    assert "rm -f mycluster/talosconfig mycluster/kubeconfig" in text
    rm_block = text.split("rm -f ", 1)[1].split("```", 1)[0]
    assert "taloscluster converge -C mycluster" in rm_block
