"""Tests for clusterctl.state: talos secrets persistence + reset, using tmp_path.

No real secrets are written -- just marker text -- since we only test the
State helper's filesystem behaviour, permissions, and error contract.
"""

from __future__ import annotations

import os
import stat

import pytest

from clusterctl.errors import StateError
from clusterctl.state import DERIVED_FILES, SECRETS_FILE, State


def test_secrets_exist_false_when_missing(tmp_path):
    state = State(tmp_path)
    assert state.secrets_exist() is False


def test_secrets_exist_false_when_empty_file(tmp_path):
    (tmp_path / SECRETS_FILE).write_text("")
    state = State(tmp_path)
    assert state.secrets_exist() is False


def test_secrets_exist_true_after_write_secrets(tmp_path):
    state = State(tmp_path)
    state.write_secrets("cluster:\n  ca: dummy\n")
    assert state.secrets_exist() is True


def test_write_secrets_sets_mode_0600(tmp_path):
    state = State(tmp_path)
    state.write_secrets("dummy secrets")
    mode = stat.S_IMODE(os.stat(state.secrets_path).st_mode)
    assert mode == 0o600


def test_require_secrets_raises_state_error_when_missing(tmp_path):
    state = State(tmp_path)
    with pytest.raises(StateError):
        state.require_secrets()


def test_require_secrets_returns_path_when_present(tmp_path):
    state = State(tmp_path)
    state.write_secrets("dummy secrets")
    result = state.require_secrets()
    assert result == state.secrets_path
    assert result.is_file()


def test_reset_removes_secrets_and_derived_files(tmp_path):
    secrets = tmp_path / SECRETS_FILE
    derived = [tmp_path / f for f in DERIVED_FILES]
    secrets.write_text("dummy")
    for path in derived:
        path.write_text("dummy")

    State(tmp_path).reset()

    assert not secrets.exists()
    for path in derived:
        assert not path.exists()


def test_reset_is_noop_when_files_dont_exist(tmp_path):
    state = State(tmp_path)
    # nothing exists -> reset must not raise
    state.reset()
    assert not (tmp_path / SECRETS_FILE).exists()
