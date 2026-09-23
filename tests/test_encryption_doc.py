"""Keep the disk-encryption docs honest about what the static passphrase buys.

``_disk_encryption_patch`` keys STATE and EPHEMERAL with one cluster-wide
static passphrase, and Talos stores the key material on the disk itself: the
STATE partition's passphrase lands in the disk's unencrypted META partition
and the EPHEMERAL passphrase rides the machine configuration on STATE. A
stolen disk or volume snapshot therefore carries everything needed to unlock
both partitions, so ``docs/concepts/talos.md`` must not claim at-rest
protection against one, and the CHANGELOG bullet must scope encryption to
clusters created after it was introduced -- an existing cluster's
``talossecrets.yaml`` carries no passphrase, so its machines stay unencrypted.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TALOS_DOC = (ROOT / "docs" / "concepts" / "talos.md").read_text()
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


def _encryption_paragraph() -> str:
    paragraphs = [p for p in TALOS_DOC.split("\n\n") if "LUKS2" in p]
    assert paragraphs, "the disk-encryption paragraph vanished from talos.md"
    (paragraph,) = paragraphs
    return paragraph


def test_encryption_paragraph_states_the_disk_carries_its_own_unlock_keys():
    paragraph = _encryption_paragraph()
    assert "unencrypted META partition" in paragraph
    assert "lives on STATE" in paragraph
    assert "unlock both partitions" in paragraph


def test_encryption_page_does_not_claim_snapshot_protection():
    assert "reads as ciphertext" not in TALOS_DOC
    assert "no storage the hypervisor keeps" not in TALOS_DOC


def test_encryption_paragraph_names_the_stronger_keys_taloscluster_omits():
    paragraph = _encryption_paragraph()
    assert "TPM" in paragraph
    assert "KMS" in paragraph


def test_changelog_scopes_encryption_to_new_clusters():
    bullet = next(
        line for line in CHANGELOG.splitlines() if "Encrypt STATE and EPHEMERAL partitions" in line
    )
    assert "new clusters only" in bullet
