from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    display_name: str
    identity_prompt: str
    identity_version: int


class PersonaCatalog:
    def __init__(self, personas: tuple[Persona, ...], default_persona_key: str) -> None:
        if not personas:
            raise ValueError("persona catalog cannot be empty")

        by_key = {persona.key: persona for persona in personas}
        if len(by_key) != len(personas):
            raise ValueError("persona keys must be unique")
        if default_persona_key not in by_key:
            raise ValueError("default persona must exist in catalog")

        self._by_key = by_key
        self.default_persona_key = default_persona_key

    @classmethod
    def from_toml(cls, path: Path) -> "PersonaCatalog":
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)

        personas = tuple(
            Persona(
                key=entry["key"],
                display_name=entry["display_name"],
                identity_prompt=entry["identity_prompt"],
                identity_version=entry["identity_version"],
            )
            for entry in data.get("personas", [])
        )
        for persona in personas:
            if persona.identity_version < 1:
                raise ValueError("persona identity_version must be positive")

        return cls(personas, data["default_persona_key"])

    def get(self, key: str) -> Persona | None:
        return self._by_key.get(key)
