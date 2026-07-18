import asyncio

import pytest

from lenkobot.live_smoke import LiveSmokeError, run_live_smoke
from lenkobot.memory import SQLiteMemoryStore
from lenkobot.runtime import load_runtime_settings
from lenkobot.telegram_presentation import TelegramResponseKind


def write_config(directory):
    directory.mkdir()
    config_path = directory / "lenkobot.toml"
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
""".strip(),
        encoding="utf-8",
    )
    return config_path


class RecordingResponsePort:
    def __init__(self):
        self.responses = []

    async def send(self, response):
        self.responses.append(response)


def test_live_smoke_runs_owner_commands_in_fresh_state_and_deletes_probe(tmp_path):
    config_path = write_config(tmp_path / "config")
    data_root = tmp_path / "smoke-state"
    settings = load_runtime_settings(config_path, data_root=data_root)
    response_port = RecordingResponsePort()
    delivery_calls = []

    async def delivery(bot_token, target_chat_id, action):
        assert (data_root / "state.db").is_file()
        delivery_calls.append((bot_token, target_chat_id))
        return await action(response_port)

    report = asyncio.run(
        run_live_smoke(
            settings,
            "telegram-secret",
            config_path=config_path,
            confirmed=True,
            delivery=delivery,
            marker="run-123",
        )
    )

    assert delivery_calls == [("telegram-secret", 123456789)]
    assert report.commands == (
        "/start",
        "/help",
        "/persona",
        "/remember",
        "/memories",
        "/forget",
    )
    assert report.command_count == 6
    assert [response.kind for response in response_port.responses] == [
        TelegramResponseKind.FINAL
    ] * 6
    assert all(response.chat_id == 123456789 for response in response_port.responses)
    assert "/remember <text>" in response_port.responses[0].text
    assert "Доступные персоны: lenko (Lenko)." == response_port.responses[2].text
    assert response_port.responses[3].text == "Запомнил: LenkoBot smoke run-123."
    assert "[shared] LenkoBot smoke run-123" in response_port.responses[4].text
    assert response_port.responses[5].text == "Удалено: запись 1."

    memory_store = SQLiteMemoryStore(data_root / "state.db")
    try:
        assert memory_store.list_for_user(
            user_id=123456789,
            page=1,
            page_size=5,
        ) == ()
    finally:
        memory_store.close()


@pytest.mark.parametrize(
    "case",
    ("unconfirmed", "inside-config", "existing-root", "missing-parent"),
)
def test_live_smoke_rejects_unsafe_start_before_bot_delivery(tmp_path, case):
    config_path = write_config(tmp_path / "config")
    data_root = tmp_path / "smoke-state"
    confirmed = True
    if case == "unconfirmed":
        confirmed = False
    elif case == "inside-config":
        data_root = config_path.parent / "smoke-state"
    elif case == "existing-root":
        data_root.mkdir()
    else:
        data_root = tmp_path / "missing-parent" / "smoke-state"
    settings = load_runtime_settings(config_path, data_root=data_root)
    delivery_called = False

    async def delivery(*args):
        nonlocal delivery_called
        delivery_called = True

    with pytest.raises(LiveSmokeError):
        asyncio.run(
            run_live_smoke(
                settings,
                "telegram-secret",
                config_path=config_path,
                confirmed=confirmed,
                delivery=delivery,
            )
        )

    assert delivery_called is False


def test_live_smoke_stops_after_delivery_failure_and_keeps_diagnostic_state(tmp_path):
    config_path = write_config(tmp_path / "config")
    data_root = tmp_path / "smoke-state"
    settings = load_runtime_settings(config_path, data_root=data_root)

    class FailingResponsePort(RecordingResponsePort):
        async def send(self, response):
            await super().send(response)
            if len(self.responses) == 4:
                raise RuntimeError("telegram transport failed")

    response_port = FailingResponsePort()

    async def delivery(bot_token, target_chat_id, action):
        return await action(response_port)

    with pytest.raises(RuntimeError, match="telegram transport failed"):
        asyncio.run(
            run_live_smoke(
                settings,
                "telegram-secret",
                config_path=config_path,
                confirmed=True,
                delivery=delivery,
                marker="failed-run",
            )
        )

    assert len(response_port.responses) == 4
    memory_store = SQLiteMemoryStore(data_root / "state.db")
    try:
        records = memory_store.list_for_user(
            user_id=123456789,
            page=1,
            page_size=5,
        )
        assert [record.content for record in records] == [
            "LenkoBot smoke failed-run"
        ]
    finally:
        memory_store.close()
