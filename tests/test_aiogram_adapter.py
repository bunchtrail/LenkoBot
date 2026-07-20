import asyncio
from types import SimpleNamespace

import lenkobot.aiogram_adapter as aiogram_adapter
import pytest
from aiogram.exceptions import TelegramBadRequest
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
    TELEGRAM_COMMANDS,
    TelegramResponse,
    TelegramResponseKind,
    TelegramParseMode,
    TelegramInlineButton,
    TelegramSentMessage,
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


def test_dispatcher_registers_callback_ingress_for_application_handler():
    dispatcher = create_dispatcher(RecordingApplicationService())

    assert dispatcher.resolve_used_update_types() == ["callback_query", "message"]


def test_run_polling_uses_the_dispatchers_registered_update_types(monkeypatch):
    router = RecordingRouter()
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(aiogram_adapter, "Bot", RecordingBot)
    monkeypatch.setattr(aiogram_adapter, "create_dispatcher", lambda _: dispatcher)

    asyncio.run(aiogram_adapter.run_polling("123:token", router))

    assert dispatcher.started_with == (RecordingBot.instances[0], ["message"])
    assert RecordingBot.instances[0].token == "123:token"


def test_run_polling_registers_owner_scoped_command_menu_before_polling(monkeypatch):
    router = RecordingRouter()
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(aiogram_adapter, "Bot", RecordingBot)
    monkeypatch.setattr(
        aiogram_adapter,
        "create_dispatcher",
        lambda router, **kwargs: dispatcher,
    )

    asyncio.run(
        aiogram_adapter.run_polling(
            "123:token",
            router,
            command_scope_chat_id=42,
        )
    )

    bot = RecordingBot.instances[-1]
    assert bot.command_menu_scope.chat_id == 42
    assert [command.command for command in bot.command_menu] == [
        item.command for item in TELEGRAM_COMMANDS
    ]
    assert dispatcher.started_with == (bot, ["message"])


def test_run_polling_fails_before_polling_when_command_menu_registration_fails(
    monkeypatch,
):
    router = RecordingRouter()
    dispatcher = RecordingDispatcher()

    class FailingCommandBot(RecordingBot):
        async def set_my_commands(self, commands, *, scope):
            raise RuntimeError("telegram-token-secret")

    monkeypatch.setattr(aiogram_adapter, "Bot", FailingCommandBot)
    monkeypatch.setattr(
        aiogram_adapter,
        "create_dispatcher",
        lambda router, **kwargs: dispatcher,
    )

    with pytest.raises(TelegramDeliveryError, match="command menu") as error:
        asyncio.run(
            aiogram_adapter.run_polling(
                "123:token",
                router,
                command_scope_chat_id=42,
            )
        )

    assert "telegram-token-secret" not in str(error.value)
    assert dispatcher.started_with is None


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


def test_adapter_maps_inline_keyboard_to_aiogram_markup():
    service = KeyboardApplicationService()
    message = answering_telegram_message_with_markup()
    adapter = AiogramTelegramAdapter(
        service,
        response_port_factory=AiogramTelegramResponsePort,
    )

    asyncio.run(adapter.handle_message(message))

    assert message.answers[0][0] == "done"
    markup = message.answers[0][1]
    assert markup.inline_keyboard[0][0].text == "Analyst"
    assert markup.inline_keyboard[0][0].callback_data == "persona:v1:analyst"


def test_adapter_maps_and_acknowledges_callback_query():
    service = RecordingApplicationService()
    callback = answering_callback_query()
    adapter = AiogramTelegramAdapter(
        service,
        response_port_factory=AiogramTelegramResponsePort,
    )

    asyncio.run(adapter.handle_callback(callback))

    assert callback.answers == [None]
    assert service.callbacks[0].data == "persona:v1:analyst"


def test_adapter_acknowledges_callback_without_message():
    service = RecordingApplicationService()
    callback = answering_callback_query(message=None)
    adapter = AiogramTelegramAdapter(service)

    asyncio.run(adapter.handle_callback(callback))

    assert callback.answers == [None]
    assert service.callbacks == []


def test_adapter_acknowledges_callback_when_handler_fails():
    class FailingCallbackService(RecordingApplicationService):
        async def handle_callback(self, callback, response_port=None):
            raise RuntimeError("provider-secret")

    callback = answering_callback_query()
    adapter = AiogramTelegramAdapter(FailingCallbackService())

    with pytest.raises(RuntimeError, match="provider-secret"):
        asyncio.run(adapter.handle_callback(callback))

    assert callback.answers == [None]


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
        self.command_menu = None
        self.command_menu_scope = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def set_my_commands(self, commands, *, scope):
        self.command_menu = commands
        self.command_menu_scope = scope


class RecordingSendBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, *, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=1)


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

    async def send_message(self, *, chat_id, text, reply_markup=None):
        self.events.append(("send_message", chat_id, text))
        await super().send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return SimpleNamespace(message_id=1)


class RecordingApplicationService:
    def __init__(self):
        self.messages = []
        self.callbacks = []

    async def handle(self, message, response_port):
        self.messages.append(message)
        await response_port.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text="done",
            )
        )

    async def handle_callback(self, callback, response_port):
        self.callbacks.append(callback)


class KeyboardApplicationService(RecordingApplicationService):
    async def handle(self, message, response_port):
        await response_port.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text="done",
                inline_keyboard=(
                    (
                        TelegramInlineButton(
                            text="Analyst",
                            callback_data="persona:v1:analyst",
                        ),
                    ),
                ),
            )
        )


def answering_telegram_message():
    message = telegram_message()
    message.answers = []

    async def answer(text):
        message.answers.append(text)
        return SimpleNamespace(message_id=1)

    message.answer = answer
    return message


def answering_telegram_message_with_markup():
    message = telegram_message()
    message.answers = []

    async def answer(text, *, reply_markup=None):
        message.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=1)

    message.answer = answer
    return message


def answering_callback_query(*, message="sentinel"):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        message=(telegram_message() if message == "sentinel" else message),
        data="persona:v1:analyst",
        answers=[],
    )

    async def answer(text=None, **kwargs):
        callback.answers.append(text)

    callback.answer = answer
    return callback


def replying_telegram_message():
    message = telegram_message()
    message.message_id = 321
    message.answers = []

    async def answer(text, *, reply_parameters):
        message.answers.append((text, reply_parameters))
        return SimpleNamespace(message_id=322)

    message.answer = answer
    return message


class RecordingBotAPI:
    def __init__(self):
        self.edits = []
        self.chat_actions = []
        self.edit_parse_modes = []
        self.edit_error = None

    async def edit_message_text(
        self,
        *,
        text,
        chat_id,
        message_id,
        reply_markup=None,
        parse_mode=None,
    ):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((text, chat_id, message_id, reply_markup))
        self.edit_parse_modes.append(parse_mode)
        return True

    async def send_chat_action(self, *, chat_id, action):
        self.chat_actions.append((chat_id, action))


def editable_telegram_message(*, bot=None, message_id=10):
    message = telegram_message()
    message.message_id = message_id
    message.bot = bot if bot is not None else RecordingBotAPI()
    message.sent = []
    message.parse_modes = []

    async def answer(text, *, reply_markup=None, parse_mode=None):
        sent = SimpleNamespace(message_id=100 + len(message.sent))
        message.sent.append((text, reply_markup, sent.message_id))
        message.parse_modes.append(parse_mode)
        return sent

    message.answer = answer
    return message


def test_port_send_returns_handle_and_bound_handle_points_at_source_message():
    message = editable_telegram_message()
    port = AiogramTelegramResponsePort(message)

    handle = asyncio.run(
        port.send(
            TelegramResponse(
                chat_id=500,
                kind=TelegramResponseKind.FINAL,
                text="done",
            )
        )
    )

    assert handle == TelegramSentMessage(chat_id=500, message_id=100)
    assert port.bound_handle() == TelegramSentMessage(chat_id=500, message_id=10)


def test_port_edit_maps_text_and_markup_to_bot_api():
    bot = RecordingBotAPI()
    port = AiogramTelegramResponsePort(editable_telegram_message(bot=bot))
    handle = TelegramSentMessage(chat_id=500, message_id=100)

    result = asyncio.run(
        port.edit(
            handle,
            TelegramResponse(
                chat_id=500,
                kind=TelegramResponseKind.FINAL,
                text="updated",
                inline_keyboard=(
                    (TelegramInlineButton(text="A", callback_data="mem:v1:2"),),
                ),
            ),
        )
    )

    assert result is True
    text, chat_id, message_id, markup = bot.edits[0]
    assert (text, chat_id, message_id) == ("updated", 500, 100)
    assert markup.inline_keyboard[0][0].callback_data == "mem:v1:2"


def test_port_passes_explicit_html_parse_mode_on_send_and_edit():
    bot = RecordingBotAPI()
    message = editable_telegram_message(bot=bot)
    port = AiogramTelegramResponsePort(message)
    response = TelegramResponse(
        chat_id=500,
        kind=TelegramResponseKind.FINAL,
        text='<b>Источники:</b>\n1. <a href="https://example.com">Example</a>',
        parse_mode=TelegramParseMode.HTML,
    )

    asyncio.run(port.send(response))
    edited = asyncio.run(
        port.edit(TelegramSentMessage(chat_id=500, message_id=100), response)
    )

    assert message.parse_modes == ["HTML"]
    assert edited is True
    assert bot.edit_parse_modes == ["HTML"]


def test_port_edit_treats_not_modified_as_successful_noop():
    bot = RecordingBotAPI()
    bot.edit_error = TelegramBadRequest(
        method=None,
        message="Bad Request: message is not modified",
    )
    port = AiogramTelegramResponsePort(editable_telegram_message(bot=bot))

    result = asyncio.run(
        port.edit(
            TelegramSentMessage(chat_id=500, message_id=100),
            TelegramResponse(chat_id=500, kind=TelegramResponseKind.FINAL, text="same"),
        )
    )

    assert result is True


def test_port_edit_returns_false_when_message_cannot_be_edited():
    for error in (
        TelegramBadRequest(method=None, message="Bad Request: message to edit not found"),
        RuntimeError("network down"),
    ):
        bot = RecordingBotAPI()
        bot.edit_error = error
        port = AiogramTelegramResponsePort(editable_telegram_message(bot=bot))

        result = asyncio.run(
            port.edit(
                TelegramSentMessage(chat_id=500, message_id=100),
                TelegramResponse(
                    chat_id=500,
                    kind=TelegramResponseKind.FINAL,
                    text="updated",
                ),
            )
        )

        assert result is False


def test_port_shows_typing_while_status_is_open_and_stops_on_final():
    async def scenario():
        bot = RecordingBotAPI()
        port = AiogramTelegramResponsePort(
            editable_telegram_message(bot=bot),
            typing_interval_seconds=0.01,
        )
        await port.send(
            TelegramResponse(chat_id=500, kind=TelegramResponseKind.STATUS, text="сек")
        )
        await asyncio.sleep(0.05)
        await port.send(
            TelegramResponse(chat_id=500, kind=TelegramResponseKind.FINAL, text="done")
        )
        actions_at_final = len(bot.chat_actions)
        await asyncio.sleep(0.05)
        return bot, actions_at_final

    bot, actions_at_final = asyncio.run(scenario())

    assert actions_at_final > 0
    assert all(action == ("500", "typing") or action == (500, "typing") for action in bot.chat_actions)
    assert len(bot.chat_actions) == actions_at_final


def test_bot_delivery_port_supports_edit_but_has_no_bound_handle():
    class DeliveryBot:
        def __init__(self):
            self.sent = []
            self.edits = []

        async def send_message(self, *, chat_id, text, reply_markup=None):
            self.sent.append((chat_id, text))
            return SimpleNamespace(message_id=77)

        async def edit_message_text(self, *, text, chat_id, message_id, reply_markup=None):
            self.edits.append((text, chat_id, message_id))
            return True

    bot = DeliveryBot()
    port = AiogramBotResponsePort(bot, target_chat_id=500)

    handle = asyncio.run(
        port.send(TelegramResponse(chat_id=500, kind=TelegramResponseKind.FINAL, text="x"))
    )
    edited = asyncio.run(
        port.edit(
            handle,
            TelegramResponse(chat_id=500, kind=TelegramResponseKind.FINAL, text="y"),
        )
    )

    assert handle == TelegramSentMessage(chat_id=500, message_id=77)
    assert edited is True
    assert bot.edits == [("y", 500, 77)]
    assert port.bound_handle() is None
