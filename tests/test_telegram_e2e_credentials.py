import json

import pytest

from lenkobot.telegram_e2e_credentials import (
    TelegramE2ECredentialError,
    TelegramE2ECredentialState,
    WindowsTelegramE2ECredentialStore,
)


class FakeCredentialApi:
    def __init__(self, blob=None):
        self.blob = blob
        self.reads = []
        self.writes = []

    def read(self, target_name):
        self.reads.append(target_name)
        return self.blob

    def write(self, target_name, blob):
        self.writes.append((target_name, blob))
        self.blob = blob


def credential_state(**overrides):
    values = {
        "api_id": 12345,
        "api_hash": "a" * 32,
        "session": "serialized-session-secret",
        "user_id": 555,
    }
    values.update(overrides)
    return TelegramE2ECredentialState(**values)


def test_telegram_e2e_credential_store_round_trips_redacted_state():
    api = FakeCredentialApi()
    store = WindowsTelegramE2ECredentialStore(api=api)
    expected = credential_state()

    store.save(expected)
    loaded = store.load()

    assert loaded == expected
    assert api.reads == ["LenkoBot/telegram-e2e/v1/default"]
    assert api.writes[0][0] == "LenkoBot/telegram-e2e/v1/default"
    assert "serialized-session-secret" not in repr(loaded)
    assert "a" * 32 not in repr(loaded)


def test_telegram_e2e_credential_store_rejects_malformed_or_oversized_blob():
    malformed = FakeCredentialApi(
        json.dumps(
            {
                "api_id": 12345,
                "api_hash": "api-hash-secret",
                "session": "session-secret",
            }
        ).encode("utf-8")
    )
    store = WindowsTelegramE2ECredentialStore(api=malformed)

    with pytest.raises(TelegramE2ECredentialError) as malformed_error:
        store.load()

    assert "api-hash-secret" not in str(malformed_error.value)
    assert "session-secret" not in str(malformed_error.value)

    oversized_api = FakeCredentialApi()
    oversized_store = WindowsTelegramE2ECredentialStore(api=oversized_api)
    with pytest.raises(TelegramE2ECredentialError, match="too large"):
        oversized_store.save(credential_state(session="s" * 2600))
    assert oversized_api.writes == []


def test_telegram_e2e_credential_store_returns_none_when_not_configured():
    store = WindowsTelegramE2ECredentialStore(api=FakeCredentialApi())

    assert store.load() is None
