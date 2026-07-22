import asyncio
from dataclasses import replace
from datetime import datetime
import math
from pathlib import Path
import re
from typing import Protocol

from .action_confirmation import ConfirmationAction, ConfirmationResolution
from .memory import MemoryExtractionRun, MemoryRecord, MemoryScope, NewMemory
from .memory_extraction import ExtractionCoordinator
from .personas import Persona, PersonaCatalog, VoiceRenderer
from .reminder_parser import ParsedReminder, extract_reminder_request
from .reminder_schedule import ScheduleKind
from .reminder_store import (
    ProfileReminderPolicy,
    ReminderDraft,
    ReminderJob,
    ReminderJobStatus,
    ReminderRun,
    ReminderRunStatus,
    TaskRecord,
    TaskStatus,
)
from .session_store import FailureStage, TranscriptFailure, TranscriptTurn
from .session_store import SessionFinalizer
from .telegram_presentation import (
    TelegramCommand,
    TelegramEditableResponsePort,
    TelegramInlineButton,
    TelegramParseMode,
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    TelegramSentMessage,
    TelegramWebSource,
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
    render_sources_html,
    split_telegram_text,
)
from .telegram_router import (
    IncomingTelegramCallback,
    IncomingTelegramMessage,
    RoutedTurn,
    TelegramRouter,
)
from .web_search import SearchResult, WebSearchToolLoop
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


class ConfirmationService(Protocol):
    def request(
        self,
        *,
        owner_user_id: int,
        action_type: str,
        payload: dict,
    ) -> str: ...

    def resolve(
        self,
        *,
        token: str,
        owner_user_id: int,
        confirmed: bool,
    ) -> ConfirmationResolution | None: ...


class ReminderParserPort(Protocol):
    def parse(self, request: str, *, default_timezone: str) -> ParsedReminder: ...


class ReminderStore(Protocol):
    def ensure_profile(self, *, owner_user_id: int) -> ProfileReminderPolicy: ...

    def get_profile_policy(self, *, owner_user_id: int) -> ProfileReminderPolicy: ...

    def set_profile_policy(
        self,
        *,
        owner_user_id: int,
        timezone_name: str,
        quiet_start_minute: int | None,
        quiet_end_minute: int | None,
    ) -> ProfileReminderPolicy: ...

    def persona_id_for_key(self, persona_key: str) -> int | None: ...

    def create_draft(self, draft: ReminderDraft): ...

    def mark_awaiting_confirmation(
        self,
        *,
        task_id: int,
        owner_user_id: int,
    ) -> ReminderJob: ...

    def activate(self, *, task_id: int, owner_user_id: int) -> ReminderJob: ...

    def get_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord | None: ...

    def get_job_for_task(
        self,
        *,
        task_id: int,
        owner_user_id: int,
    ) -> ReminderJob | None: ...

    def list_tasks(
        self,
        *,
        owner_user_id: int,
        limit: int = 100,
    ) -> tuple[TaskRecord, ...]: ...

    def cancel_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord: ...

    def complete_task(self, *, task_id: int, owner_user_id: int) -> TaskRecord: ...

    def snooze_task(
        self,
        *,
        task_id: int,
        owner_user_id: int,
        action_token: str,
        delay_seconds: int = 600,
    ) -> ReminderRun: ...


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
_REMINDER_ACTION_TYPES = frozenset(
    {
        "activate_reminder",
        "cancel_reminder",
        "complete_reminder",
        "snooze_reminder",
    }
)
_REMINDER_TASK_LIMIT = 10
_SNOOZE_SECONDS = 600


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
        confirmation_service: ConfirmationService | None = None,
        tool_loop: WebSearchToolLoop | None = None,
        reminder_store: ReminderStore | None = None,
        reminder_parser: ReminderParserPort | None = None,
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
        self._confirmation_service = confirmation_service
        self._tool_loop = tool_loop
        self._reminder_store = reminder_store
        self._reminder_parser = reminder_parser
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

        reminder_request = extract_reminder_request(message.text)
        if reminder_request is not None:
            if not self._router.is_authorized(message.user_id, message.chat_type):
                return None
            presenter = self._response_port_for(response_port)
            await self._handle_reminder_request(
                message,
                reminder_request,
                presenter,
            )
            return None

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

        sources = ()
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
            if self._tool_loop is None:
                result = await asyncio.to_thread(
                    self._provider.respond,
                    prompt,
                )
            else:
                async def on_search_start(query: str) -> None:
                    await self._update_search_status(
                        presenter,
                        chat_id=turn.chat_id,
                        status_handle=status_handle,
                        query=query,
                    )

                loop_result = await self._tool_loop.respond(
                    prompt,
                    on_search_start=on_search_start,
                )
                result = loop_result.response
                sources = loop_result.sources
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
            await self._send_sources(
                presenter,
                chat_id=turn.chat_id,
                sources=sources,
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
        if self._confirmation_service is None:
            await self._send_command_error(
                callback.chat_id,
                presenter,
                _STALE_CALLBACK_TEXT,
            )
            return
        resolution = self._confirmation_service.resolve(
            token=token,
            owner_user_id=callback.user_id,
            confirmed=confirmed,
        )
        if resolution is None or (
            not resolution.first_resolution
            and resolution.action.action_type not in _REMINDER_ACTION_TYPES
        ):
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        action = resolution.action
        if not confirmed:
            if (
                action.action_type == "activate_reminder"
                and self._reminder_store is not None
            ):
                task_id = action.payload.get("task_id")
                if isinstance(task_id, int) and not isinstance(task_id, bool):
                    try:
                        self._reminder_store.cancel_task(
                            task_id=task_id,
                            owner_user_id=callback.user_id,
                        )
                    except Exception:
                        pass
            await self._send_callback_result(callback, presenter, "Отменено.")
            return
        if action.action_type in _REMINDER_ACTION_TYPES:
            await self._execute_reminder_action(callback, presenter, action)
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

    async def _execute_reminder_action(
        self,
        callback: IncomingTelegramCallback,
        presenter: TelegramResponsePort,
        action: ConfirmationAction,
    ) -> None:
        task_id = action.payload.get("task_id")
        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
            or self._reminder_store is None
        ):
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        try:
            if action.action_type == "activate_reminder":
                job = self._reminder_store.activate(
                    task_id=task_id,
                    owner_user_id=callback.user_id,
                )
                if job.status is ReminderJobStatus.NEEDS_REVIEW:
                    text = (
                        "Время попало в переход часового пояса. "
                        "Создайте напоминание с другим временем."
                    )
                else:
                    text = (
                        "Напоминание создано: "
                        f"{_format_local_datetime(job.local_start)} "
                        f"({job.timezone_name})."
                    )
            elif action.action_type == "cancel_reminder":
                self._reminder_store.cancel_task(
                    task_id=task_id,
                    owner_user_id=callback.user_id,
                )
                text = "Напоминание отменено."
            elif action.action_type == "complete_reminder":
                self._reminder_store.complete_task(
                    task_id=task_id,
                    owner_user_id=callback.user_id,
                )
                text = "Задача выполнена."
            elif action.action_type == "snooze_reminder":
                delay_seconds = action.payload.get("delay_seconds")
                if (
                    isinstance(delay_seconds, bool)
                    or not isinstance(delay_seconds, int)
                    or delay_seconds != _SNOOZE_SECONDS
                ):
                    raise ValueError("reminder snooze payload is invalid")
                run = self._reminder_store.snooze_task(
                    task_id=task_id,
                    owner_user_id=callback.user_id,
                    action_token=action.token,
                    delay_seconds=delay_seconds,
                )
                text = (
                    "Напомню снова через 10 минут."
                    if run.status is ReminderRunStatus.DUE
                    else "Не удалось отложить напоминание из-за quiet hours."
                )
            else:
                raise ValueError("unknown reminder action")
        except (KeyError, PermissionError, RuntimeError, ValueError):
            await self._send_callback_result(
                callback,
                presenter,
                _STALE_CALLBACK_TEXT,
                kind=TelegramResponseKind.ERROR,
            )
            return
        await self._send_callback_result(callback, presenter, text)

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

    async def _update_search_status(
        self,
        presenter: TelegramResponsePort,
        *,
        chat_id: int,
        status_handle: TelegramSentMessage | None,
        query: str,
    ) -> None:
        if (
            status_handle is None
            or status_handle.chat_id != chat_id
            or not isinstance(presenter, TelegramEditableResponsePort)
        ):
            return
        try:
            await presenter.edit(
                status_handle,
                TelegramResponse(
                    chat_id=chat_id,
                    kind=TelegramResponseKind.STATUS,
                    text=f"ищу: «{_snippet(query, limit=80)}»",
                ),
            )
        except Exception:
            pass

    async def _send_sources(
        self,
        presenter: TelegramResponsePort,
        *,
        chat_id: int,
        sources: tuple[SearchResult, ...],
    ) -> None:
        if not sources:
            return
        text = render_sources_html(
            tuple(
                TelegramWebSource(title=source.title, url=source.url)
                for source in sources
            )
        )
        if not text:
            return
        try:
            await presenter.send(
                TelegramResponse(
                    chat_id=chat_id,
                    kind=TelegramResponseKind.NOTICE,
                    text=text,
                    parse_mode=TelegramParseMode.HTML,
                )
            )
        except Exception:
            pass

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

        if command.name == "remind":
            await self._handle_remind_command(message, command, presenter)
            return None

        if command.name == "tasks":
            await self._handle_tasks(message, command, presenter)
            return None

        if command.name == "timezone":
            await self._handle_timezone(message, command, presenter)
            return None

        if command.name == "quiet":
            await self._handle_quiet(message, command, presenter)
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
        if self._confirmation_service is None:
            return None
        try:
            token = self._confirmation_service.request(
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

    async def _handle_remind_command(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        request = " ".join(command.arguments).strip()
        if not request:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /remind <что и когда>.",
            )
            return
        await self._handle_reminder_request(message, request, presenter)

    async def _handle_reminder_request(
        self,
        message: IncomingTelegramMessage,
        request: str,
        presenter: TelegramResponsePort,
    ) -> None:
        if (
            self._reminder_store is None
            or self._reminder_parser is None
            or self._confirmation_service is None
        ):
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Напоминания сейчас недоступны.",
            )
            return
        if not request:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Напишите, что и когда напомнить.",
            )
            return
        try:
            policy = self._reminder_store.ensure_profile(
                owner_user_id=message.user_id
            )
            turn = self._router.route(
                IncomingTelegramMessage(
                    user_id=message.user_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    text=request,
                )
            )
            if turn is None:
                return
            persona_id = self._reminder_store.persona_id_for_key(turn.persona_key)
            if persona_id is None:
                raise RuntimeError("active reminder persona is unavailable")
            parsed = await asyncio.to_thread(
                self._reminder_parser.parse,
                request,
                default_timezone=policy.timezone_name,
            )
            created = self._reminder_store.create_draft(
                ReminderDraft(
                    owner_user_id=message.user_id,
                    persona_id=persona_id,
                    chat_id=message.chat_id,
                    text=parsed.text,
                    local_start=parsed.local_start,
                    timezone_name=parsed.timezone_name,
                    recurrence=parsed.recurrence,
                    quiet_start_minute=policy.quiet_start_minute,
                    quiet_end_minute=policy.quiet_end_minute,
                    urgent=parsed.urgent,
                )
            )
            self._reminder_store.mark_awaiting_confirmation(
                task_id=created.task.id,
                owner_user_id=message.user_id,
            )
            token = self._confirmation_service.request(
                owner_user_id=message.user_id,
                action_type="activate_reminder",
                payload={"task_id": created.task.id},
            )
        except Exception:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Не удалось разобрать напоминание. Уточните дату и время.",
            )
            return
        recurrence = _format_recurrence(parsed)
        prompt = (
            f"Создать напоминание «{parsed.text}» на "
            f"{_format_local_datetime(parsed.local_start)} "
            f"({parsed.timezone_name}){recurrence}?"
        )
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(message, prompt),
                inline_keyboard=(
                    (
                        TelegramInlineButton(
                            text="Создать",
                            callback_data=confirmation_callback_data(
                                "confirm",
                                token,
                            ),
                        ),
                        TelegramInlineButton(
                            text="Отмена",
                            callback_data=confirmation_callback_data(
                                "cancel",
                                token,
                            ),
                        ),
                    ),
                ),
            )
        )

    async def _handle_tasks(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if command.arguments:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /tasks.",
            )
            return
        if self._reminder_store is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Напоминания сейчас недоступны.",
            )
            return
        try:
            tasks = self._reminder_store.list_tasks(
                owner_user_id=message.user_id,
                limit=_REMINDER_TASK_LIMIT,
            )
        except Exception:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Не удалось загрузить задачи.",
            )
            return
        if not tasks:
            await presenter.send(
                TelegramResponse(
                    chat_id=message.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=self._command_voice(message, "Задач пока нет."),
                )
            )
            return
        lines = ["Задачи:"]
        keyboard = []
        for task in tasks:
            job = self._reminder_store.get_job_for_task(
                task_id=task.id,
                owner_user_id=message.user_id,
            )
            status = _task_status_label(task.status)
            schedule = ""
            if job is not None and job.next_scheduled_for is not None:
                schedule = (
                    " · следующее: "
                    f"{_format_utc_datetime(job.next_scheduled_for)}"
                )
            lines.append(
                f"{task.id}. {task.text} · {status}{schedule}"
            )
            buttons = self._reminder_task_buttons(
                owner_user_id=message.user_id,
                task=task,
            )
            if buttons:
                keyboard.append(buttons)
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(message, "\n".join(lines)),
                inline_keyboard=tuple(keyboard),
            )
        )

    def _reminder_task_buttons(
        self,
        *,
        owner_user_id: int,
        task: TaskRecord,
    ) -> tuple[TelegramInlineButton, ...]:
        if self._confirmation_service is None:
            return ()
        actions = []
        if task.status is TaskStatus.ACTIVE:
            actions.extend(
                (
                    ("Через 10 минут", "snooze_reminder", {"delay_seconds": _SNOOZE_SECONDS}),
                    ("Готово", "complete_reminder", {}),
                    ("Отменить", "cancel_reminder", {}),
                )
            )
        elif task.status in {
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.NEEDS_REVIEW,
        }:
            actions.append(("Отменить", "cancel_reminder", {}))
        buttons = []
        for label, action_type, extra_payload in actions:
            try:
                token = self._confirmation_service.request(
                    owner_user_id=owner_user_id,
                    action_type=action_type,
                    payload={"task_id": task.id, **extra_payload},
                )
                data = confirmation_callback_data("confirm", token)
            except Exception:
                continue
            buttons.append(TelegramInlineButton(text=label, callback_data=data))
        return tuple(buttons)

    async def _handle_timezone(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if len(command.arguments) > 1 or self._reminder_store is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /timezone [IANA timezone].",
            )
            return
        try:
            policy = self._reminder_store.ensure_profile(
                owner_user_id=message.user_id
            )
            if command.arguments:
                policy = self._reminder_store.set_profile_policy(
                    owner_user_id=message.user_id,
                    timezone_name=command.arguments[0],
                    quiet_start_minute=policy.quiet_start_minute,
                    quiet_end_minute=policy.quiet_end_minute,
                )
        except (KeyError, ValueError):
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Неизвестная timezone. Используйте IANA-имя, например Europe/Moscow.",
            )
            return
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(
                    message,
                    f"Timezone: {policy.timezone_name}.",
                ),
            )
        )

    async def _handle_quiet(
        self,
        message: IncomingTelegramMessage,
        command: TelegramCommand,
        presenter: TelegramResponsePort,
    ) -> None:
        if len(command.arguments) > 1 or self._reminder_store is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Формат команды: /quiet [HH:MM-HH:MM|off].",
            )
            return
        try:
            policy = self._reminder_store.ensure_profile(
                owner_user_id=message.user_id
            )
            if command.arguments:
                value = command.arguments[0]
                quiet = (
                    (None, None)
                    if value.casefold() == "off"
                    else _parse_quiet_range(value)
                )
                policy = self._reminder_store.set_profile_policy(
                    owner_user_id=message.user_id,
                    timezone_name=policy.timezone_name,
                    quiet_start_minute=quiet[0],
                    quiet_end_minute=quiet[1],
                )
        except (KeyError, ValueError):
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Quiet hours задаются как HH:MM-HH:MM или off.",
            )
            return
        quiet_text = _format_quiet_policy(policy)
        await presenter.send(
            TelegramResponse(
                chat_id=message.chat_id,
                kind=TelegramResponseKind.FINAL,
                text=self._command_voice(message, quiet_text),
            )
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
        if self._confirmation_service is None:
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Подтверждения сейчас недоступны.",
            )
            return
        try:
            token = self._confirmation_service.request(
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


def _format_local_datetime(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def _format_utc_datetime(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M UTC")


def _format_recurrence(parsed: ParsedReminder) -> str:
    rule = parsed.recurrence
    if rule.kind is ScheduleKind.ONCE:
        return ""
    if rule.kind is ScheduleKind.DAILY:
        description = f", каждые {rule.interval} дн."
    elif rule.kind is ScheduleKind.WEEKLY:
        weekdays = ",".join(str(day + 1) for day in rule.weekdays)
        description = f", еженедельно по дням {weekdays}"
    else:
        monthday = rule.monthday or parsed.local_start.day
        description = f", ежемесячно {monthday}-го числа"
    if rule.count is not None:
        description += f", {rule.count} раз"
    return description


def _task_status_label(status: TaskStatus) -> str:
    return {
        TaskStatus.AWAITING_CONFIRMATION: "ждёт подтверждения",
        TaskStatus.ACTIVE: "активна",
        TaskStatus.NEEDS_REVIEW: "нужно уточнить время",
        TaskStatus.COMPLETED: "выполнена",
        TaskStatus.CANCELLED: "отменена",
    }[status]


def _parse_quiet_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", value)
    if match is None:
        raise ValueError("quiet hours format is invalid")
    start_hour, start_minute, end_hour, end_minute = (
        int(part) for part in match.groups()
    )
    if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
        raise ValueError("quiet hours value is invalid")
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        raise ValueError("quiet hours range cannot be empty")
    return start, end


def _format_quiet_policy(policy: ProfileReminderPolicy) -> str:
    if policy.quiet_start_minute is None or policy.quiet_end_minute is None:
        return "Quiet hours выключены."
    start = divmod(policy.quiet_start_minute, 60)
    end = divmod(policy.quiet_end_minute, 60)
    return (
        "Quiet hours: "
        f"{start[0]:02d}:{start[1]:02d}-{end[0]:02d}:{end[1]:02d}."
    )
