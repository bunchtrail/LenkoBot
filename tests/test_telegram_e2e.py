import asyncio

import pytest

from lenkobot.telegram_e2e import (
    TelegramE2EError,
    TelegramE2EMessage,
    load_telegram_e2e_settings,
    prepare_telegram_e2e_bot_data_root,
    run_telegram_e2e,
    validate_telegram_e2e_bot_data_root,
)
from lenkobot.telegram_e2e_credentials import TelegramE2ECredentialState


def write_config(tmp_path, *, bot_user_id=777, bot_username="lenkobot_test_bot"):
    config_path = tmp_path / "config.e2e.toml"
    config_path.write_text(
        f"""
default_persona_key = "lenko"

[[personas]]
key = "lenko"
display_name = "Lenko"
identity_prompt = "Be concise."
identity_version = 1

[telegram]
allowed_user_id = 555

[telegram_e2e]
bot_user_id = {bot_user_id}
bot_username = "{bot_username}"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def credential_state(*, user_id=555):
    return TelegramE2ECredentialState(
        api_id=12345,
        api_hash="a" * 32,
        session="serialized-session-secret",
        user_id=user_id,
    )


class ScriptedTransport:
    def __init__(self, response_for):
        self._response_for = response_for
        self.commands = []
        self.closed = False

    async def exchange(self, command):
        self.commands.append(command)
        return TelegramE2EMessage(
            id=100 + len(self.commands),
            text=self._response_for(command),
        )

    async def close(self):
        self.closed = True


def successful_response_script():
    probe = None

    def response(command):
        nonlocal probe
        if command in {"/start", "/help"}:
            return (
                "Доступные команды:\n"
                "/start, /help — показать эту справку.\n"
                "/persona [key] — выбрать персону.\n"
                "/remember <text> — сохранить общую запись.\n"
                "/memories [page] — показать записи памяти.\n"
                "/forget <id> — удалить запись памяти."
            )
        if command == "/persona":
            return "Доступные персоны: lenko (Lenko)."
        if command.startswith("/remember "):
            probe = command.removeprefix("/remember ")
            return f"Запомнил: {probe}."
        if command == "/memories":
            return f"Память, страница 1:\n7. [shared] {probe}"
        if command == "/forget 7":
            return "Удалено: запись 7."
        raise AssertionError(f"unexpected command: {command}")

    return response


def test_telegram_e2e_receives_and_checks_real_command_reply_sequence(tmp_path):
    settings = load_telegram_e2e_settings(write_config(tmp_path))
    transport = ScriptedTransport(successful_response_script())
    factory_calls = []

    async def transport_factory(observed_settings, credentials):
        factory_calls.append((observed_settings, credentials))
        return transport

    report = asyncio.run(
        run_telegram_e2e(
            settings,
            credential_state(),
            confirmed=True,
            transport_factory=transport_factory,
            marker="run-123",
        )
    )

    assert factory_calls == [(settings, credential_state())]
    assert transport.commands == [
        "/start",
        "/help",
        "/persona",
        "/remember LenkoBot E2E run-123",
        "/memories",
        "/forget 7",
    ]
    assert transport.closed is True
    assert report.command_count == 6
    assert [step.command for step in report.steps] == [
        "/start",
        "/help",
        "/persona",
        "/remember",
        "/memories",
        "/forget",
    ]
    assert report.steps[3].response_text == "Запомнил: <probe>."
    assert "<id>. [shared] <probe>" in report.steps[4].response_text
    assert report.steps[5].response_text == "Удалено: запись <id>."


@pytest.mark.parametrize("case", ("unconfirmed", "wrong-user"))
def test_telegram_e2e_fails_before_transport_for_unsafe_identity(tmp_path, case):
    settings = load_telegram_e2e_settings(write_config(tmp_path))
    factory_called = False

    async def transport_factory(*args):
        nonlocal factory_called
        factory_called = True

    with pytest.raises(TelegramE2EError):
        asyncio.run(
            run_telegram_e2e(
                settings,
                credential_state(user_id=999 if case == "wrong-user" else 555),
                confirmed=case != "unconfirmed",
                transport_factory=transport_factory,
            )
        )

    assert factory_called is False


def test_telegram_e2e_stops_on_unexpected_reply_and_closes_transport(tmp_path):
    settings = load_telegram_e2e_settings(write_config(tmp_path))
    transport = ScriptedTransport(lambda command: "unexpected private reply")

    async def transport_factory(*args):
        return transport

    with pytest.raises(TelegramE2EError) as error:
        asyncio.run(
            run_telegram_e2e(
                settings,
                credential_state(),
                confirmed=True,
                transport_factory=transport_factory,
            )
        )

    assert transport.commands == ["/start"]
    assert transport.closed is True
    assert "unexpected private reply" not in str(error.value)


def test_telegram_e2e_rejects_help_reply_with_appended_private_text(tmp_path):
    settings = load_telegram_e2e_settings(write_config(tmp_path))
    base_response = successful_response_script()

    def response(command):
        result = base_response(command)
        if command == "/start":
            return result + "\nprivate-secret-from-wrong-state"
        return result

    transport = ScriptedTransport(response)

    async def transport_factory(*args):
        return transport

    with pytest.raises(TelegramE2EError) as error:
        asyncio.run(
            run_telegram_e2e(
                settings,
                credential_state(),
                confirmed=True,
                transport_factory=transport_factory,
            )
        )

    assert transport.commands == ["/start"]
    assert "private-secret-from-wrong-state" not in str(error.value)


def test_telegram_e2e_rejects_nonisolated_memory_reply_without_leaking_it(tmp_path):
    settings = load_telegram_e2e_settings(write_config(tmp_path))
    base_response = successful_response_script()

    def response(command):
        result = base_response(command)
        if command == "/memories":
            return result + "\n8. [shared] existing-private-memory"
        return result

    transport = ScriptedTransport(response)

    async def transport_factory(*args):
        return transport

    with pytest.raises(TelegramE2EError) as error:
        asyncio.run(
            run_telegram_e2e(
                settings,
                credential_state(),
                confirmed=True,
                transport_factory=transport_factory,
                marker="run-123",
            )
        )

    assert transport.commands[-1] == "/memories"
    assert transport.closed is True
    assert "existing-private-memory" not in str(error.value)


@pytest.mark.parametrize(
    ("bot_user_id", "bot_username"),
    ((555, "lenkobot_test_bot"), (777, "bad username")),
)
def test_telegram_e2e_config_rejects_unpinned_or_invalid_bot(
    tmp_path,
    bot_user_id,
    bot_username,
):
    with pytest.raises(ValueError):
        load_telegram_e2e_settings(
            write_config(
                tmp_path,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
            )
        )


@pytest.mark.parametrize("case", ("inside-config", "existing", "missing-parent"))
def test_e2e_bot_data_root_must_be_fresh_and_external(tmp_path, case):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    config_path = write_config(config_directory)
    data_root = tmp_path / "e2e-state"
    if case == "inside-config":
        data_root = config_directory / "e2e-state"
    elif case == "existing":
        data_root.mkdir()
    else:
        data_root = tmp_path / "missing" / "e2e-state"

    with pytest.raises(ValueError):
        validate_telegram_e2e_bot_data_root(
            data_root,
            config_path=config_path,
        )

    safe_root = tmp_path / f"safe-{case}"
    assert validate_telegram_e2e_bot_data_root(
        safe_root,
        config_path=config_path,
    ) == safe_root.resolve()

    prepared_root = prepare_telegram_e2e_bot_data_root(
        safe_root,
        config_path=config_path,
    )
    assert prepared_root == safe_root.resolve()
    assert prepared_root.is_dir()
