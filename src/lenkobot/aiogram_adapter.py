from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Protocol

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .telegram_presentation import TelegramResponse, TelegramResponsePort
from .telegram_router import IncomingTelegramMessage


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
