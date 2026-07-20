from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import string
import tomllib
from typing import Mapping


_VOICE_KINDS = ("status", "notice", "command", "error")
_VOICE_MAX_VARIANTS = 16
_VOICE_MAX_TEMPLATE_CHARS = 500
_VOICE_ALLOWED_PLACEHOLDERS = frozenset(
    {"persona", "key", "text", "page", "id", "count"}
)
_BANNED_VOICE_PHRASES = (
    "as an ai",
    "you are an ai",
    "an ai assistant",
    "how can i help",
    "как искусственный интеллект",
    "ваш запрос обработан",
    "чем могу помочь",
    "чем помочь",
)
_ANTI_TEMPLATE_MARKERS = (
    "canned",
    "robotic",
    "anti-template",
    "anti template",
    "канцелярит",
    "тик ассистента",
)


def _validate_template(template: object) -> str:
    if not isinstance(template, str) or not template.strip():
        raise ValueError("voice template must be a non-empty string")
    if len(template) > _VOICE_MAX_TEMPLATE_CHARS:
        raise ValueError("voice template exceeds the bounded limit")
    lowered = template.casefold()
    if any(phrase in lowered for phrase in _BANNED_VOICE_PHRASES):
        raise ValueError("voice template contains a banned phrase")
    try:
        parsed = tuple(string.Formatter().parse(template))
    except ValueError as error:
        raise ValueError("voice template format is invalid") from error
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if (
            not field_name
            or any(character in field_name for character in ".[]")
            or format_spec
            or conversion
        ):
            raise ValueError("voice template placeholder is invalid")
        if field_name not in _VOICE_ALLOWED_PLACEHOLDERS:
            raise ValueError("voice template placeholder is not allowlisted")
    return template


@dataclass(frozen=True, slots=True)
class VoicePack:
    status: tuple[str, ...] = ()
    notice: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    error: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for kind in _VOICE_KINDS:
            values = getattr(self, kind)
            if isinstance(values, str) or not isinstance(values, (tuple, list)):
                raise ValueError(f"voice {kind} collection must be an array")
            if len(values) > _VOICE_MAX_VARIANTS:
                raise ValueError(f"voice {kind} collection exceeds the bounded limit")
            normalized = tuple(_validate_template(value) for value in values)
            object.__setattr__(self, kind, normalized)

    @classmethod
    def from_mapping(cls, value: object) -> "VoicePack":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("personas.voice must be a table")
        unknown = set(value) - set(_VOICE_KINDS)
        if unknown:
            raise ValueError("personas.voice contains an unknown field")
        return cls(
            **{
                kind: value.get(kind, ())
                for kind in _VOICE_KINDS
            }
        )

    def render(
        self,
        kind: str,
        *,
        index: int = 0,
        fallback: str = "",
        **values: object,
    ) -> str:
        if kind not in _VOICE_KINDS:
            raise ValueError("unknown voice template kind")
        variants = getattr(self, kind)
        if not variants:
            return fallback
        template = variants[index % len(variants)]
        try:
            return template.format(**values)
        except (KeyError, ValueError, IndexError) as error:
            raise ValueError("voice template values are invalid") from error

    def as_dict(self) -> dict[str, list[str]]:
        return {kind: list(getattr(self, kind)) for kind in _VOICE_KINDS}


class VoiceRenderer:
    def __init__(self) -> None:
        self._indexes: dict[tuple[str, int, str], int] = {}

    def render(
        self,
        persona: "Persona",
        kind: str,
        *,
        fallback: str,
        **values: object,
    ) -> str:
        key = (persona.key, persona.identity_version, kind)
        index = self._indexes.get(key, 0)
        self._indexes[key] = index + 1
        template_values = {
            "persona": persona.display_name,
            "key": persona.key,
            **values,
        }
        return persona.voice.render(
            kind,
            index=index,
            fallback=fallback,
            **template_values,
        )


@dataclass(frozen=True, slots=True)
class PersonaVersion:
    id: int
    persona_id: int
    key: str
    display_name: str
    identity_prompt: str
    identity_version: int
    voice: VoicePack
    content_hash: str
    created_at: str

    def as_persona(self) -> "Persona":
        return Persona(
            key=self.key,
            display_name=self.display_name,
            identity_prompt=self.identity_prompt,
            identity_version=self.identity_version,
            voice=self.voice,
        )


def persona_content_hash(persona: "Persona") -> str:
    payload = {
        "key": persona.key,
        "display_name": persona.display_name,
        "identity_prompt": persona.identity_prompt,
        "identity_version": persona.identity_version,
        "voice": persona.voice.as_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    display_name: str
    identity_prompt: str
    identity_version: int
    voice: VoicePack = field(default_factory=VoicePack)


def lint_persona(persona: Persona) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(persona.identity_prompt, str) or not persona.identity_prompt.strip():
        errors.append("identity prompt must be a non-empty string")
        return tuple(errors)
    lowered_identity = persona.identity_prompt.casefold()
    if any(phrase in lowered_identity for phrase in _BANNED_VOICE_PHRASES):
        errors.append("identity prompt contains a banned phrase")
    has_voice_templates = any(
        getattr(persona.voice, kind) for kind in _VOICE_KINDS
    )
    if has_voice_templates and not any(
        marker in lowered_identity for marker in _ANTI_TEMPLATE_MARKERS
    ):
        errors.append("identity prompt lacks anti-template guidance")
    return tuple(errors)


class PersonaCatalog:
    def __init__(self, personas: tuple[Persona, ...], default_persona_key: str) -> None:
        if not personas:
            raise ValueError("persona catalog cannot be empty")

        for persona in personas:
            lint_errors = lint_persona(persona)
            if lint_errors:
                raise ValueError(lint_errors[0])

        by_key = {persona.key: persona for persona in personas}
        if len(by_key) != len(personas):
            raise ValueError("persona keys must be unique")
        if default_persona_key not in by_key:
            raise ValueError("default persona must exist in catalog")

        self._by_key = by_key
        self.default_persona_key = default_persona_key

    @property
    def personas(self) -> tuple[Persona, ...]:
        return tuple(self._by_key.values())

    @classmethod
    def from_toml(cls, path: Path) -> "PersonaCatalog":
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)

        raw_personas = data.get("personas", [])
        if not isinstance(raw_personas, list):
            raise ValueError("personas must be an array")
        personas = tuple(
            Persona(
                key=entry["key"],
                display_name=entry["display_name"],
                identity_prompt=entry["identity_prompt"],
                identity_version=entry["identity_version"],
                voice=VoicePack.from_mapping(entry.get("voice")),
            )
            for entry in raw_personas
        )
        for persona in personas:
            if persona.identity_version < 1:
                raise ValueError("persona identity_version must be positive")

        return cls(personas, data["default_persona_key"])

    def get(self, key: str) -> Persona | None:
        return self._by_key.get(key)
