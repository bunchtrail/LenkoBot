from datetime import datetime, timedelta, timezone
import json
import os
import sys

import pytest

from lenkobot.linux_oauth_credentials import (
    LinuxOAuthCredentialStore,
    LinuxOAuthRefreshLock,
    linux_credential_path,
)
from lenkobot.xai_provider import CredentialUnavailable, OAuthTokenState


PRIVATE_MODE = 0o600


class FakePosixSecurity:
    def __init__(self, *, uid=1000, mode=PRIVATE_MODE, owner_uid=None):
        self.uid = uid
        self.mode = mode
        self.owner_uid = uid if owner_uid is None else owner_uid
        self.locked_descriptors = set()
        self.lock_attempts = 0

    def current_uid(self):
        return self.uid

    def descriptor_owner_uid(self, descriptor):
        return self.owner_uid

    def descriptor_mode(self, descriptor):
        return self.mode

    def lock_exclusive(self, descriptor):
        self.lock_attempts += 1
        if self.locked_descriptors:
            return False
        self.locked_descriptors.add(descriptor)
        return True

    def unlock(self, descriptor):
        self.locked_descriptors.discard(descriptor)


def state(access="access-token", refresh="refresh-token"):
    return OAuthTokenState(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc) + timedelta(hours=1),
    )


def store(tmp_path, security=None, **kwargs):
    return LinuxOAuthCredentialStore(
        path=tmp_path / "xai-oauth.json",
        security=security or FakePosixSecurity(),
        **kwargs,
    )


def test_linux_store_round_trips_state_through_private_file(tmp_path):
    security = FakePosixSecurity()
    credential_store = store(tmp_path, security)

    credential_store.save(state())
    loaded = credential_store.load()

    assert loaded == state()
    assert (tmp_path / "xai-oauth.json").exists()


def test_linux_store_returns_none_for_missing_credential_file(tmp_path):
    assert store(tmp_path).load() is None


def test_linux_store_writes_only_expected_fields(tmp_path):
    credential_store = store(tmp_path)

    credential_store.save(state())

    payload = json.loads((tmp_path / "xai-oauth.json").read_text(encoding="utf-8"))
    assert set(payload) == {"access_token", "expires_at", "refresh_token"}


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o700])
def test_linux_store_fails_closed_for_non_private_file_mode(tmp_path, mode):
    credential_store = store(tmp_path)
    credential_store.save(state())
    relaxed = store(tmp_path, FakePosixSecurity(mode=mode))

    with pytest.raises(CredentialUnavailable):
        relaxed.load()


def test_linux_store_fails_closed_when_another_uid_owns_the_file(tmp_path):
    credential_store = store(tmp_path)
    credential_store.save(state())
    foreign = store(tmp_path, FakePosixSecurity(uid=1000, owner_uid=0))

    with pytest.raises(CredentialUnavailable):
        foreign.load()


@pytest.mark.parametrize(
    "blob",
    [
        "not json",
        json.dumps({"access_token": "a"}),
        json.dumps({"access_token": "a", "refresh_token": "b", "expires_at": "nope"}),
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "b",
                "expires_at": "2026-07-26T12:00:00+00:00",
                "extra": "field",
            }
        ),
    ],
)
def test_linux_store_rejects_malformed_state(tmp_path, blob):
    path = tmp_path / "xai-oauth.json"
    path.write_text(blob, encoding="utf-8")

    with pytest.raises(CredentialUnavailable):
        store(tmp_path).load()


def test_linux_store_rejects_oversized_state(tmp_path):
    path = tmp_path / "xai-oauth.json"
    path.write_text("x" * 4096, encoding="utf-8")

    with pytest.raises(CredentialUnavailable):
        store(tmp_path).load()


def test_linux_store_replaces_previous_state_without_leaving_temporary_files(tmp_path):
    credential_store = store(tmp_path)

    credential_store.save(state())
    credential_store.save(state(access="rotated", refresh="rotated-refresh"))

    assert credential_store.load() == state(access="rotated", refresh="rotated-refresh")
    assert [item.name for item in tmp_path.iterdir()] == ["xai-oauth.json"]


def test_linux_store_keeps_previous_state_when_write_fails(tmp_path):
    credential_store = store(tmp_path)
    credential_store.save(state())

    with pytest.raises(CredentialUnavailable):
        credential_store.save(OAuthTokenState("", "refresh", state().expires_at))

    assert credential_store.load() == state()
    assert [item.name for item in tmp_path.iterdir()] == ["xai-oauth.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_linux_store_refuses_symlinked_credential_path(tmp_path):
    target = tmp_path / "actual.json"
    target.write_text(
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "b",
                "expires_at": "2026-07-26T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    link = tmp_path / "xai-oauth.json"
    link.symlink_to(target)

    with pytest.raises(CredentialUnavailable):
        store(tmp_path).load()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode semantics")
def test_linux_store_creates_owner_only_file_on_disk(tmp_path):
    store(tmp_path).save(state())

    mode = os.stat(tmp_path / "xai-oauth.json").st_mode
    assert mode & 0o777 == PRIVATE_MODE


def test_linux_store_creates_missing_private_directory(tmp_path):
    nested = tmp_path / "config" / "lenkobot"
    credential_store = LinuxOAuthCredentialStore(
        path=nested / "xai-oauth.json",
        security=FakePosixSecurity(),
    )

    credential_store.save(state())

    assert credential_store.load() == state()


def test_linux_credential_path_prefers_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert linux_credential_path() == tmp_path / "xdg" / "lenkobot" / "xai-oauth.json"


def test_linux_credential_path_falls_back_to_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert linux_credential_path(home=tmp_path / "home") == (
        tmp_path / "home" / ".config" / "lenkobot" / "xai-oauth.json"
    )


def test_linux_credential_path_ignores_relative_xdg_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")

    assert linux_credential_path(home=tmp_path) == (
        tmp_path / ".config" / "lenkobot" / "xai-oauth.json"
    )


def test_linux_refresh_lock_acquires_and_releases(tmp_path):
    security = FakePosixSecurity()
    lock = LinuxOAuthRefreshLock(tmp_path / "xai-oauth.json", security=security)

    with lock:
        assert security.locked_descriptors

    assert not security.locked_descriptors


def test_linux_refresh_lock_fails_closed_when_already_held(tmp_path):
    security = FakePosixSecurity()
    path = tmp_path / "xai-oauth.json"
    holder = LinuxOAuthRefreshLock(path, security=security)
    contender = LinuxOAuthRefreshLock(path, security=security, timeout_seconds=0.05)

    with holder:
        with pytest.raises(CredentialUnavailable):
            with contender:
                pass

    assert not security.locked_descriptors


def test_linux_refresh_lock_rejects_invalid_timeout(tmp_path):
    with pytest.raises(ValueError):
        LinuxOAuthRefreshLock(
            tmp_path / "xai-oauth.json",
            security=FakePosixSecurity(),
            timeout_seconds=0,
        )
