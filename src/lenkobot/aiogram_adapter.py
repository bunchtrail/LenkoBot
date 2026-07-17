from typing import Protocol

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .telegram_router import IncomingTelegramMessage


class TelegramMessageRouter(Protocol):
    def handle(self, message: IncomingTelegramMessage) -> object | None: ...


class AiogramTelegramAdapter:
    def __init__(self, router: TelegramMessageRouter) -> None:
        self._router = router

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.handle_message)

    async def handle_message(self, message: Message) -> None:
        if message.from_user is None or message.chat is None or message.text is None:
            return

        self._router.handle(
            IncomingTelegramMessage(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                chat_type=message.chat.type,
                text=message.text,
            )
        )


def create_dispatcher(router: TelegramMessageRouter) -> Dispatcher:
    dispatcher = Dispatcher()
    AiogramTelegramAdapter(router).register(dispatcher)
    return dispatcher


async def run_polling(bot_token: str, router: TelegramMessageRouter) -> None:
    if not bot_token.strip():
        raise ValueError("Telegram bot token cannot be empty")

    dispatcher = create_dispatcher(router)
    async with Bot(token=bot_token) as bot:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
