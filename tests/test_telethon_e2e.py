import asyncio
from types import SimpleNamespace

import pytest

from lenkobot.telegram_e2e import (
    TelegramE2EError,
    TelegramE2ESettings,
)
from lenkobot.telegram_e2e_credentials import TelegramE2ECredentialState
from lenkobot.telethon_e2e import (
    TelethonBindings,
    authorize_telethon_user,
    open_telethon_transport,
)


class PasswordNeededError(Exception):
    pass


class FakeStringSession:
    def __init__(self, value=""):
        self.value = value

    def save(self):
        return self.value or "new-serialized-session"


def settings():
    return TelegramE2ESettings(
        allowed_user_id=555,
        bot_user_id=777,
        bot_username="lenkobot_test_bot",
        persona_display_names=(("lenko", "Lenko"),),
    )


def credential_state():
    return TelegramE2ECredentialState(
        api_id=12345,
        api_hash="a" * 32,
        session="serialized-session-secret",
        user_id=555,
    )


def test_telethon_authorization_handles_code_and_2fa_without_exposing_secrets():
    client = AuthorizationClient(require_password=True)
    bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )

    state = asyncio.run(
        authorize_telethon_user(
            api_id=12345,
            api_hash="a" * 32,
            phone="+10000000000",
            code_provider=lambda: "12345",
            password_provider=lambda: "two-factor-secret",
            bindings=bindings,
        )
    )

    assert state.user_id == 555
    assert state.session == "new-serialized-session"
    assert client.sign_ins == [
        {
            "phone": "+10000000000",
            "code": "12345",
            "phone_code_hash": "phone-code-hash",
        },
        {"password": "two-factor-secret"},
    ]
    assert client.disconnected is True
    assert "two-factor-secret" not in repr(state)
    assert "new-serialized-session" not in repr(state)


def test_telethon_transport_pins_identities_and_receives_new_bot_reply():
    client = RuntimeClient()
    bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )

    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=bindings,
            timeout_seconds=0.1,
            quiet_seconds=0,
        )
    )
    message = asyncio.run(transport.exchange("/start"))
    asyncio.run(transport.close())

    assert message.id == 12
    assert message.text == "bot reply"
    assert client.sent == [(client.bot, "/start")]
    assert client.event_filters == [
        {"chats": client.bot}
    ]
    assert client.disconnected is True


def test_telethon_transport_rejects_wrong_bot_or_message_identity():
    wrong_bot_client = RuntimeClient(bot_id=778)
    wrong_bot_bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: wrong_bot_client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )

    with pytest.raises(TelegramE2EError):
        asyncio.run(
            open_telethon_transport(
                settings(),
                credential_state(),
                bindings=wrong_bot_bindings,
            )
        )
    assert wrong_bot_client.disconnected is True

    wrong_message_client = RuntimeClient(message_sender_id=999)
    wrong_message_bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: wrong_message_client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )
    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=wrong_message_bindings,
            timeout_seconds=0.1,
            quiet_seconds=0,
        )
    )
    with pytest.raises(TelegramE2EError):
        asyncio.run(transport.exchange("/start"))
    asyncio.run(transport.close())

    stale_reply_client = RuntimeClient(reply_to_message_id=9)
    stale_reply_bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: stale_reply_client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )
    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=stale_reply_bindings,
            timeout_seconds=0.1,
            quiet_seconds=0,
        )
    )
    with pytest.raises(TelegramE2EError):
        asyncio.run(transport.exchange("/start"))
    asyncio.run(transport.close())


@pytest.mark.parametrize("mode", ("duplicate", "timeout"))
def test_telethon_transport_rejects_duplicate_or_missing_reply(mode):
    client = RuntimeClient(
        message_count=2 if mode == "duplicate" else 0,
    )
    bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )
    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=bindings,
            timeout_seconds=0.01,
            quiet_seconds=0,
        )
    )

    with pytest.raises(TelegramE2EError):
        asyncio.run(transport.exchange("/start"))
    asyncio.run(transport.close())


def test_telethon_transport_rejects_delayed_reply_before_next_send():
    client = RuntimeClient(stale_event_on_second_lookup=True)
    bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )
    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=bindings,
            timeout_seconds=0.1,
            quiet_seconds=0,
        )
    )

    with pytest.raises(TelegramE2EError):
        asyncio.run(transport.exchange("/start"))
    assert client.sent == []
    asyncio.run(transport.close())


def test_telethon_transport_rejects_concurrent_outgoing_activity():
    client = RuntimeClient(concurrent_outgoing_message=True)
    bindings = TelethonBindings(
        client_factory=lambda session, api_id, api_hash: client,
        string_session_factory=FakeStringSession,
        new_message_factory=lambda **kwargs: kwargs,
        password_needed_error=PasswordNeededError,
    )
    transport = asyncio.run(
        open_telethon_transport(
            settings(),
            credential_state(),
            bindings=bindings,
            timeout_seconds=0.1,
            quiet_seconds=0,
        )
    )

    with pytest.raises(TelegramE2EError):
        asyncio.run(transport.exchange("/start"))
    asyncio.run(transport.close())


class AuthorizationClient:
    def __init__(self, *, require_password):
        self.require_password = require_password
        self.session = FakeStringSession()
        self.sign_ins = []
        self.disconnected = False

    async def connect(self):
        return None

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="phone-code-hash")

    async def sign_in(self, **kwargs):
        self.sign_ins.append(kwargs)
        if self.require_password and "password" not in kwargs:
            raise PasswordNeededError
        return None

    async def get_me(self):
        return SimpleNamespace(id=555, bot=False)

    async def disconnect(self):
        self.disconnected = True


class RuntimeClient:
    def __init__(
        self,
        *,
        bot_id=777,
        message_sender_id=777,
        message_count=1,
        reply_to_message_id=11,
        stale_event_on_second_lookup=False,
        concurrent_outgoing_message=False,
    ):
        self.me = SimpleNamespace(id=555, bot=False)
        self.bot = SimpleNamespace(
            id=bot_id,
            bot=True,
            username="lenkobot_test_bot",
        )
        self.message_sender_id = message_sender_id
        self.message_count = message_count
        self.reply_to_message_id = reply_to_message_id
        self.stale_event_on_second_lookup = stale_event_on_second_lookup
        self.concurrent_outgoing_message = concurrent_outgoing_message
        self.message_lookups = 0
        self.handlers = []
        self.event_filters = []
        self.sent = []
        self.disconnected = False

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, username):
        return self.bot

    async def get_messages(self, entity, *, limit):
        self.message_lookups += 1
        if self.stale_event_on_second_lookup and self.message_lookups == 2:
            event = SimpleNamespace(
                id=11,
                sender_id=777,
                chat_id=777,
                out=False,
                raw_text="delayed previous reply",
                reply_to_msg_id=9,
            )
            for handler in tuple(self.handlers):
                await handler(event)
        return [SimpleNamespace(id=10)]

    def add_event_handler(self, handler, event_filter):
        self.handlers.append(handler)
        self.event_filters.append(event_filter)

    def remove_event_handler(self, handler, event_filter):
        self.handlers.remove(handler)

    async def send_message(self, entity, text):
        self.sent.append((entity, text))
        if self.concurrent_outgoing_message:
            concurrent = SimpleNamespace(
                id=13,
                sender_id=555,
                chat_id=777,
                out=True,
                raw_text="manual concurrent message",
                reply_to_msg_id=None,
            )
            for handler in tuple(self.handlers):
                await handler(concurrent)
        for index in range(self.message_count):
            event = SimpleNamespace(
                id=12 + index,
                sender_id=self.message_sender_id,
                chat_id=777,
                out=False,
                raw_text="bot reply",
                reply_to_msg_id=self.reply_to_message_id,
            )
            for handler in tuple(self.handlers):
                await handler(event)
        return SimpleNamespace(id=11)

    async def disconnect(self):
        self.disconnected = True
