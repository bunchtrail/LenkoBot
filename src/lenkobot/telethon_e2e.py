import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .telegram_e2e import (
    TelegramE2EError,
    TelegramE2EMessage,
    TelegramE2ESettings,
    TelegramE2ETransport,
)
from .telegram_e2e_credentials import TelegramE2ECredentialState


@dataclass(frozen=True, slots=True)
class TelethonBindings:
    client_factory: Callable[..., Any]
    string_session_factory: Callable[..., Any]
    new_message_factory: Callable[..., Any]
    password_needed_error: type[Exception]


def load_telethon_bindings() -> TelethonBindings:
    try:
        from telethon import TelegramClient, events
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import StringSession
    except ImportError:
        raise TelegramE2EError(
            "Telethon E2E dependency is unavailable; install the e2e extra"
        ) from None
    return TelethonBindings(
        client_factory=TelegramClient,
        string_session_factory=StringSession,
        new_message_factory=events.NewMessage,
        password_needed_error=SessionPasswordNeededError,
    )


async def authorize_telethon_user(
    *,
    api_id: int,
    api_hash: str,
    phone: str,
    code_provider: Callable[[], str],
    password_provider: Callable[[], str],
    bindings: TelethonBindings | None = None,
) -> TelegramE2ECredentialState:
    selected_bindings = bindings or load_telethon_bindings()
    try:
        client = selected_bindings.client_factory(
            selected_bindings.string_session_factory(),
            api_id,
            api_hash,
        )
    except Exception:
        raise TelegramE2EError("Telegram E2E login could not initialize") from None
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone)
        try:
            await client.sign_in(
                phone=phone,
                code=code_provider(),
                phone_code_hash=sent_code.phone_code_hash,
            )
        except selected_bindings.password_needed_error:
            await client.sign_in(password=password_provider())
        user = await client.get_me()
        if user is None or getattr(user, "bot", None) is not False:
            raise TelegramE2EError("Telegram E2E login did not authorize a user")
        session = client.session.save()
        return TelegramE2ECredentialState(
            api_id=api_id,
            api_hash=api_hash,
            session=session,
            user_id=user.id,
        )
    except TelegramE2EError:
        raise
    except Exception:
        raise TelegramE2EError("Telegram E2E login failed") from None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def open_telethon_transport(
    settings: TelegramE2ESettings,
    credentials: TelegramE2ECredentialState,
    *,
    bindings: TelethonBindings | None = None,
    timeout_seconds: float = 15,
    quiet_seconds: float = 0.5,
) -> TelegramE2ETransport:
    selected_bindings = bindings or load_telethon_bindings()
    try:
        client = selected_bindings.client_factory(
            selected_bindings.string_session_factory(credentials.session),
            credentials.api_id,
            credentials.api_hash,
        )
    except Exception:
        raise TelegramE2EError("Telegram E2E client could not initialize") from None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramE2EError("Telegram E2E session is not authorized")
        user = await client.get_me()
        if (
            user is None
            or getattr(user, "bot", None) is not False
            or user.id != credentials.user_id
            or user.id != settings.allowed_user_id
        ):
            raise TelegramE2EError("Telegram E2E user identity mismatch")
        bot = await client.get_entity(settings.bot_username)
        if (
            getattr(bot, "bot", None) is not True
            or bot.id != settings.bot_user_id
            or not isinstance(getattr(bot, "username", None), str)
            or bot.username.casefold() != settings.bot_username.casefold()
        ):
            raise TelegramE2EError("Telegram E2E bot identity mismatch")
        latest = await client.get_messages(bot, limit=1)
        watermark = 0 if not latest else int(latest[0].id)
        return _TelethonE2ETransport(
            client=client,
            bot=bot,
            bot_user_id=settings.bot_user_id,
            bindings=selected_bindings,
            watermark=watermark,
            timeout_seconds=timeout_seconds,
            quiet_seconds=quiet_seconds,
        )
    except TelegramE2EError:
        await _disconnect_safely(client)
        raise
    except Exception:
        await _disconnect_safely(client)
        raise TelegramE2EError("Telegram E2E transport could not connect") from None


class _TelethonE2ETransport:
    def __init__(
        self,
        *,
        client: Any,
        bot: Any,
        bot_user_id: int,
        bindings: TelethonBindings,
        watermark: int,
        timeout_seconds: float,
        quiet_seconds: float,
    ) -> None:
        self._client = client
        self._bot = bot
        self._bot_user_id = bot_user_id
        self._bindings = bindings
        self._watermark = watermark
        self._timeout_seconds = timeout_seconds
        self._quiet_seconds = quiet_seconds

    async def exchange(self, command: str) -> TelegramE2EMessage:
        events = asyncio.Queue()

        async def handle(event: Any) -> None:
            events.put_nowait(event)

        event_filter = self._bindings.new_message_factory(
            chats=self._bot,
        )
        self._client.add_event_handler(handle, event_filter)
        try:
            latest = await self._client.get_messages(self._bot, limit=1)
            if (
                latest and int(latest[0].id) > self._watermark
            ) or not events.empty():
                raise TelegramE2EError(
                    "Telegram E2E dialog changed outside the current exchange"
                )
            sent_message = await self._client.send_message(self._bot, command)
            sent_message_id = getattr(sent_message, "id", None)
            if (
                isinstance(sent_message_id, bool)
                or not isinstance(sent_message_id, int)
                or sent_message_id <= 0
            ):
                raise TelegramE2EError(
                    "Telegram E2E sent message identity is invalid"
                )
            try:
                event = await asyncio.wait_for(
                    events.get(),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError:
                raise TelegramE2EError("Telegram E2E reply timed out") from None
            message = self._message_from_event(
                event,
                reply_to_message_id=sent_message_id,
            )
            if self._quiet_seconds:
                await asyncio.sleep(self._quiet_seconds)
            if not events.empty():
                raise TelegramE2EError(
                    "Telegram E2E received duplicate or invalid replies"
                )
            self._watermark = message.id
            return message
        except TelegramE2EError:
            raise
        except Exception:
            raise TelegramE2EError("Telegram E2E message exchange failed") from None
        finally:
            self._client.remove_event_handler(handle, event_filter)

    async def close(self) -> None:
        try:
            await self._client.disconnect()
        except Exception:
            raise TelegramE2EError("Telegram E2E disconnect failed") from None

    def _message_from_event(
        self,
        event: Any,
        *,
        reply_to_message_id: int,
    ) -> TelegramE2EMessage:
        message_id = getattr(event, "id", None)
        text = getattr(event, "raw_text", None)
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= self._watermark
            or getattr(event, "sender_id", None) != self._bot_user_id
            or getattr(event, "chat_id", None) != self._bot_user_id
            or getattr(event, "out", None) is not False
            or getattr(event, "reply_to_msg_id", None) != reply_to_message_id
            or not isinstance(text, str)
            or not text
        ):
            raise TelegramE2EError("Telegram E2E message identity is invalid")
        return TelegramE2EMessage(id=message_id, text=text)


async def _disconnect_safely(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass
