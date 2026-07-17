import asyncio
from typing import Protocol

from .memory import MemoryRecord, MemoryScope, NewMemory
from .personas import Persona, PersonaCatalog
from .telegram_presentation import (
    TelegramCommand,
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    parse_telegram_command,
)
from .telegram_router import IncomingTelegramMessage, RoutedTurn, TelegramRouter
from .xai_provider import XaiTextResponse


class TextProvider(Protocol):
    def respond(self, prompt: str) -> XaiTextResponse: ...


class TurnContextBuilder(Protocol):
    def build(self, *, user_id: int, persona: Persona, turn: RoutedTurn) -> str: ...


class MemoryCommandStore(Protocol):
    def create(self, memory: NewMemory) -> MemoryRecord: ...

    def list_for_user(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
    ) -> tuple[MemoryRecord, ...]: ...

    def delete(self, memory_id: int, *, user_id: int) -> bool: ...


_MEMORY_PAGE_SIZE = 5
_MAX_REMEMBER_LENGTH = 500
_COMMAND_HELP = (
    "Доступные команды:\n"
    "/start, /help — показать эту справку.\n"
    "/persona [key] — выбрать персону.\n"
    "/remember <text> — сохранить общую запись.\n"
    "/memories [page] — показать записи памяти.\n"
    "/forget <id> — удалить запись памяти."
)


class TelegramApplicationService:
    def __init__(
        self,
        router: TelegramRouter,
        persona_catalog: PersonaCatalog,
        provider: TextProvider,
        response_port: TelegramResponsePort | None = None,
        context_builder: TurnContextBuilder | None = None,
        memory_store: MemoryCommandStore | None = None,
    ) -> None:
        self._router = router
        self._persona_catalog = persona_catalog
        self._provider = provider
        self._response_port = response_port
        self._context_builder = context_builder
        self._memory_store = memory_store

    async def handle(
        self,
        message: IncomingTelegramMessage,
        response_port: TelegramResponsePort | None = None,
    ) -> XaiTextResponse | None:
        command = parse_telegram_command(message.text)
        if command is not None:
            if not self._router.is_authorized(message.user_id, message.chat_type):
                return None
            presenter = self._response_port_for(response_port)
            return await self._handle_command(message, command, presenter)

        turn = self._router.route(message)
        if turn is None:
            return None
        presenter = self._response_port_for(response_port)

        persona = self._persona_catalog.get(turn.persona_key)
        if persona is None:
            raise ValueError("routed persona is not present in catalog")

        await presenter.send(
            TelegramResponse(
                chat_id=turn.chat_id,
                kind=TelegramResponseKind.STATUS,
                text="Готовлю ответ",
            )
        )
        try:
            prompt = self._build_prompt(persona, turn)
            if self._context_builder is not None:
                prompt = self._context_builder.build(
                    user_id=message.user_id,
                    persona=persona,
                    turn=turn,
                )
            result = await asyncio.to_thread(
                self._provider.respond,
                prompt,
            )
        except Exception:
            await presenter.send(
                TelegramResponse(
                    chat_id=turn.chat_id,
                    kind=TelegramResponseKind.ERROR,
                    text="Не удалось подготовить ответ. Попробуйте ещё раз.",
                )
            )
            return None

        if result.fallback_from is not None:
            await presenter.send(
                TelegramResponse(
                    chat_id=turn.chat_id,
                    kind=TelegramResponseKind.NOTICE,
                    text=(
                        "OAuth недоступен; использован API key. "
                        "Это может привести к расходам."
                    ),
                )
            )
        await presenter.send(
            TelegramResponse(
                chat_id=turn.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=result.text,
            )
        )
        return result

    def _response_port_for(
        self,
        response_port: TelegramResponsePort | None,
    ) -> TelegramResponsePort:
        presenter = response_port if response_port is not None else self._response_port
        if presenter is None:
            raise ValueError("Telegram response port is not configured")
        return presenter

    async def _handle_command(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if not self._router.is_authorized(message.user_id, message.chat_type):
            return None

        if command.name in {"start", "help"}:
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=_COMMAND_HELP,
                )
            )
            return None

        if command.name != "persona":
            if command.name == "remember":
                await self._handle_remember(message, command, presenter)
                return None
            if command.name == "memories":
                await self._handle_memories(message, command, presenter)
                return None
            if command.name == "forget":
                await self._handle_forget(message, command, presenter)
                return None
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Неизвестная команда. Используйте /help.",
            )
            return None

        if not command.arguments:
            available = ", ".join(
                f"{persona.key} ({persona.display_name})"
                for persona in self._persona_catalog.personas
            )
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=f"Доступные персоны: {available}.",
                )
            )
            return None

        if len(command.arguments) != 1:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /persona <key>.",
            )
            return None

        persona_key = command.arguments[0]
        persona = self._persona_catalog.get(persona_key)
        if persona is None or not self._router.switch_persona(
            user_id=message.user_id,
            chat_id=message.chat_id,
            persona_key=persona_key,
            chat_type=message.chat_type,
        ):
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Неизвестная персона. Проверьте key командой /persona.",
            )
            return None

        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=f"Персона переключена: {persona.display_name}.",
            )
        )
        return None

    async def _handle_remember(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        text = " ".join(command.arguments).strip()
        if not text:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /remember <text>.",
            )
            return
        if len(text) > _MAX_REMEMBER_LENGTH:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Текст не должен быть длиннее 500 символов.",
            )
            return
        if self._memory_store is None:
            await self._send_memory_error(message.chat_id, presenter)
            return
        try:
            self._memory_store.create(
                NewMemory(
                    user_id=message.user_id,
                    scope=MemoryScope.SHARED,
                    kind="fact",
                    content=text,
                )
            )
        except Exception:
            await self._send_memory_error(message.chat_id, presenter)
            return
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=f"Запомнил: {text}.",
            )
        )

    async def _handle_memories(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if len(command.arguments) > 1:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /memories [page].",
            )
            return
        page = 1
        if command.arguments:
            try:
                page = int(command.arguments[0])
            except ValueError:
                page = 0
            if page < 1:
                await self._send_command_error(
                    message.chat_id,
                    presenter,
                    "Номер страницы должен быть положительным.",
                )
                return
        if self._memory_store is None:
            await self._send_memory_error(message.chat_id, presenter)
            return
        try:
            records = self._memory_store.list_for_user(
                user_id=message.user_id,
                page=page,
                page_size=_MEMORY_PAGE_SIZE,
            )
        except Exception:
            await self._send_memory_error(message.chat_id, presenter)
            return
        if not records:
            text = "Память пуста." if page == 1 else f"На странице {page} записей нет."
        else:
            lines = [f"Память, страница {page}:"]
            lines.extend(
                f"{record.id}. [{record.scope.value}] {record.content}"
                for record in records
            )
            text = "\n".join(lines)
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=text,
            )
        )

    async def _handle_forget(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if len(command.arguments) != 1:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /forget <id>.",
            )
            return
        try:
            memory_id = int(command.arguments[0])
        except ValueError:
            memory_id = 0
        if memory_id < 1:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /forget <id>.",
            )
            return
        if self._memory_store is None:
            await self._send_memory_error(message.chat_id, presenter)
            return
        try:
            deleted = self._memory_store.delete(memory_id, user_id=message.user_id)
        except Exception:
            await self._send_memory_error(message.chat_id, presenter)
            return
        if not deleted:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Запись памяти не найдена.",
            )
            return
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=f"Удалено: запись {memory_id}.",
            )
        )

    @staticmethod
    def _build_prompt(persona: Persona, turn: RoutedTurn) -> str:
        return f"{persona.identity_prompt}\n\nUser message:\n{turn.text}"

    @staticmethod
    async def _send_command_error(
        chat_id: int,
        presenter: TelegramResponsePort,
        text: str,
    ) -> None:
        await presenter.send(
            TelegramResponse(
                chat_id=chat_id,
                kind=TelegramResponseKind.ERROR,
                text=text,
            )
        )

    @staticmethod
    async def _send_memory_error(
        chat_id: int,
        presenter: TelegramResponsePort,
    ) -> None:
        await TelegramApplicationService._send_command_error(
            chat_id,
            presenter,
            "Память сейчас недоступна. Попробуйте ещё раз.",
        )
