import asyncio
from types import SimpleNamespace

import lenkobot.aiogram_adapter as aiogram_adapter
import pytest
from lenkobot.aiogram_adapter import (
    AiogramBotResponsePort,
    AiogramTelegramAdapter,
    AiogramTelegramReplyResponsePort,
    AiogramTelegramResponsePort,
    TelegramDeliveryError,
    create_dispatcher,
    run_bot_delivery,
    verify_bot_identity,
)
from lenkobot.telegram_router import IncomingTelegramMessage
from lenkobot.telegram_presentation import (
    TelegramResponse,
    TelegramResponseKind,
)


class RecordingRouter:
    def __init__(self):
        self.messages = []

    def handle(self, message):
        self.messages.append(message)


def telegram_message(*, user_id=42, chat_id=500, chat_type="private", text="hello"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id) if user_id is not None else None,
        chat=SimpleNamespace(id=chat_id, type=chat_type) if chat_id is not None else None,
        text=text,
    )


def test_adapter_maps_private_text_message_to_domain_message():
    router = RecordingRouter()
    adapter = AiogramTelegramAdapter(router)

    asyncio.run(adapter.handle_message(telegram_message(text="check this")))

    assert router.messages == [
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="private",
            text="check this",
        )
    ]


def test_adapter_ignores_updates_without_user_chat_or_text():
    router = RecordingRouter()
    adapter = AiogramTelegramAdapter(router)

    for message in (
        telegram_message(user_id=None),
        telegram_message(chat_id=None),
        telegram_message(text=None),
    ):
        asyncio.run(adapter.handle_message(message))

    assert router.messages == []


def test_adapter_preserves_chat_type_for_domain_authorization():
    router = RecordingRouter()
    adapter = AiogramTelegramAdapter(router)

    asyncio.run(adapter.handle_message(telegram_message(chat_type="group")))

    assert router.messages == [
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="group",
            text="hello",
        )
    ]


def test_dispatcher_registers_only_message_ingress_for_mvp():
    dispatcher = create_dispatcher(RecordingRouter())

    assert dispatcher.resolve_used_update_types() == ["message"]


def test_run_polling_uses_the_dispatchers_registered_update_types(monkeypatch):
    router = RecordingRouter()
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(aiogram_adapter, "Bot", RecordingBot)
    monkeypatch.setattr(aiogram_adapter, "create_dispatcher", lambda _: dispatcher)

    asyncio.run(aiogram_adapter.run_polling("123:token", router))

    assert dispatcher.started_with == (RecordingBot.instances[0], ["message"])
    assert RecordingBot.instances[0].token == "123:token"


def test_adapter_binds_aiogram_message_to_application_response_port():
    service = RecordingApplicationService()
    message = answering_telegram_message()
    adapter = AiogramTelegramAdapter(
        service,
        response_port_factory=AiogramTelegramResponsePort,
    )

    asyncio.run(adapter.handle_message(message))

    assert service.messages == [
        IncomingTelegramMessage(
            user_id=42,
            chat_id=500,
            chat_type="private",
            text="hello",
        )
    ]
    assert message.answers == ["done"]


def test_bot_response_port_is_fixed_to_owner_and_marks_smoke_messages():
    bot = RecordingSendBot()
    port = AiogramBotResponsePort(bot, target_chat_id=42)

    asyncio.run(
        port.send(
            TelegramResponse(
                chat_id=42,
                kind=TelegramResponseKind.FINAL,
                text="check complete",
            )
        )
    )

    assert bot.messages == [(42, "[SMOKE] check complete")]
    with pytest.raises(ValueError, match="target"):
        asyncio.run(
            port.send(
                TelegramResponse(
                    chat_id=99,
                    kind=TelegramResponseKind.FINAL,
                    text="wrong chat",
                )
            )
        )
    assert bot.messages == [(42, "[SMOKE] check complete")]


def test_e2e_reply_response_port_correlates_to_source_message():
    message = replying_telegram_message()
    port = AiogramTelegramReplyResponsePort(message)

    asyncio.run(
        port.send(
            TelegramResponse(
                chat_id=500,
                kind=TelegramResponseKind.FINAL,
                text="correlated reply",
            )
        )
    )

    assert len(message.answers) == 1
    text, reply_parameters = message.answers[0]
    assert text == "correlated reply"
    assert reply_parameters.message_id == 321
    with pytest.raises(ValueError, match="target"):
        asyncio.run(
            port.send(
                TelegramResponse(
                    chat_id=99,
                    kind=TelegramResponseKind.FINAL,
                    text="wrong chat",
                )
            )
        )
    assert len(message.answers) == 1


def test_bot_delivery_verifies_identity_and_redacts_transport_errors(monkeypatch):
    RecordingDirectBot.instances = []
    monkeypatch.setattr(aiogram_adapter, "Bot", RecordingDirectBot)

    async def action(port):
        await port.send(
            TelegramResponse(
                chat_id=42,
                kind=TelegramResponseKind.FINAL,
                text="done",
            )
        )
        return "result"

    result = asyncio.run(run_bot_delivery("123:token", 42, action))

    assert result == "result"
    assert RecordingDirectBot.instances[0].events == [
        "enter",
        "get_me",
        ("send_message", 42, "[SMOKE] done"),
        "exit",
    ]

    class FailingIdentityBot(RecordingDirectBot):
        async def get_me(self):
            raise RuntimeError("123:token")

    monkeypatch.setattr(aiogram_adapter, "Bot", FailingIdentityBot)
    with pytest.raises(TelegramDeliveryError) as error:
        asyncio.run(run_bot_delivery("123:token", 42, action))
    assert "123:token" not in str(error.value)


def test_bot_delivery_redacts_initialization_and_send_errors(monkeypatch):
    class FailingInitializationBot:
        def __init__(self, token):
            raise RuntimeError(token)

    monkeypatch.setattr(aiogram_adapter, "Bot", FailingInitializationBot)

    async def unused_action(port):
        raise AssertionError("action must not run")

    with pytest.raises(TelegramDeliveryError) as initialization_error:
        asyncio.run(run_bot_delivery("123:token", 42, unused_action))
    assert "123:token" not in str(initialization_error.value)

    class FailingSendBot:
        async def send_message(self, *, chat_id, text):
            raise RuntimeError("transport-private-data")

    port = AiogramBotResponsePort(FailingSendBot(), target_chat_id=42)
    with pytest.raises(TelegramDeliveryError) as send_error:
        asyncio.run(
            port.send(
                TelegramResponse(
                    chat_id=42,
                    kind=TelegramResponseKind.FINAL,
                    text="done",
                )
            )
        )
    assert "transport-private-data" not in str(send_error.value)


def test_bot_identity_verification_pins_expected_bot_id(monkeypatch):
    RecordingDirectBot.instances = []
    monkeypatch.setattr(aiogram_adapter, "Bot", RecordingDirectBot)

    asyncio.run(verify_bot_identity("123:token", expected_bot_user_id=1))

    assert RecordingDirectBot.instances[0].events == [
        "enter",
        "get_me",
        "exit",
    ]
    with pytest.raises(TelegramDeliveryError, match="identity mismatch"):
        asyncio.run(verify_bot_identity("123:token", expected_bot_user_id=2))


class RecordingDispatcher:
    def __init__(self):
        self.started_with = None

    def resolve_used_update_types(self):
        return ["message"]

    async def start_polling(self, bot, *, allowed_updates):
        self.started_with = (bot, allowed_updates)


class RecordingBot:
    instances = []

    def __init__(self, token):
        self.token = token
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class RecordingSendBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, *, chat_id, text):
        self.messages.append((chat_id, text))


class RecordingDirectBot(RecordingSendBot):
    instances = []

    def __init__(self, token):
        super().__init__()
        self.token = token
        self.events = []
        self.instances.append(self)

    async def __aenter__(self):
        self.events.append("enter")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.events.append("exit")

    async def get_me(self):
        self.events.append("get_me")
        return SimpleNamespace(id=1, username="lenko_test_bot")

    async def send_message(self, *, chat_id, text):
        self.events.append(("send_message", chat_id, text))
        await super().send_message(chat_id=chat_id, text=text)


class RecordingApplicationService:
    def __init__(self):
        self.messages = []

    async def handle(self, message, response_port):
        self.messages.append(message)
        await response_port.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text="done",
            )
        )


def answering_telegram_message():
    message = telegram_message()
    message.answers = []

    async def answer(text):
        message.answers.append(text)

    message.answer = answer
    return message


def replying_telegram_message():
    message = telegram_message()
    message.message_id = 321
    message.answers = []

    async def answer(text, *, reply_parameters):
        message.answers.append((text, reply_parameters))

    message.answer = answer
    return message
