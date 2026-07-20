import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lenkobot.oauth_credentials import OAuthDeviceAuthorization
from lenkobot.runtime import (
    login,
    load_runtime_settings,
    login_telegram_e2e,
    main,
    run_application,
    run_local_chat,
)
from lenkobot.telegram_e2e import TelegramE2EReport, TelegramE2EStep
from lenkobot.telegram_e2e_credentials import TelegramE2ECredentialState
from lenkobot.xai_provider import CredentialUnavailable, OAuthTokenState


def write_config(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_load_runtime_settings_uses_hermes_reference_client_when_oauth_override_is_missing(
    tmp_path,
):
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").split("\n[oauth]\n", 1)[0],
        encoding="utf-8",
    )

    settings = load_runtime_settings(config_path)

    assert settings.oauth_client_id == "b1a00492-073a-47ea-816f-4c329264a828"


def test_load_runtime_settings_exposes_only_configured_export_recipient(tmp_path):
    config_path = write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write("\n[export]\nage_recipient = \"age1example\"\n")

    settings = load_runtime_settings(config_path)

    assert settings.export_recipient == "age1example"


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
        recovery_users = []

        def __init__(self, database_path, **kwargs):
            self.paths.append(database_path)

        def extraction_lane_ids_for_user(self, *, owner_user_id):
            type(self).recovery_users.append(owner_user_id)
            return ()

        def close(self):
            type(self).closed += 1

    class RecordingSessionStore:
        paths = []
        closed = 0

        def __init__(self, database_path, **kwargs):
            self.paths.append(database_path)

        def close(self):
            type(self).closed += 1

    observed = {}

    async def polling(
        bot_token,
        handler,
        *,
        response_port_factory,
        command_scope_chat_id,
    ):
        observed["bot_token"] = bot_token
        observed["handler"] = handler
        observed["response_port_factory"] = response_port_factory
        observed["command_scope_chat_id"] = command_scope_chat_id
        if polling_error is not None:
            raise polling_error

    monkeypatch.setattr(runtime, "WindowsOAuthCredentialStore", ExistingCredentialStore)
    monkeypatch.setattr(runtime, "WindowsOAuthRefreshMutex", FakeMutex)
    monkeypatch.setattr(runtime, "SQLiteConversationStore", RecordingConversationStore)
    monkeypatch.setattr(runtime, "SQLiteMemoryStore", RecordingMemoryStore)
    monkeypatch.setattr(runtime, "SQLiteSessionStore", RecordingSessionStore)
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
    assert RecordingSessionStore.paths == [expected_database_path]
    assert RecordingConversationStore.closed == 1
    assert RecordingMemoryStore.closed == 1
    assert RecordingSessionStore.closed == 1
    assert RecordingMemoryStore.recovery_users == [settings.allowed_user_id]
    assert observed["bot_token"] == "telegram-secret"
    assert isinstance(observed["handler"], TelegramApplicationService)
    assert observed["response_port_factory"] is AiogramTelegramResponsePort
    assert observed["command_scope_chat_id"] == settings.allowed_user_id


def test_main_composes_confirmed_live_smoke_without_oauth(tmp_path, monkeypatch, capsys):
    import lenkobot.runtime as runtime

    config_path = write_config(tmp_path / "config")
    data_root = tmp_path / "smoke-state"
    observed = {}

    async def live_smoke(settings, bot_token, *, config_path, confirmed):
        observed["settings"] = settings
        observed["bot_token"] = bot_token
        observed["config_path"] = config_path
        observed["confirmed"] = confirmed
        return SimpleNamespace(command_count=6)

    monkeypatch.setattr(runtime, "run_live_smoke", live_smoke)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")

    result = main(
        [
            "live-smoke",
            "--config",
            str(config_path),
            "--data-root",
            str(data_root),
            "--confirm-send",
        ]
    )

    assert result == 0
    assert observed["settings"].data_root == data_root
    assert observed["bot_token"] == "telegram-secret"
    assert observed["config_path"] == config_path
    assert observed["confirmed"] is True
    assert capsys.readouterr().out == "Telegram live smoke completed: 6 commands delivered.\n"


def test_telegram_e2e_login_saves_only_successful_redacted_authorization():
    class RecordingStore:
        def __init__(self):
            self.saved = []

        def save(self, state):
            self.saved.append(state)

    store = RecordingStore()
    inputs = iter(("12345", "+10000000000"))
    secrets = iter(("a" * 32, "12345", "two-factor-secret"))
    output = []

    async def authorize(**kwargs):
        assert kwargs["api_id"] == 12345
        assert kwargs["api_hash"] == "a" * 32
        assert kwargs["phone"] == "+10000000000"
        assert kwargs["code_provider"]() == "12345"
        assert kwargs["password_provider"]() == "two-factor-secret"
        return TelegramE2ECredentialState(
            api_id=12345,
            api_hash="a" * 32,
            session="serialized-session-secret",
            user_id=555,
        )

    login_telegram_e2e(
        authorize=authorize,
        store=store,
        expected_user_id=555,
        input_value=lambda prompt: next(inputs),
        secret_input=lambda prompt: next(secrets),
        output=output.append,
    )

    assert len(store.saved) == 1
    assert output == ["Telegram E2E login completed for test user ID 555."]
    assert "serialized-session-secret" not in repr(output)
    assert "two-factor-secret" not in repr(output)


def test_telegram_e2e_login_does_not_overwrite_session_for_wrong_user():
    class RecordingStore:
        def __init__(self):
            self.saved = []

        def save(self, state):
            self.saved.append(state)

    store = RecordingStore()
    inputs = iter(("12345", "+10000000000"))
    secrets = iter(("a" * 32,))

    async def authorize(**kwargs):
        return TelegramE2ECredentialState(
            api_id=12345,
            api_hash="a" * 32,
            session="wrong-user-session-secret",
            user_id=999,
        )

    with pytest.raises(ValueError, match="configured test user"):
        login_telegram_e2e(
            authorize=authorize,
            store=store,
            expected_user_id=555,
            input_value=lambda prompt: next(inputs),
            secret_input=lambda prompt: next(secrets),
        )

    assert store.saved == []


def test_main_runs_telegram_e2e_and_prints_verified_replies(
    tmp_path,
    monkeypatch,
    capsys,
):
    import lenkobot.runtime as runtime

    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n\n[telegram_e2e]\n"
        + "bot_user_id = 777\n"
        + 'bot_username = "lenkobot_test_bot"\n',
        encoding="utf-8",
    )
    credentials = TelegramE2ECredentialState(
        api_id=12345,
        api_hash="a" * 32,
        session="serialized-session-secret",
        user_id=123456789,
    )

    class CredentialStore:
        def load(self):
            return credentials

    async def transport_factory(settings, state):
        raise AssertionError("run function is stubbed")

    async def e2e_run(settings, state, *, confirmed, transport_factory):
        assert settings.allowed_user_id == 123456789
        assert state == credentials
        assert confirmed is True
        return TelegramE2EReport(
            steps=(
                TelegramE2EStep("/start", "verified help reply"),
                TelegramE2EStep("/forget", "Удалено: запись <id>."),
            )
        )

    monkeypatch.setattr(
        runtime,
        "WindowsTelegramE2ECredentialStore",
        CredentialStore,
    )
    monkeypatch.setattr(
        runtime,
        "_load_telethon_e2e_adapters",
        lambda: (None, transport_factory),
    )
    monkeypatch.setattr(runtime, "run_telegram_e2e", e2e_run)

    result = main(
        [
            "telegram-e2e",
            "--config",
            str(config_path),
            "--confirm-send",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == (
        "/start -> verified help reply\n"
        "/forget -> Удалено: запись <id>.\n"
        "Telegram E2E completed: 2 replies received and verified.\n"
    )


def test_main_runs_correlated_e2e_bot_with_fresh_external_state(
    tmp_path,
    monkeypatch,
):
    import lenkobot.runtime as runtime
    from lenkobot.aiogram_adapter import AiogramTelegramReplyResponsePort

    config_directory = tmp_path / "config"
    config_path = write_config(config_directory)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n\n[telegram_e2e]\n"
        + "bot_user_id = 777\n"
        + 'bot_username = "lenkobot_test_bot"\n',
        encoding="utf-8",
    )
    data_root = tmp_path / "e2e-state"
    observed = {}

    async def run(settings, bot_token, *, response_port_factory, **kwargs):
        assert settings.data_root.is_dir()
        observed["settings"] = settings
        observed["bot_token"] = bot_token
        observed["response_port_factory"] = response_port_factory

    monkeypatch.setattr(runtime, "run_application", run)
    verified = []

    async def verify(bot_token, *, expected_bot_user_id):
        verified.append((bot_token, expected_bot_user_id))

    monkeypatch.setattr(runtime, "verify_bot_identity", verify)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")

    result = main(
        [
            "telegram-e2e-bot",
            "--config",
            str(config_path),
            "--data-root",
            str(data_root),
            "--confirm-run",
        ]
    )

    assert result == 0
    assert observed["settings"].data_root == data_root.resolve()
    assert observed["bot_token"] == "telegram-secret"
    assert observed["response_port_factory"] is AiogramTelegramReplyResponsePort
    assert verified == [("telegram-secret", 777)]


def test_chat_prints_only_final_responses_and_closes_application(tmp_path):
    from lenkobot.telegram_presentation import (
        TelegramResponse,
        TelegramResponseKind,
    )

    settings = load_runtime_settings(write_config(tmp_path))
    closed = []

    class FakeService:
        def __init__(self):
            self.messages = []

        async def handle(self, message, response_port=None):
            self.messages.append(message)
            await response_port.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.STATUS,
                    text="status text",
                )
            )
            await response_port.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text="answer text",
                )
            )

    class FakeApplication:
        def __init__(self, service):
            self.service = service

        def close(self):
            closed.append(True)

    service = FakeService()
    output = []

    asyncio.run(
        run_local_chat(
            settings,
            "hello",
            output=output.append,
            open_application=lambda current: FakeApplication(service),
        )
    )

    assert output == ["answer text"]
    assert closed == [True]
    assert [
        (message.user_id, message.chat_id, message.chat_type, message.text)
        for message in service.messages
    ] == [(123456789, 123456789, "private", "hello")]


def test_chat_rejects_empty_message_before_opening_application(tmp_path):
    settings = load_runtime_settings(write_config(tmp_path))

    def open_application(current):
        raise AssertionError("application must not open")

    with pytest.raises(ValueError, match="message"):
        asyncio.run(
            run_local_chat(
                settings,
                "   ",
                output=lambda text: None,
                open_application=open_application,
            )
        )


def test_chat_composes_oauth_only_service_and_preserves_conversation_across_invocations(
    tmp_path,
    monkeypatch,
):
    import lenkobot.runtime as runtime
    from lenkobot.xai_provider import XaiTextResponse

    class ExistingCredentialStore:
        target_name = "LenkoBot/xai-oauth/v1/default"

        def __init__(self, *args, **kwargs):
            pass

        def load(self):
            return token_state()

    class FakeMutex:
        def __init__(self, *args, **kwargs):
            pass

    class FakeProvider:
        supports_message_input = True
        prompts = []

        def __init__(self, *args, **kwargs):
            pass

        def respond(self, prompt):
            self.prompts.append(prompt)
            return XaiTextResponse(
                response_id="response-1",
                model="grok-4.5",
                text="local answer",
                credential_source="xai_oauth",
            )

    class FakeStructuredProvider:
        def __init__(self, *args, **kwargs):
            pass

        def respond(self, prompt, *, schema_name, schema):
            return SimpleNamespace(value={"candidates": []})

    monkeypatch.setattr(runtime, "WindowsOAuthCredentialStore", ExistingCredentialStore)
    monkeypatch.setattr(runtime, "WindowsOAuthRefreshMutex", FakeMutex)
    monkeypatch.setattr(runtime, "XaiProvider", FakeProvider)
    monkeypatch.setattr(runtime, "XaiStructuredProvider", FakeStructuredProvider)
    config_path = write_config(tmp_path)
    data_root = tmp_path / "chat-state"
    settings = load_runtime_settings(config_path, data_root=data_root)
    output = []

    asyncio.run(run_local_chat(settings, "hello", output=output.append))
    asyncio.run(run_local_chat(settings, "again", output=output.append))

    assert output == ["local answer", "local answer"]
    assert (data_root / "state.db").is_file()
    first_prompt, second_prompt = FakeProvider.prompts
    assert first_prompt[0].role == "system"
    assert [message.role for message in first_prompt] == ["system", "user"]
    second_roles = [message.role for message in second_prompt]
    assert second_roles[0] == "system"
    assert second_roles.count("user") >= 2
    assert "assistant" in second_roles


def test_main_runs_chat_with_explicit_data_root(tmp_path, monkeypatch):
    import lenkobot.runtime as runtime

    config_path = write_config(tmp_path / "config")
    data_root = tmp_path / "chat-state"
    observed = {}

    async def chat(settings, message, **kwargs):
        observed["settings"] = settings
        observed["message"] = message

    monkeypatch.setattr(runtime, "run_local_chat", chat)

    result = main(
        [
            "chat",
            "--config",
            str(config_path),
            "--data-root",
            str(data_root),
            "--message",
            "hello",
        ]
    )

    assert result == 0
    assert observed["settings"].data_root == data_root
    assert observed["message"] == "hello"
