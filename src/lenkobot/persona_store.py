from datetime import datetime, timezone
import json
import sqlite3

from .personas import Persona, PersonaVersion, VoicePack, persona_content_hash


def ensure_persona_version(
    connection: sqlite3.Connection,
    persona: Persona,
    *,
    profile_id: str = "default",
    created_at: str | None = None,
) -> tuple[int, int]:
    timestamp = created_at or datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    )
    content_hash = persona_content_hash(persona)
    connection.execute(
        """
        INSERT INTO persona (
            profile_id, key, display_name, identity_prompt,
            identity_version, status
        )
        VALUES (?, ?, ?, ?, ?, 'active')
        ON CONFLICT(profile_id, key) DO UPDATE SET
            display_name = excluded.display_name,
            identity_prompt = excluded.identity_prompt,
            identity_version = excluded.identity_version,
            status = 'active'
        """,
        (
            profile_id,
            persona.key,
            persona.display_name,
            persona.identity_prompt,
            persona.identity_version,
        ),
    )
    persona_row = connection.execute(
        "SELECT id FROM persona WHERE profile_id = ? AND key = ?",
        (profile_id, persona.key),
    ).fetchone()
    if persona_row is None:
        raise RuntimeError("persona was not persisted")
    persona_id = int(persona_row["id"])
    newest_row = connection.execute(
        """
        SELECT MAX(identity_version) AS identity_version
        FROM persona_version
        WHERE persona_id = ?
        """,
        (persona_id,),
    ).fetchone()
    newest_version = newest_row["identity_version"]
    if newest_version is not None and persona.identity_version < int(newest_version):
        raise ValueError("persona identity_version cannot decrease")
    connection.execute(
        """
        INSERT OR IGNORE INTO persona_version (
            persona_id, identity_version, display_name, identity_prompt,
            voice_json, content_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            persona.identity_version,
            persona.display_name,
            persona.identity_prompt,
            json.dumps(
                persona.voice.as_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            content_hash,
            timestamp,
        ),
    )
    version_row = connection.execute(
        """
        SELECT id, content_hash
        FROM persona_version
        WHERE persona_id = ? AND identity_version = ?
        """,
        (persona_id, persona.identity_version),
    ).fetchone()
    if version_row is None:
        raise RuntimeError("persona version was not persisted")
    if str(version_row["content_hash"]) != content_hash:
        raise ValueError(
            "persona identity_version is immutable; increment identity_version"
        )
    version_id = int(version_row["id"])
    return persona_id, version_id


def persona_version_from_row(row: sqlite3.Row) -> PersonaVersion:
    try:
        voice_data = json.loads(str(row["voice_json"]))
    except json.JSONDecodeError as error:
        raise ValueError("stored persona voice is invalid") from error
    return PersonaVersion(
        id=int(row["id"]),
        persona_id=int(row["persona_id"]),
        key=str(row["key"]),
        display_name=str(row["display_name"]),
        identity_prompt=str(row["identity_prompt"]),
        identity_version=int(row["identity_version"]),
        voice=VoicePack.from_mapping(voice_data),
        content_hash=str(row["content_hash"]),
        created_at=str(row["created_at"]),
    )
