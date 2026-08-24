"""Tests for taloscluster.openstack.image._download_and_decompress.

The function streams an xz-compressed Talos raw image from the factory and
lzma-decompresses it to disk. We monkeypatch ``requests.get`` with a fake
context-manager response so no network is involved, and assert the happy path
plus the two truncation guards (Content-Length mismatch and decomp.eof).
"""

from __future__ import annotations

import lzma

import pytest

from taloscluster.openstack import image


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


def _chunked(data: bytes, size: int = 1024) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def _patch_get(monkeypatch, fake: FakeResponse):
    monkeypatch.setattr(image.requests, "get", lambda *a, **k: fake)


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
