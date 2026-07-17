import asyncio
from typing import Protocol

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


class TelegramApplicationService:
    def __init__(
        self,
        router: TelegramRouter,
        persona_catalog: PersonaCatalog,
        provider: TextProvider,
        response_port: TelegramResponsePort | None = None,
    ) -> None:
        self._router = router
        self._persona_catalog = persona_catalog
        self._provider = provider
        self._response_port = response_port

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
            result = await asyncio.to_thread(
                self._provider.respond,
                self._build_prompt(persona, turn),
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

        if command.name != "persona":
            await self._send_command_error(
                message.chat_id,
                presenter,
                "Неизвестная команда. Используйте /persona <key>.",
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
