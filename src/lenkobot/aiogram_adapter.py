import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from inspect import isawaitable
from typing import Protocol, TypeVar

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from .telegram_presentation import (
    TELEGRAM_COMMANDS,
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    TelegramSentMessage,
)
from .telegram_router import IncomingTelegramCallback, IncomingTelegramMessage


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

    def handle_callback(
        self,
        callback: IncomingTelegramCallback,
        response_port: TelegramResponsePort,
    ) -> Awaitable[object | None]: ...


def _reply_markup(response: TelegramResponse) -> InlineKeyboardMarkup | None:
    if not response.inline_keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button.text,
                    callback_data=button.callback_data,
                )
                for button in row
            ]
            for row in response.inline_keyboard
        ]
    )


async def _edit_via_bot(
    edit_call: Callable[..., Awaitable[object]],
    response: TelegramResponse,
) -> bool:
    markup = _reply_markup(response)
    kwargs = {"text": response.text}
    if markup is not None:
        kwargs["reply_markup"] = markup
    if response.parse_mode is not None:
        kwargs["parse_mode"] = response.parse_mode.value
    try:
        await edit_call(**kwargs)
    except TelegramBadRequest as error:
        return "message is not modified" in str(error).casefold()
    except Exception:
        return False
    return True


class AiogramTelegramResponsePort:
    def __init__(
        self,
        message: Message,
        *,
        typing_interval_seconds: float = 4.0,
    ) -> None:
        self._message = message
        self._typing_interval_seconds = typing_interval_seconds
        self._typing_task: asyncio.Task | None = None

    async def send(self, response: TelegramResponse) -> TelegramSentMessage | None:
        if self._message.chat is None or response.chat_id != self._message.chat.id:
            raise ValueError("Telegram response target does not match source chat")
        markup = _reply_markup(response)
        kwargs = {}
        if markup is not None:
            kwargs["reply_markup"] = markup
        if response.parse_mode is not None:
            kwargs["parse_mode"] = response.parse_mode.value
        sent = await self._message.answer(response.text, **kwargs)
        if response.kind is TelegramResponseKind.STATUS:
            self._start_typing()
        elif response.kind in (TelegramResponseKind.FINAL, TelegramResponseKind.ERROR):
            await self._stop_typing()
        return TelegramSentMessage(
            chat_id=self._message.chat.id,
            message_id=sent.message_id,
        )

    async def edit(
        self,
        handle: TelegramSentMessage,
        response: TelegramResponse,
    ) -> bool:
        if response.kind in (TelegramResponseKind.FINAL, TelegramResponseKind.ERROR):
            await self._stop_typing()
        bot = getattr(self._message, "bot", None)
        if bot is None:
            return False
        return await _edit_via_bot(
            lambda **kwargs: bot.edit_message_text(
                chat_id=handle.chat_id,
                message_id=handle.message_id,
                **kwargs,
            ),
            response,
        )

    def bound_handle(self) -> TelegramSentMessage | None:
        if self._message.chat is None:
            return None
        return TelegramSentMessage(
            chat_id=self._message.chat.id,
            message_id=self._message.message_id,
        )

    def _start_typing(self) -> None:
        self._cancel_typing_task()
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _stop_typing(self) -> None:
        task = self._typing_task
        self._typing_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _cancel_typing_task(self) -> None:
        if self._typing_task is not None:
            self._typing_task.cancel()
            self._typing_task = None

    async def _typing_loop(self) -> None:
        bot = getattr(self._message, "bot", None)
        chat = self._message.chat
        if bot is None or chat is None:
            return
        while True:
            try:
                await bot.send_chat_action(chat_id=chat.id, action="typing")
            except Exception:
                return
            await asyncio.sleep(self._typing_interval_seconds)


class AiogramTelegramReplyResponsePort:
    def __init__(self, message: Message) -> None:
        self._message = message

    async def send(self, response: TelegramResponse) -> TelegramSentMessage | None:
        if self._message.chat is None or response.chat_id != self._message.chat.id:
            raise ValueError("Telegram response target does not match source chat")
        markup = _reply_markup(response)
        kwargs = {
            "reply_parameters": ReplyParameters(message_id=self._message.message_id),
        }
        if markup is not None:
            kwargs["reply_markup"] = markup
        if response.parse_mode is not None:
            kwargs["parse_mode"] = response.parse_mode.value
        sent = await self._message.answer(response.text, **kwargs)
        return TelegramSentMessage(
            chat_id=self._message.chat.id,
            message_id=sent.message_id,
        )

    async def edit(
        self,
        handle: TelegramSentMessage,
        response: TelegramResponse,
    ) -> bool:
        bot = getattr(self._message, "bot", None)
        if bot is None:
            return False
        return await _edit_via_bot(
            lambda **kwargs: bot.edit_message_text(
                chat_id=handle.chat_id,
                message_id=handle.message_id,
                **kwargs,
            ),
            response,
        )

    def bound_handle(self) -> TelegramSentMessage | None:
        if self._message.chat is None:
            return None
        return TelegramSentMessage(
            chat_id=self._message.chat.id,
            message_id=self._message.message_id,
        )


class TelegramDeliveryError(OSError):
    pass


class AiogramBotResponsePort:
    def __init__(self, bot: Bot, *, target_chat_id: int) -> None:
        self._bot = bot
        self._target_chat_id = target_chat_id

    async def send(self, response: TelegramResponse) -> TelegramSentMessage | None:
        if response.chat_id != self._target_chat_id:
            raise ValueError("Telegram response target does not match smoke target")
        try:
            kwargs = {
                "chat_id": self._target_chat_id,
                "text": f"{_LIVE_SMOKE_PREFIX}{response.text}",
            }
            markup = _reply_markup(response)
            if markup is not None:
                kwargs["reply_markup"] = markup
            if response.parse_mode is not None:
                kwargs["parse_mode"] = response.parse_mode.value
            sent = await self._bot.send_message(**kwargs)
        except Exception as error:
            raise TelegramDeliveryError("Telegram Bot API delivery failed") from error
        return TelegramSentMessage(
            chat_id=self._target_chat_id,
            message_id=sent.message_id,
        )

    async def edit(
        self,
        handle: TelegramSentMessage,
        response: TelegramResponse,
    ) -> bool:
        if handle.chat_id != self._target_chat_id:
            raise ValueError("Telegram response target does not match smoke target")
        return await _edit_via_bot(
            lambda **kwargs: self._bot.edit_message_text(
                chat_id=handle.chat_id,
                message_id=handle.message_id,
                **kwargs,
            ),
            response,
        )

    def bound_handle(self) -> TelegramSentMessage | None:
        return None


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
        if callable(getattr(self._router, "handle_callback", None)):
            dispatcher.callback_query.register(self.handle_callback)

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

    async def handle_callback(self, callback: CallbackQuery) -> None:
        try:
            handler = getattr(self._router, "handle_callback", None)
            message = getattr(callback, "message", None)
            user = getattr(callback, "from_user", None)
            data = getattr(callback, "data", None)
            if (
                not callable(handler)
                or user is None
                or message is None
                or message.chat is None
                or not isinstance(data, str)
            ):
                return

            incoming = IncomingTelegramCallback(
                user_id=user.id,
                chat_id=message.chat.id,
                chat_type=message.chat.type,
                data=data,
            )
            if self._response_port_factory is None:
                outcome = handler(incoming)
            else:
                outcome = handler(
                    incoming,
                    self._response_port_factory(message),
                )
            if isawaitable(outcome):
                await outcome
        finally:
            await callback.answer()


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
    command_scope_chat_id: int | None = None,
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
        if command_scope_chat_id is not None:
            await register_owner_command_menu(bot, command_scope_chat_id)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )


async def register_owner_command_menu(bot: Bot, owner_user_id: int) -> None:
    if (
        isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id <= 0
    ):
        raise ValueError("Telegram command menu owner must be a positive integer")
    try:
        result = await bot.set_my_commands(
            commands=[
                BotCommand(command=item.command, description=item.description)
                for item in TELEGRAM_COMMANDS
            ],
            scope=BotCommandScopeChat(chat_id=owner_user_id),
        )
    except Exception as error:
        raise TelegramDeliveryError(
            "Telegram command menu configuration failed"
        ) from error
    if result is False:
        raise TelegramDeliveryError("Telegram command menu configuration failed")
