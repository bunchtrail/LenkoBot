import asyncio
from typing import Protocol

from .memory import MemoryExtractionRun, MemoryRecord, MemoryScope, NewMemory
from .personas import Persona, PersonaCatalog
from .session_store import FailureStage, TranscriptFailure, TranscriptTurn
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
    def build(
        self,
        *,
        user_id: int,
        persona: Persona,
        turn: RoutedTurn,
        active_session_id: int | None = None,
        current_transcript_turn_id: int | None = None,
    ) -> str: ...


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

    def ensure_extraction_run(
        self,
        *,
        owner_user_id: int,
        session_id: int,
        source_turn_id: int,
    ) -> MemoryExtractionRun: ...


class TranscriptStore(Protocol):
    def begin_user_turn(
        self,
        *,
        user_id: int,
        persona_session_id: int,
        content: str,
    ) -> TranscriptTurn: ...

    def append_assistant_turn(
        self,
        *,
        session_id: int,
        content: str,
        provider_response_id: str | None,
    ) -> TranscriptTurn: ...

    def record_failure(
        self,
        *,
        session_id: int,
        related_turn_id: int,
        stage: FailureStage,
        error_kind: str,
    ) -> TranscriptFailure: ...


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
        session_store: TranscriptStore | None = None,
    ) -> None:
        self._router = router
        self._persona_catalog = persona_catalog
        self._provider = provider
        self._response_port = response_port
        self._context_builder = context_builder
        self._memory_store = memory_store
        self._session_store = session_store

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

        user_turn = None
        if self._session_store is not None:
            try:
                user_turn = self._session_store.begin_user_turn(
                    user_id=message.user_id,
                    persona_session_id=turn.session_id,
                    content=turn.text,
                )
            except Exception:
                await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text="Не удалось сохранить сообщение. Попробуйте ещё раз.",
                    )
                )
                return None
        try:
            await presenter.send(
                TelegramResponse(
                    chat_id=turn.chat_id,
                    kind=TelegramResponseKind.STATUS,
                    text="Готовлю ответ",
                )
            )
        except Exception:
            self._record_transcript_failure(
                user_turn,
                FailureStage.DELIVERY,
                "telegram_status_delivery_failed",
            )
            raise

        try:
            prompt = self._build_prompt(persona, turn)
            if self._context_builder is not None:
                prompt = self._context_builder.build(
                    user_id=message.user_id,
                    persona=persona,
                    turn=turn,
                    active_session_id=(
                        None if user_turn is None else user_turn.session_id
                    ),
                    current_transcript_turn_id=(
                        None if user_turn is None else user_turn.id
                    ),
                )
            result = await asyncio.to_thread(
                self._provider.respond,
                prompt,
            )
        except Exception:
            self._record_transcript_failure(
                user_turn,
                FailureStage.PROVIDER,
                "provider_request_failed",
            )
            try:
                await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text="Не удалось подготовить ответ. Попробуйте ещё раз.",
                    )
                )
            except Exception:
                self._record_transcript_failure(
                    user_turn,
                    FailureStage.DELIVERY,
                    "telegram_error_delivery_failed",
                )
                raise
            return None

        assistant_turn = None
        if self._session_store is not None and user_turn is not None:
            try:
                assistant_turn = self._session_store.append_assistant_turn(
                    session_id=user_turn.session_id,
                    content=result.text,
                    provider_response_id=result.response_id,
                )
            except Exception:
                await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text="Не удалось сохранить ответ. Попробуйте ещё раз.",
                    )
                )
                return None
        if self._memory_store is not None and user_turn is not None:
            try:
                self._memory_store.ensure_extraction_run(
                    owner_user_id=message.user_id,
                    session_id=user_turn.session_id,
                    source_turn_id=user_turn.id,
                )
            except Exception:
                await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text="Не удалось сохранить состояние памяти. Попробуйте ещё раз.",
                    )
                )
                return None
        try:
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
        except Exception:
            self._record_transcript_failure(
                assistant_turn,
                FailureStage.DELIVERY,
                "telegram_delivery_failed",
            )
            raise
        return result

    def _record_transcript_failure(
        self,
        related_turn: TranscriptTurn | None,
        stage: FailureStage,
        error_kind: str,
    ) -> None:
        if self._session_store is None or related_turn is None:
            return
        self._session_store.record_failure(
            session_id=related_turn.session_id,
            related_turn_id=related_turn.id,
            stage=stage,
            error_kind=error_kind,
        )

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
