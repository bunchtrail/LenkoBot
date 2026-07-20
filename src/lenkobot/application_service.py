import asyncio
from dataclasses import replace
import math
from pathlib import Path
from typing import Protocol

from .action_confirmation import ConfirmationAction
from .memory import MemoryExtractionRun, MemoryRecord, MemoryScope, NewMemory
from .memory_extraction import ExtractionCoordinator
from .personas import Persona, PersonaCatalog, VoiceRenderer
from .session_store import FailureStage, TranscriptFailure, TranscriptTurn
from .session_store import SessionFinalizer
from .telegram_presentation import (
    TelegramCommand,
    TelegramEditableResponsePort,
    TelegramInlineButton,
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    TelegramSentMessage,
    confirmation_callback_data,
    forget_callback_data,
    memories_page_callback_data,
    parse_confirmation_callback_data,
    parse_forget_callback_data,
    parse_memories_page_callback_data,
    parse_persona_callback_data,
    parse_telegram_command,
    persona_callback_data,
    render_command_index,
    split_telegram_text,
)
from .telegram_router import (
    IncomingTelegramCallback,
    IncomingTelegramMessage,
    RoutedTurn,
    TelegramRouter,
)
from .xai_provider import XaiPrompt, XaiTextResponse


class TextProvider(Protocol):
    def respond(self, prompt: XaiPrompt) -> XaiTextResponse: ...


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

    def build_messages(
        self,
        *,
        user_id: int,
        persona: Persona,
        turn: RoutedTurn,
        active_session_id: int | None = None,
        current_transcript_turn_id: int | None = None,
    ) -> XaiPrompt: ...


class MemoryCommandStore(Protocol):
    def create(self, memory: NewMemory) -> MemoryRecord: ...

    def get(self, memory_id: int, *, user_id: int) -> MemoryRecord | None: ...

    def list_for_user(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
    ) -> tuple[MemoryRecord, ...]: ...

    def count_for_user(self, *, user_id: int) -> int: ...

    def delete(self, memory_id: int, *, user_id: int) -> bool: ...

    def ensure_extraction_run(
        self,
        *,
        owner_user_id: int,
        session_id: int,
        source_turn_id: int,
    ) -> MemoryExtractionRun: ...


class ConfirmationStore(Protocol):
    def create(
        self,
        *,
        owner_user_id: int,
        action_type: str,
        payload: dict,
    ) -> str: ...

    def consume(
        self,
        *,
        token: str,
        owner_user_id: int,
    ) -> ConfirmationAction | None: ...


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

    def active_session_for_lane(
        self,
        *,
        user_id: int,
        persona_session_id: int,
    ) -> object | None: ...


class ExtractionService(Protocol):
    def process_with_retry(
        self,
        run_id: int,
        *,
        owner_user_id: int,
        max_attempts: int = 3,
    ) -> object: ...


class ExtractionCoordinatorPort(Protocol):
    def drain_for_lane(
        self,
        *,
        owner_user_id: int,
        persona_session_id: int,
    ) -> None: ...

    def process_after_delivery(
        self,
        *,
        run_id: int,
        owner_user_id: int,
        persona_session_id: int,
    ) -> None: ...


_MEMORY_PAGE_SIZE = 5
_MAX_REMEMBER_LENGTH = 500
_SNIPPET_LENGTH = 60
_STALE_CALLBACK_TEXT = "Кнопка устарела. Повторите команду ещё раз."


def _snippet(text: str, *, limit: int = _SNIPPET_LENGTH) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


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
        extraction_service: ExtractionService | None = None,
        extraction_coordinator: ExtractionCoordinatorPort | None = None,
        session_finalizer: SessionFinalizer | None = None,
        persona_config_path: Path | None = None,
        confirmation_store: ConfirmationStore | None = None,
    ) -> None:
        self._router = router
        self._persona_catalog = persona_catalog
        self._provider = provider
        self._response_port = response_port
        self._context_builder = context_builder
        self._memory_store = memory_store
        self._session_store = session_store
        self._extraction_service = extraction_service
        self._extraction_coordinator = extraction_coordinator
        if (
            self._extraction_coordinator is None
            and memory_store is not None
            and extraction_service is not None
        ):
            self._extraction_coordinator = ExtractionCoordinator(
                memory_store,
                extraction_service,
            )
        self._session_finalizer = session_finalizer
        self._persona_config_path = persona_config_path
        self._confirmation_store = confirmation_store
        self._voice_renderer = VoiceRenderer()

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

        persona = self._router.persona_for_turn(turn)

        if self._extraction_coordinator is not None:
            try:
                await asyncio.to_thread(
                    self._extraction_coordinator.drain_for_lane,
                    owner_user_id=message.user_id,
                    persona_session_id=turn.session_id,
                )
            except Exception:
                pass

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
                        text=self._voice(
                            persona,
                            "error",
                            "Не удалось сохранить сообщение. Попробуйте ещё раз.",
                            text="Не удалось сохранить сообщение. Попробуйте ещё раз.",
                        ),
                    )
                )
                return None
        try:
            status_handle = await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.STATUS,
                        text=self._voice(
                            persona,
                            "status",
                            "Готовлю ответ",
                        ),
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
            prompt: XaiPrompt = self._build_prompt(persona, turn)
            if self._context_builder is not None:
                context_kwargs = {
                    "user_id": message.user_id,
                    "persona": persona,
                    "turn": turn,
                    "active_session_id": (
                        None if user_turn is None else user_turn.session_id
                    ),
                    "current_transcript_turn_id": (
                        None if user_turn is None else user_turn.id
                    ),
                }
                if (
                    getattr(self._provider, "supports_message_input", False)
                    and hasattr(self._context_builder, "build_messages")
                ):
                    prompt = self._context_builder.build_messages(**context_kwargs)
                else:
                    prompt = self._context_builder.build(**context_kwargs)
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
                await self._deliver(
                    presenter,
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text=self._voice(
                            persona,
                            "error",
                            "Не удалось подготовить ответ. Попробуйте ещё раз.",
                            text="Не удалось подготовить ответ. Попробуйте ещё раз.",
                        ),
                    ),
                    edit_handle=status_handle,
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
        extraction_run: MemoryExtractionRun | None = None
        if self._session_store is not None and user_turn is not None:
            try:
                assistant_turn = self._session_store.append_assistant_turn(
                    session_id=user_turn.session_id,
                    content=result.text,
                    provider_response_id=result.response_id,
                )
            except Exception:
                await self._deliver(
                    presenter,
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text=self._voice(
                            persona,
                            "error",
                            "Не удалось сохранить ответ. Попробуйте ещё раз.",
                            text="Не удалось сохранить ответ. Попробуйте ещё раз.",
                        ),
                    ),
                    edit_handle=status_handle,
                )
                return None
        if self._memory_store is not None and user_turn is not None:
            try:
                extraction_run = self._memory_store.ensure_extraction_run(
                    owner_user_id=message.user_id,
                    session_id=user_turn.session_id,
                    source_turn_id=user_turn.id,
                )
            except Exception:
                await self._deliver(
                    presenter,
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.ERROR,
                        text=self._voice(
                            persona,
                            "error",
                            "Не удалось сохранить состояние памяти. Попробуйте ещё раз.",
                            text="Не удалось сохранить состояние памяти. Попробуйте ещё раз.",
                        ),
                    ),
                    edit_handle=status_handle,
                )
                return None
        try:
            if result.fallback_from is not None:
                await presenter.send(
                    TelegramResponse(
                        chat_id=turn.chat_id,
                        kind=TelegramResponseKind.NOTICE,
                        text=self._voice(
                            persona,
                            "notice",
                            "OAuth недоступен; использован API key. "
                            "Это может привести к расходам.",
                            text="OAuth недоступен; использован API key. "
                            "Это может привести к расходам.",
                        ),
                    )
                )
            await self._deliver(
                presenter,
                TelegramResponse(
                    chat_id=turn.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=result.text,
                ),
                edit_handle=status_handle,
            )
        except Exception:
            self._record_transcript_failure(
                assistant_turn,
                FailureStage.DELIVERY,
                "telegram_delivery_failed",
            )
            raise
        if self._extraction_coordinator is not None and extraction_run is not None:
            try:
                await asyncio.to_thread(
                    self._extraction_coordinator.process_after_delivery,
                    run_id=extraction_run.id,
                    owner_user_id=message.user_id,
                    persona_session_id=turn.session_id,
                )
            except Exception:
                pass
        return result

    async def handle_callback(
        self,
        callback: IncomingTelegramCallback,
        response_port: TelegramResponsePort | None = None,
    ) -> None:
        if not self._router.is_authorized(
            callback.user_id,
            callback.chat_type,
        ):
            return None

        presenter = self._response_port_for(response_port)

        confirmation = parse_confirmation_callback_data(callback.data)
        if confirmation is not None:
            action, token = confirmation
            await self._handle_confirmation_callback(
                callback,
                presenter,
                confirmed=action == "confirm",
                token=token,
            )
            return None

        page = parse_memories_page_callback_data(callback.data)
        if page is not None:
            await self._handle_memories_page_callback(callback, presenter, page)
            return None

        memory_id = parse_forget_callback_data(callback.data)
        if memory_id is not None:
            await self._handle_forget_button_callback(callback, presenter, memory_id)
            return None

        persona_key = parse_persona_callback_data(callback.data)
        persona = (
            None
            if persona_key is None
            else self._persona_catalog.get(persona_key)
        )
        if persona is None:
            await self._send_command_error(
                callback.chat_id,
                presenter,
                _STALE_CALLBACK_TEXT,
            )
            return None

        current_lane = self._router.current_lane(
            IncomingTelegramMessage(
                user_id=callback.user_id,
                chat_id=callback.chat_id,
                chat_type=callback.chat_type,
                text="",
            )
        )
        already_active = (
            current_lane is not None
            and current_lane.persona_key == persona.key
            and current_lane.identity_version == persona.identity_version
        )
        if not already_active and not self._router.switch_persona(
            user_id=callback.user_id,
            chat_id=callback.chat_id,
            persona_key=persona.key,
            chat_type=callback.chat_type,
        ):
            await self._send_command_error(
                callback.chat_id,
                presenter,
                "Не удалось переключить персону. Откройте /persona ещё раз.",
            )
            return None

        picker = self._persona_picker_response(
            user_id=callback.user_id,
            chat_id=callback.chat_id,
            chat_type=callback.chat_type,
        )
        if not await self._edit_bound(presenter, picker):
            await presenter.send(
                TelegramResponse(
                    chat_id=callback.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=self._command_voice(
                        IncomingTelegramMessage(
                            user_id=callback.user_id,
                            chat_id=callback.chat_id,
                            chat_type=callback.chat_type,
                            text="",
                        ),
                        f"Персона переключена: {persona.display_name}.",
                    ),
                )
            )
        return None

    async def _handle_confirmation_callback(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        *,
        confirmed: bool,
        token: str,
    ) -> None:
        if self._confirmation_store is None:
            await self._send_command_error(
                callback.chat_id,
                presenter,
                _STALE_CALLBACK_TEXT,
            )
            return
        action = self._confirmation_store.consume(
            token=token,
            owner_user_id=callback.user_id,
        )
        if action is None:
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        if not confirmed:
            await self._send_callback_result(callback, presenter, "Отменено.")
            return
        if action.action_type == "forget_memory":
            await self._execute_confirmed_forget(callback, presenter, action)
            return
        if action.action_type == "close_session":
            await self._execute_confirmed_close(callback, presenter, action)
            return
        await self._send_callback_result(
            callback,
            presenter,
            _STALE_CALLBACK_TEXT,
            kind=TelegramResponseKind.ERROR,
        )

    async def _execute_confirmed_forget(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        action: ConfirmationAction,
    ) -> None:
        memory_id = action.payload.get("memory_id")
        if (
            isinstance(memory_id, bool)
            or not isinstance(memory_id, int)
            or memory_id < 1
            or self._memory_store is None
        ):
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        try:
            deleted = self._memory_store.delete(memory_id, user_id=callback.user_id)
        except Exception:
            await self._send_callback_result(
                callback,
                presenter,
                "Память сейчас недоступна. Попробуйте ещё раз.",
                kind=TelegramResponseKind.ERROR,
            )
            return
        text = (
            f"Удалено: запись {memory_id}."
            if deleted
            else "Запись уже удалена."
        )
        await self._send_callback_result(callback, presenter, text)

    async def _execute_confirmed_close(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        action: ConfirmationAction,
    ) -> None:
        lane_session_id = action.payload.get("lane_session_id")
        session_id = action.payload.get("session_id")
        if (
            isinstance(lane_session_id, bool)
            or not isinstance(lane_session_id, int)
            or isinstance(session_id, bool)
            or not isinstance(session_id, int)
            or self._session_store is None
            or self._session_finalizer is None
        ):
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        try:
            active_session = self._session_store.active_session_for_lane(
                user_id=callback.user_id,
                persona_session_id=lane_session_id,
            )
        except Exception:
            active_session = None
        if active_session is None or active_session.id != session_id:
            await self._send_callback_result(
                callback,
                presenter,
                "Разговор уже закрыт.",
            )
            return
        await self._send_callback_result(
            callback,
            presenter,
            "Закрываю разговор…",
            kind=TelegramResponseKind.NOTICE,
        )
        try:
            await asyncio.to_thread(
                self._session_finalizer.finalize,
                session_id=session_id,
                owner_user_id=callback.user_id,
            )
        except Exception:
            await self._send_callback_result(
                callback,
                presenter,
                "Не удалось закрыть разговор. Сообщения сохранены, попробуйте ещё раз.",
                kind=TelegramResponseKind.ERROR,
            )
            return
        await self._send_callback_result(
            callback,
            presenter,
            "Разговор закрыт. Следующее сообщение начнёт новый.",
        )

    async def _handle_memories_page_callback(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        page: int,
    ) -> None:
        if self._memory_store is None:
            await self._send_memory_error(callback.chat_id, presenter)
            return
        try:
            response = self._memories_page_response(
                user_id=callback.user_id,
                chat_id=callback.chat_id,
                chat_type=callback.chat_type,
                page=page,
            )
        except Exception:
            await self._send_memory_error(callback.chat_id, presenter)
            return
        if not await self._edit_bound(presenter, response):
            await presenter.send(response)

    async def _handle_forget_button_callback(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        memory_id: int,
    ) -> None:
        if self._memory_store is None:
            await self._send_memory_error(callback.chat_id, presenter)
            return
        try:
            record = self._memory_store.get(memory_id, user_id=callback.user_id)
        except Exception:
            await self._send_memory_error(callback.chat_id, presenter)
            return
        if record is None:
            await self._send_callback_result(
                callback,
                presenter,
                "Запись уже удалена.",
            )
            return
        prompt = self._forget_prompt_response(
            user_id=callback.user_id,
            chat_id=callback.chat_id,
            chat_type=callback.chat_type,
            record=record,
        )
        if prompt is None:
            await self._send_command_error(
                callback.chat_id,
                presenter,
                "Подтверждения сейчас недоступны.",
            )
            return
        await presenter.send(prompt)

    def replace_persona_catalog(self, persona_catalog: PersonaCatalog) -> None:
        self._router.replace_catalog(persona_catalog)
        self._persona_catalog = persona_catalog

    def _voice(
        self,
        persona: Persona,
        kind: str,
        fallback: str,
        **values: object,
    ) -> str:
        return self._voice_renderer.render(
            persona,
            kind,
            fallback=fallback,
            **values,
        )

    def _command_voice(
        self,
        message: IncomingTelegramMessage,
        fallback: str,
    ) -> str:
        persona = self._persona_catalog.get(self._persona_catalog.default_persona_key)
        lane = self._router.current_lane(message)
        if lane is not None:
            try:
                persona = self._router.persona_for_turn(lane)
            except ValueError:
                pass
        if persona is None:
            return fallback
        return self._voice(persona, "command", fallback, text=fallback)

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

    async def _deliver(
        self,
        presenter: TelegramResponsePort,
        response: TelegramResponse,
        *,
        edit_handle: TelegramSentMessage | None = None,
    ) -> None:
        chunks = split_telegram_text(response.text)
        first = response if len(chunks) == 1 else replace(response, text=chunks[0])
        delivered = False
        if (
            edit_handle is not None
            and edit_handle.chat_id == response.chat_id
            and isinstance(presenter, TelegramEditableResponsePort)
        ):
            try:
                delivered = await presenter.edit(edit_handle, first)
            except Exception:
                delivered = False
        if not delivered:
            await presenter.send(first)
        for chunk in chunks[1:]:
            await presenter.send(
                replace(response, text=chunk, inline_keyboard=())
            )

    async def _edit_bound(
        self,
        presenter: TelegramResponsePort,
        response: TelegramResponse,
    ) -> bool:
        if not isinstance(presenter, TelegramEditableResponsePort):
            return False
        handle = presenter.bound_handle()
        if handle is None or handle.chat_id != response.chat_id:
            return False
        try:
            return await presenter.edit(handle, response)
        except Exception:
            return False

    def _persona_for_chat(
        self,
        user_id: int,
        chat_id: int,
        chat_type: str,
    ) -> Persona | None:
        persona = self._persona_catalog.get(self._persona_catalog.default_persona_key)
        lane = self._router.current_lane(
            IncomingTelegramMessage(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=chat_type,
                text="",
            )
        )
        if lane is not None:
            try:
                persona = self._router.persona_for_turn(lane)
            except ValueError:
                pass
        return persona

    async def _send_callback_result(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        text: str,
        *,
        kind: TelegramResponseKind = TelegramResponseKind.FINAL,
    ) -> None:
        persona = self._persona_for_chat(
            callback.user_id,
            callback.chat_id,
            callback.chat_type,
        )
        rendered = text
        if persona is not None:
            voice_kind = "error" if kind is TelegramResponseKind.ERROR else "command"
            rendered = self._voice(persona, voice_kind, text, text=text)
        response = TelegramResponse(
            chat_id=callback.chat_id,
            kind=kind,
            text=rendered,
        )
        if not await self._edit_bound(presenter, response):
            await presenter.send(response)

    async def _handle_command(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if not self._router.is_authorized(message.user_id, message.chat_type):
            return None

        if command.name in {"start", "help"}:
            text = render_command_index()
            if command.name == "start":
                greeting = self._command_voice(message, "На связи. Вот что умею:")
                text = f"{greeting}\n{text}"
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=text,
                )
            )
            return None

        if command.name == "new":
            await self._handle_new(message, command, presenter)
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

        if command.arguments == ("reload",):
            await self._handle_persona_reload(message, presenter)
            return None

        if not command.arguments:
            await presenter.send(
                self._persona_picker_response(
                    user_id=message.user_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
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
                text=self._command_voice(
                    message,
                    f"Персона переключена: {persona.display_name}.",
                ),
            )
        )
        return None

    def _persona_picker_response(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str,
    ) -> TelegramResponse:
        message = IncomingTelegramMessage(
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            text="",
        )
        active_lane = self._router.current_lane(message)
        active_key = (
            self._persona_catalog.default_persona_key
            if active_lane is None
            else active_lane.persona_key
        )
        display_names = ", ".join(
            persona.display_name for persona in self._persona_catalog.personas
        )
        buttons = []
        for persona in self._persona_catalog.personas:
            try:
                callback_data = persona_callback_data(persona.key)
            except ValueError:
                continue
            prefix = "✓ " if persona.key == active_key else ""
            buttons.append(
                (
                    TelegramInlineButton(
                        text=f"{prefix}{persona.display_name}",
                        callback_data=callback_data,
                    ),
                )
            )
        return TelegramResponse(
            chat_id=chat_id,
            kind=TelegramResponseKind.FINAL,
            text=self._command_voice(message, f"Выбери персону: {display_names}."),
            inline_keyboard=tuple(buttons),
        )

    def _forget_prompt_response(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str,
        record: MemoryRecord,
    ) -> TelegramResponse | None:
        if self._confirmation_store is None:
            return None
        try:
            token = self._confirmation_store.create(
                owner_user_id=user_id,
                action_type="forget_memory",
                payload={"memory_id": record.id},
            )
        except Exception:
            return None
        message = IncomingTelegramMessage(
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            text="",
        )
        return TelegramResponse(
            chat_id=chat_id,
            kind=TelegramResponseKind.FINAL,
            text=self._command_voice(
                message,
                f"Удалить запись {record.id}: «{_snippet(record.content)}»?",
            ),
            inline_keyboard=(
                (
                    TelegramInlineButton(
                        text="Удалить",
                        callback_data=confirmation_callback_data("confirm", token),
                    ),
                    TelegramInlineButton(
                        text="Отмена",
                        callback_data=confirmation_callback_data("cancel", token),
                    ),
                ),
            ),
        )

    def _memories_page_response(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str,
        page: int,
    ) -> TelegramResponse:
        records = self._memory_store.list_for_user(
            user_id=user_id,
            page=page,
            page_size=_MEMORY_PAGE_SIZE,
        )
        total = self._memory_store.count_for_user(user_id=user_id)
        total_pages = max(1, math.ceil(total / _MEMORY_PAGE_SIZE))
        if not records:
            text = (
                "Память пуста."
                if page == 1
                else f"На странице {page} записей нет."
            )
        else:
            lines = [f"Память, страница {page} из {total_pages}:"]
            lines.extend(
                f"{record.id}. [{record.scope.value}] {record.content}"
                for record in records
            )
            text = "\n".join(lines)
        buttons = []
        if page > 1:
            buttons.append(
                TelegramInlineButton(
                    text="← Назад",
                    callback_data=memories_page_callback_data(page - 1),
                )
            )
        if page < total_pages:
            buttons.append(
                TelegramInlineButton(
                    text="Вперёд →",
                    callback_data=memories_page_callback_data(page + 1),
                )
            )
        message = IncomingTelegramMessage(
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            text="",
        )
        return TelegramResponse(
            chat_id=chat_id,
            kind=TelegramResponseKind.FINAL,
            text=self._command_voice(message, text),
            inline_keyboard=(tuple(buttons),) if buttons else (),
        )

    async def _handle_persona_reload(
        self,
        message: IncomingTelegramMessage,
        presenter: TelegramResponsePort,
    ) -> None:
        if self._persona_config_path is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Перезагрузка персон сейчас недоступна.",
            )
            return
        try:
            catalog = PersonaCatalog.from_toml(self._persona_config_path)
            self.replace_persona_catalog(catalog)
        except (OSError, KeyError, TypeError, ValueError):
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Не удалось перезагрузить персон. Проверьте конфигурацию.",
            )
            return
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(message, "Персоны перезагружены."),
            )
        )

    async def _handle_new(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if command.arguments:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /new.",
            )
            return
        if self._session_store is None or self._session_finalizer is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Закрытие разговоров сейчас недоступно.",
            )
            return
        lane = self._router.current_lane(message)
        if lane is None:
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=self._command_voice(message, "Активного разговора нет."),
                )
            )
            return
        active_session = self._session_store.active_session_for_lane(
            user_id=message.user_id,
            persona_session_id=lane.session_id,
        )
        if active_session is None:
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=self._command_voice(message, "Активного разговора нет."),
                )
            )
            return
        if self._confirmation_store is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Подтверждения сейчас недоступны.",
            )
            return
        try:
            token = self._confirmation_store.create(
                owner_user_id=message.user_id,
                action_type="close_session",
                payload={
                    "lane_session_id": lane.session_id,
                    "session_id": active_session.id,
                },
            )
        except Exception:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Подтверждения сейчас недоступны.",
            )
            return
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(message, "Закрыть текущий разговор?"),
                inline_keyboard=(
                    (
                        TelegramInlineButton(
                            text="Закрыть",
                            callback_data=confirmation_callback_data("confirm", token),
                        ),
                        TelegramInlineButton(
                            text="Отмена",
                            callback_data=confirmation_callback_data("cancel", token),
                        ),
                    ),
                ),
            )
        )

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
                text=self._command_voice(message, f"Запомнил: {text}."),
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
            response = self._memories_page_response(
                user_id=message.user_id,
                chat_id=message.chat_id,
                chat_type=message.chat_type,
                page=page,
            )
        except Exception:
            await self._send_memory_error(message.chat_id, presenter)
            return
        await presenter.send(response)

    async def _handle_forget(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if len(command.arguments) > 1:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /forget [id].",
            )
            return
        if self._memory_store is None:
            await self._send_memory_error(message.chat_id, presenter)
            return
        if not command.arguments:
            try:
                records = self._memory_store.list_for_user(
                    user_id=message.user_id,
                    page=1,
                    page_size=_MEMORY_PAGE_SIZE,
                )
            except Exception:
                await self._send_memory_error(message.chat_id, presenter)
                return
            if not records:
                await presenter.send(
                    TelegramResponse(
                        chat_id=message.chat_id,
                        kind=TelegramResponseKind.FINAL,
                        text=self._command_voice(message, "Память пуста."),
                    )
                )
                return
            lines = ["Выбери запись для удаления:"]
            buttons = []
            for record in records:
                lines.append(
                    f"{record.id}. [{record.scope.value}] {_snippet(record.content)}"
                )
                buttons.append(
                    (
                        TelegramInlineButton(
                            text=f"🗑 {record.id}",
                            callback_data=forget_callback_data(record.id),
                        ),
                    )
                )
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=self._command_voice(message, "\n".join(lines)),
                    inline_keyboard=tuple(buttons),
                )
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
                "Формат команды: /forget [id].",
            )
            return
        try:
            record = self._memory_store.get(memory_id, user_id=message.user_id)
        except Exception:
            await self._send_memory_error(message.chat_id, presenter)
            return
        if record is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Запись памяти не найдена.",
            )
            return
        prompt = self._forget_prompt_response(
            user_id=message.user_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            record=record,
        )
        if prompt is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Подтверждения сейчас недоступны.",
            )
            return
        await presenter.send(prompt)

    @staticmethod
    def _build_prompt(persona: Persona, turn: RoutedTurn) -> str:
        return f"{persona.identity_prompt}\n\nUser message:\n{turn.text}"

    async def _send_command_error(
        self,
        chat_id: int,
        presenter: TelegramResponsePort,
        text: str,
    ) -> None:
        persona = self._persona_catalog.get(self._persona_catalog.default_persona_key)
        lane = self._router.current_lane(
            IncomingTelegramMessage(
                user_id=self._router.allowed_user_id,
                chat_id=chat_id,
                text="",
                chat_type="private",
            )
        )
        if lane is not None:
            try:
                persona = self._router.persona_for_turn(lane)
            except ValueError:
                pass
        error_text = (
            text
            if persona is None
            else self._voice(persona, "error", text, text=text)
        )
        await presenter.send(
            TelegramResponse(
                chat_id=chat_id,
                kind=TelegramResponseKind.ERROR,
                text=error_text,
            )
        )

    async def _send_memory_error(
        self,
        chat_id: int,
        presenter: TelegramResponsePort,
    ) -> None:
        await self._send_command_error(
            chat_id,
            presenter,
            "Память сейчас недоступна. Попробуйте ещё раз.",
        )
