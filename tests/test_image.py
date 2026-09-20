"""Tests for taloscluster.openstack.image.

Covers the schematic-aware boot image identity: an existing image is reused
only when it carries the *current* base schematic, and a stale schematic gets a
fresh image instead of a silent reuse. Also covers ``_download_and_decompress``,
which streams an xz-compressed Talos raw image from the factory and
lzma-decompresses it to disk. We monkeypatch ``requests.get`` with a fake
context-manager response so no network is involved, and assert the happy path
plus the two truncation guards (Content-Length mismatch and decomp.eof).
"""

from __future__ import annotations

import lzma
from types import SimpleNamespace

import pytest

from taloscluster.openstack import image
from taloscluster.talos import factory


class FakeResponse:
    """A stand-in for a requests.Response used as a context manager."""

    def __init__(self, chunks: list[bytes], content_length: int | None):
        self._chunks = chunks
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size: int = 8192):
        yield from self._chunks


class FakeGlance:
    """A stand-in for the openstack ``conn.image`` façade."""

    def __init__(self, images=None):
        self.images = images if images is not None else []
        self.created: list[str] = []

    def find_image(self, name):
        return next((i for i in self.images if i.name == name), None)

    def create_image(self, filename=None, name=None, **kwargs):
        img = SimpleNamespace(name=name, filename=filename, properties={})
        self.images.append(img)
        self.created.append(name)
        return img

    def delete_image(self, image):
        self.images = [i for i in self.images if i.id != image]

    def update_image(self, img, **kwargs):
        img.properties.update(kwargs)


class FakeConn:
    """A stand-in for an openstack connection whose ``image`` is a FakeGlance."""

    def __init__(self, images=None):
        self.image = FakeGlance(images)


def _chunked(data: bytes, size: int = 1024) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def _patch_get(monkeypatch, fake: FakeResponse):
    monkeypatch.setattr(image.requests, "get", lambda *a, **k: fake)


# ---------------------------------------------------------------------------
# ensure_image: the schematic is part of the image identity
# ---------------------------------------------------------------------------

def _cfg(talos_version: str = "v1.13.9") -> SimpleNamespace:
    return SimpleNamespace(talos_version=talos_version)


def test_ensure_image_reuses_image_when_schematic_matches(monkeypatch):
    """An image built from the current base extensions is reused, not rebuilt."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    built: list[str] = []
    monkeypatch.setattr(image, "_build_image", lambda *a, **k: built.append(a[3]))
    existing = SimpleNamespace(name="talos-v1.13.9-tailscale-abc123", properties={})
    conn = FakeConn([existing])

    name = image.ensure_image(conn, _cfg())

    assert name == "talos-v1.13.9-tailscale-abc123"
    assert conn.image.created == []
    assert existing.name in {i.name for i in conn.image.images}


def test_ensure_image_builds_fresh_image_for_stale_schematic(monkeypatch):
    """An existing image for a *different* base extension set is not reused: a
    new image under the current schematic's name is built instead."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "def456")
    built: list[str] = []
    monkeypatch.setattr(image, "_build_image", lambda conn, tv, ext, name: built.append(name))
    stale = SimpleNamespace(name="talos-v1.13.9-tailscale-abc123", properties={})
    conn = FakeConn([stale])

    name = image.ensure_image(conn, _cfg())

    assert name == "talos-v1.13.9-tailscale-def456"
    assert built == ["talos-v1.13.9-tailscale-def456"]
    # the stale image is left untouched for the old schematic
    assert [i.name for i in conn.image.images] == ["talos-v1.13.9-tailscale-abc123"]


def test_ensure_image_builds_when_no_image_present(monkeypatch):
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    built: list[str] = []
    monkeypatch.setattr(image, "_build_image", lambda conn, tv, ext, name: built.append(name))
    conn = FakeConn([])

    name = image.ensure_image(conn, _cfg())

    assert name == "talos-v1.13.9-tailscale-abc123"
    assert built == ["talos-v1.13.9-tailscale-abc123"]


# ---------------------------------------------------------------------------
# valid download
# ---------------------------------------------------------------------------

def test_download_and_decompress_valid(monkeypatch, tmp_path):
    original = b"hello talos" * 1000
    compressed = lzma.compress(original)
    fake = FakeResponse(_chunked(compressed), content_length=len(compressed))
    _patch_get(monkeypatch, fake)

    dest = tmp_path / "talos.raw"
    image._download_and_decompress("http://factory/img.raw.xz", dest)

    assert dest.read_bytes() == original


def test_download_and_decompress_single_chunk(monkeypatch, tmp_path):
    original = b"hello talos" * 1000
    compressed = lzma.compress(original)
    # serve as a single chunk
    fake = FakeResponse([compressed], content_length=len(compressed))
    _patch_get(monkeypatch, fake)

    dest = tmp_path / "talos.raw"
    image._download_and_decompress("http://factory/img.raw.xz", dest)

    assert dest.read_bytes() == original


# ---------------------------------------------------------------------------
# truncated: Content-Length mismatch
# ---------------------------------------------------------------------------

def test_download_and_decompress_truncated_with_content_length(monkeypatch, tmp_path):
    original = b"hello talos" * 1000
    compressed = lzma.compress(original)
    half = compressed[: len(compressed) // 2]
    # Content-Length reports the FULL length, but we only serve half
    fake = FakeResponse(_chunked(half), content_length=len(compressed))
    _patch_get(monkeypatch, fake)

    dest = tmp_path / "talos.raw"
    with pytest.raises(RuntimeError, match="truncated"):
        image._download_and_decompress("http://factory/img.raw.xz", dest)


# ---------------------------------------------------------------------------
# truncated: no Content-Length -> decomp.eof guard
# ---------------------------------------------------------------------------

def test_download_and_decompress_truncated_without_content_length(monkeypatch, tmp_path):
    original = b"hello talos" * 1000
    compressed = lzma.compress(original)
    half = compressed[: len(compressed) // 2]
    # no Content-Length header at all -> falls through to the decomp.eof check
    fake = FakeResponse(_chunked(half), content_length=None)
    _patch_get(monkeypatch, fake)

    dest = tmp_path / "talos.raw"
    with pytest.raises(RuntimeError, match="truncated"):
        image._download_and_decompress("http://factory/img.raw.xz", dest)


def test_download_and_decompress_truncated_does_not_produce_full_image(monkeypatch, tmp_path):
    """The dest file may hold partial bytes but must not equal the original."""
    original = b"hello talos" * 1000
    compressed = lzma.compress(original)
    half = compressed[: len(compressed) // 2]
    fake = FakeResponse(_chunked(half), content_length=None)
    _patch_get(monkeypatch, fake)

    dest = tmp_path / "talos.raw"
    with pytest.raises(RuntimeError):
        image._download_and_decompress("http://factory/img.raw.xz", dest)
    # whatever landed on disk is not the complete original
    assert dest.read_bytes() != original


def test_nocloud_installer_and_iso_urls_use_the_same_schematic():
    assert factory.installer_image("abc123", "v1.13.9", platform="nocloud") == (
        "factory.talos.dev/nocloud-installer/abc123:v1.13.9"
    )
    assert factory.nocloud_iso_url("abc123", "v1.13.9").endswith(
        "/image/abc123/v1.13.9/nocloud-amd64.iso"
    )


def test_metal_installer_url_uses_the_metal_platform():
    assert factory.installer_image("abc123", "v1.13.9", platform="metal") == (
        "factory.talos.dev/metal-installer/abc123:v1.13.9"
    )


# ---------------------------------------------------------------------------
# remove_image: the legacy pre-schematic name is still matched
# ---------------------------------------------------------------------------

def _os_backend(images, talos_version="v1.13.9"):
    from taloscluster.openstack.backend import OpenStackBackend

    backend = object.__new__(OpenStackBackend)
    backend.cfg = SimpleNamespace(talos_version=talos_version)
    backend.conn = FakeConn(images)
    return backend


def test_remove_image_deletes_legacy_and_schematic_images(monkeypatch):
    """`image remove` cleans up both the current and the legacy pre-schematic
    image so the orphan left by the rename does not linger."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    backend = _os_backend(
        images=[
            SimpleNamespace(name="talos-v1.13.9-tailscale-abc123", id="img-new"),
            SimpleNamespace(name="talos-v1.13.9-tailscale", id="img-legacy"),
        ]
    )

    backend.remove_image(assume_yes=True)

    assert [i.id for i in backend.conn.image.images] == []


def test_remove_image_cleans_up_an_orphaned_legacy_image(monkeypatch):
    """A cluster that only has the old schematicless image (the migration
    orphan) can still remove it even when no current image exists."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    backend = _os_backend(
        images=[SimpleNamespace(name="talos-v1.13.9-tailscale", id="img-legacy")]
    )

    backend.remove_image(assume_yes=True)

    assert [i.id for i in backend.conn.image.images] == []


def test_remove_image_prompts_with_legacy_name_when_only_it_exists(monkeypatch):
    """When only the legacy pre-schematic image is present, `image remove`
    asks the operator to confirm the legacy name actually found, not the
    current schematic name that is not being deleted."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    monkeypatch.setattr("taloscluster.openstack.backend.dry_run", lambda: False)
    backend = _os_backend(
        images=[SimpleNamespace(name="talos-v1.13.9-tailscale", id="img-legacy")]
    )
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "talos-v1.13.9-tailscale",
    )

    backend.remove_image()

    assert prompts == ["type 'talos-v1.13.9-tailscale' to confirm: "]
    assert all("tailscale-abc123" not in p for p in prompts)


def test_remove_image_prompts_with_both_names_when_both_exist(monkeypatch):
    """When both the schematic and legacy pre-schematic images are present,
    `image remove` asks the operator to confirm every name it will delete."""
    monkeypatch.setattr(factory, "schematic_id", lambda _exts: "abc123")
    monkeypatch.setattr("taloscluster.openstack.backend.dry_run", lambda: False)
    backend = _os_backend(
        images=[
            SimpleNamespace(name="talos-v1.13.9-tailscale-abc123", id="img-new"),
            SimpleNamespace(name="talos-v1.13.9-tailscale", id="img-legacy"),
        ]
    )
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt)
        or "talos-v1.13.9-tailscale-abc123, talos-v1.13.9-tailscale",
    )

    backend.remove_image()

    assert prompts == [
        "type 'talos-v1.13.9-tailscale-abc123, talos-v1.13.9-tailscale' to confirm: "
    ]
    assert [i.id for i in backend.conn.image.images] == []
