import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from .action_confirmation import ActionConfirmationService
from .reminder_store import DeliveryLease, ReminderRun
from .telegram_presentation import (
    TelegramInlineButton,
    TelegramResponse,
    TelegramResponseKind,
    TelegramResponsePort,
    TelegramSentMessage,
    confirmation_callback_data,
)


_SNOOZE_SECONDS = 600


class ReminderWorkerStore(Protocol):
    def materialize_due(self, *, limit: int = 100) -> tuple[ReminderRun, ...]: ...

    def lease_delivery(self, *, lease_seconds: int = 60) -> DeliveryLease | None: ...

    def delivery_is_current(self, lease: DeliveryLease) -> bool: ...

    def mark_delivery_sent(
        self,
        lease: DeliveryLease,
        *,
        telegram_message_id: int,
    ) -> bool: ...

    def release_delivery(
        self,
        lease: DeliveryLease,
        *,
        error_kind: str,
        retry_delay_seconds: int,
    ) -> bool: ...


class ReminderScheduler:
    def __init__(self, store: ReminderWorkerStore) -> None:
        self._store = store

    def run_once(self, *, limit: int = 100) -> tuple[ReminderRun, ...]:
        return self._store.materialize_due(limit=limit)


class ReminderDeliveryWorker:
    def __init__(
        self,
        store: ReminderWorkerStore,
        confirmation_service: ActionConfirmationService,
        *,
        retry_delay_seconds: int = 30,
    ) -> None:
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or retry_delay_seconds < 1
            or retry_delay_seconds > 3600
        ):
            raise ValueError("reminder retry delay is invalid")
        self._store = store
        self._confirmation_service = confirmation_service
        self._retry_delay_seconds = retry_delay_seconds
        self._quiesced_epochs: set[tuple[int, int]] = set()

    async def run_once(self, port: TelegramResponsePort) -> bool:
        lease = self._store.lease_delivery()
        if lease is None:
            return False
        if (
            (lease.owner_user_id, lease.lifecycle_epoch) in self._quiesced_epochs
            or not self._store.delivery_is_current(lease)
        ):
            return True
        try:
            sent = await port.send(
                TelegramResponse(
                    chat_id=lease.chat_id,
                    kind=TelegramResponseKind.FINAL,
                    text=f"напоминание: {lease.text}",
                    inline_keyboard=(self._action_buttons(lease),),
                )
            )
            if (
                not isinstance(sent, TelegramSentMessage)
                or sent.chat_id != lease.chat_id
                or sent.message_id < 1
            ):
                raise RuntimeError("Telegram reminder delivery returned no handle")
        except Exception:
            try:
                self._store.release_delivery(
                    lease,
                    error_kind="telegram_unavailable",
                    retry_delay_seconds=self._retry_delay_seconds,
                )
            except Exception:
                pass
            return True
        self._store.mark_delivery_sent(
            lease,
            telegram_message_id=sent.message_id,
        )
        return True

    def quiesce(self, owner_user_id: int, lifecycle_epoch: int) -> None:
        self._quiesced_epochs.add((owner_user_id, lifecycle_epoch))

    def _action_buttons(
        self,
        lease: DeliveryLease,
    ) -> tuple[TelegramInlineButton, ...]:
        actions = (
            (
                "Через 10 минут",
                "snooze_reminder",
                {"task_id": lease.task_id, "delay_seconds": _SNOOZE_SECONDS},
            ),
            ("Готово", "complete_reminder", {"task_id": lease.task_id}),
            ("Отменить", "cancel_reminder", {"task_id": lease.task_id}),
        )
        buttons = []
        for label, action_type, payload in actions:
            try:
                token = self._confirmation_service.request(
                    owner_user_id=lease.owner_user_id,
                    action_type=action_type,
                    payload=payload,
                )
                data = confirmation_callback_data("confirm", token)
            except Exception:
                continue
            buttons.append(TelegramInlineButton(text=label, callback_data=data))
        return tuple(buttons)


async def run_reminder_loop(
    scheduler: ReminderScheduler,
    worker: ReminderDeliveryWorker,
    port: TelegramResponsePort,
    *,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("reminder poll interval must be positive")
    while True:
        try:
            scheduler.run_once()
            for _ in range(100):
                if not await worker.run_once(port):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await sleep(poll_interval_seconds)
