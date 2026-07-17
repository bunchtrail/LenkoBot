import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from lenkobot.oauth_credentials import OAuthDeviceAuthorization
from lenkobot.runtime import login, load_runtime_settings, run_application
from lenkobot.xai_provider import CredentialUnavailable, OAuthTokenState


def write_config(tmp_path):
    config_path = tmp_path / "lenkobot.toml"
    config_path.write_text(
        """
default_persona_key = "lenko"

[[personas]]
key = "lenko"
display_name = "Lenko"
identity_prompt = "Be concise."
identity_version = 1

[telegram]
allowed_user_id = 123456789

[oauth]
client_id = "public-client-id"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def token_state():
    return OAuthTokenState(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def test_load_runtime_settings_uses_config_parent_data_root_and_persona_catalog(
    tmp_path,
):
    settings = load_runtime_settings(write_config(tmp_path))

    assert settings.data_root == tmp_path / "data"
    assert settings.allowed_user_id == 123456789
    assert settings.oauth_client_id == "public-client-id"
    assert settings.persona_catalog.default_persona_key == "lenko"


def test_login_uses_configured_client_and_only_prints_user_visible_data(
    tmp_path,
    monkeypatch,
):
    import lenkobot.runtime as runtime

    class FakeCredentialStore:
        target_name = "LenkoBot/xai-oauth/v1/default"

        def __init__(self, *args, **kwargs):
            pass

    class FakeMutex:
        targets = []

        def __init__(self, target_name, *args, **kwargs):
            self.targets.append(target_name)

    class FakeDeviceClient:
        client_ids = []
        authorization = OAuthDeviceAuthorization(
            device_code="device-secret",
            user_code="ABCD-EFGH",
            verification_uri="https://accounts.x.ai/activate",
            verification_uri_complete="https://accounts.x.ai/activate?code=ABCD-EFGH",
            expires_at=datetime(2026, 7, 17, 12, 15, tzinfo=timezone.utc),
            interval_seconds=5,
        )
        completions = []

        def __init__(self, *, client_id):
            self.client_ids.append(client_id)

        def start_device_authorization(self):
            return self.authorization

        def complete_device_authorization(self, authorization, *, store, lock):
            self.completions.append((authorization, store, lock))
            return token_state()

    monkeypatch.setattr(runtime, "WindowsOAuthCredentialStore", FakeCredentialStore)
    monkeypatch.setattr(runtime, "WindowsOAuthRefreshMutex", FakeMutex)
    monkeypatch.setattr(runtime, "XaiOAuthDeviceClient", FakeDeviceClient)
    output = []

    login(load_runtime_settings(write_config(tmp_path)), output=output.append)

    assert FakeDeviceClient.client_ids == ["public-client-id"]
    assert FakeMutex.targets == ["LenkoBot/xai-oauth/v1/default"]
    assert output == [
        "Open: https://accounts.x.ai/activate",
        "Code: ABCD-EFGH",
        "OAuth login completed.",
    ]
    assert "device-secret" not in "\n".join(output)
    assert len(FakeDeviceClient.completions) == 1


def test_run_fails_before_polling_when_oauth_state_is_missing(tmp_path, monkeypatch):
    import lenkobot.runtime as runtime

    class MissingCredentialStore:
        target_name = "LenkoBot/xai-oauth/v1/default"

        def __init__(self, *args, **kwargs):
            pass

        def load(self):
            return None

    async def polling(*args, **kwargs):
        raise AssertionError("polling must not start")

    monkeypatch.setattr(runtime, "WindowsOAuthCredentialStore", MissingCredentialStore)

    with pytest.raises(CredentialUnavailable, match="unavailable"):
        asyncio.run(
            run_application(
                load_runtime_settings(write_config(tmp_path)),
                "telegram-secret",
                polling=polling,
            )
        )


@pytest.mark.parametrize("polling_error", (None, RuntimeError("polling failed")))
def test_run_composes_oauth_only_service_with_shared_state_database_and_closes_stores(
    tmp_path,
    monkeypatch,
    polling_error,
):
    import lenkobot.runtime as runtime
    from lenkobot.aiogram_adapter import AiogramTelegramResponsePort
    from lenkobot.application_service import TelegramApplicationService

    class ExistingCredentialStore:
        target_name = "LenkoBot/xai-oauth/v1/default"

        def __init__(self, *args, **kwargs):
            pass

        def load(self):
            return token_state()

    class FakeMutex:
        def __init__(self, *args, **kwargs):
            pass

    class RecordingConversationStore:
        paths = []
        closed = 0

        def __init__(self, database_path):
            self.paths.append(database_path)

        def close(self):
            type(self).closed += 1

    class RecordingMemoryStore:
        paths = []
        closed = 0

        def __init__(self, database_path, **kwargs):
            self.paths.append(database_path)

        def close(self):
            type(self).closed += 1

    observed = {}

    async def polling(bot_token, handler, *, response_port_factory):
        observed["bot_token"] = bot_token
        observed["handler"] = handler
        observed["response_port_factory"] = response_port_factory
        if polling_error is not None:
            raise polling_error

    monkeypatch.setattr(runtime, "WindowsOAuthCredentialStore", ExistingCredentialStore)
    monkeypatch.setattr(runtime, "WindowsOAuthRefreshMutex", FakeMutex)
    monkeypatch.setattr(runtime, "SQLiteConversationStore", RecordingConversationStore)
    monkeypatch.setattr(runtime, "SQLiteMemoryStore", RecordingMemoryStore)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    settings = load_runtime_settings(write_config(tmp_path))

    if polling_error is None:
        asyncio.run(run_application(settings, "telegram-secret", polling=polling))
    else:
        with pytest.raises(RuntimeError, match="polling failed"):
            asyncio.run(run_application(settings, "telegram-secret", polling=polling))

    expected_database_path = tmp_path / "data" / "state.db"
    assert RecordingConversationStore.paths == [expected_database_path]
    assert RecordingMemoryStore.paths == [expected_database_path]
    assert RecordingConversationStore.closed == 1
    assert RecordingMemoryStore.closed == 1
    assert observed["bot_token"] == "telegram-secret"
    assert isinstance(observed["handler"], TelegramApplicationService)
    assert observed["response_port_factory"] is AiogramTelegramResponsePort
