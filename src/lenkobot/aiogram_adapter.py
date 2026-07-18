from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Protocol, TypeVar

from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReplyParameters

from .telegram_presentation import TelegramResponse, TelegramResponsePort
from .telegram_router import IncomingTelegramMessage


_Result = TypeVar("_Result")
_LIVE_SMOKE_PREFIX = "[SMOKE] "


class TelegramMessageRouter(Protocol):
    def handle(self, message: IncomingTelegramMessage) -> object | None: ...


class TelegramApplicationHandler(Protocol):
    def handle(
        self,
        message: IncomingTelegramMessage,
        response_port: TelegramResponsePort,
    ) -> Awaitable[object | None]: ...


class AiogramTelegramResponsePort:
    def __init__(self, message: Message) -> None:
        self._message = message

    async def send(self, response: TelegramResponse) -> None:
        if self._message.chat is None or response.chat_id != self._message.chat.id:
            raise ValueError("Telegram response target does not match source chat")
        await self._message.answer(response.text)


class AiogramTelegramReplyResponsePort:
    def __init__(self, message: Message) -> None:
        self._message = message

    async def send(self, response: TelegramResponse) -> None:
        if self._message.chat is None or response.chat_id != self._message.chat.id:
            raise ValueError("Telegram response target does not match source chat")
        await self._message.answer(
            response.text,
            reply_parameters=ReplyParameters(message_id=self._message.message_id),
        )


class TelegramDeliveryError(OSError):
    pass


class AiogramBotResponsePort:
    def __init__(self, bot: Bot, *, target_chat_id: int) -> None:
        self._bot = bot
        self._target_chat_id = target_chat_id

    async def send(self, response: TelegramResponse) -> None:
        if response.chat_id != self._target_chat_id:
            raise ValueError("Telegram response target does not match smoke target")
        try:
            await self._bot.send_message(
                chat_id=self._target_chat_id,
                text=f"{_LIVE_SMOKE_PREFIX}{response.text}",
            )
        except Exception as error:
            raise TelegramDeliveryError("Telegram Bot API delivery failed") from error


async def run_bot_delivery(
    bot_token: str,
    target_chat_id: int,
    action: Callable[[TelegramResponsePort], Awaitable[_Result]],
) -> _Result:
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise ValueError("Telegram bot token cannot be empty")
    try:
        bot_context = Bot(token=bot_token)
    except Exception as error:
        raise TelegramDeliveryError("Telegram Bot API client initialization failed") from error

    async with bot_context as bot:
        try:
            await bot.get_me()
        except Exception as error:
            raise TelegramDeliveryError("Telegram Bot API identity check failed") from error
        return await action(
            AiogramBotResponsePort(bot, target_chat_id=target_chat_id)
        )


async def verify_bot_identity(
    bot_token: str,
    *,
    expected_bot_user_id: int,
) -> None:
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise ValueError("Telegram bot token cannot be empty")
    try:
        bot_context = Bot(token=bot_token)
    except Exception as error:
        raise TelegramDeliveryError("Telegram Bot API client initialization failed") from error
    async with bot_context as bot:
        try:
            identity = await bot.get_me()
        except Exception as error:
            raise TelegramDeliveryError("Telegram Bot API identity check failed") from error
        if getattr(identity, "id", None) != expected_bot_user_id:
            raise TelegramDeliveryError("Telegram Bot API identity mismatch")


class AiogramTelegramAdapter:
    def __init__(
        self,
        router: TelegramMessageRouter | TelegramApplicationHandler,
        *,
        response_port_factory: Callable[[Message], TelegramResponsePort] | None = None,
    ) -> None:
        self._router = router
        self._response_port_factory = response_port_factory

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.handle_message)

    async def handle_message(self, message: Message) -> None:
        if message.from_user is None or message.chat is None or message.text is None:
            return

        incoming = IncomingTelegramMessage(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            text=message.text,
        )
        if self._response_port_factory is None:
            outcome = self._router.handle(incoming)
        else:
            outcome = self._router.handle(
                incoming,
                self._response_port_factory(message),
            )
        if isawaitable(outcome):
            await outcome


def create_dispatcher(
    router: TelegramMessageRouter | TelegramApplicationHandler,
    *,
    response_port_factory: Callable[[Message], TelegramResponsePort] | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    AiogramTelegramAdapter(
        router,
        response_port_factory=response_port_factory,
    ).register(dispatcher)
    return dispatcher


async def run_polling(
    bot_token: str,
    router: TelegramMessageRouter | TelegramApplicationHandler,
    *,
    response_port_factory: Callable[[Message], TelegramResponsePort] | None = None,
) -> None:
    if not bot_token.strip():
        raise ValueError("Telegram bot token cannot be empty")

    if response_port_factory is None:
        dispatcher = create_dispatcher(router)
    else:
        dispatcher = create_dispatcher(
            router,
            response_port_factory=response_port_factory,
        )
    async with Bot(token=bot_token) as bot:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
