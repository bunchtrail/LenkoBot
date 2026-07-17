import asyncio
from types import SimpleNamespace

import lenkobot.aiogram_adapter as aiogram_adapter
from lenkobot.aiogram_adapter import (
    AiogramTelegramAdapter,
    AiogramTelegramResponsePort,
    create_dispatcher,
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
