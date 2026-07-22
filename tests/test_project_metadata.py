import re
from pathlib import Path
import tomllib

from lenkobot.runtime import load_runtime_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_is_ready_for_an_open_source_release():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    assert project["version"] != "0.0.0"
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["urls"] == {
        "Documentation": "https://github.com/bunchtrail/LenkoBot/tree/main/docs",
        "Issues": "https://github.com/bunchtrail/LenkoBot/issues",
        "Repository": "https://github.com/bunchtrail/LenkoBot",
    }
    assert (PROJECT_ROOT / project["readme"]).is_file()
    assert (PROJECT_ROOT / "LICENSE").is_file()


def test_open_source_community_contract_is_present():
    required_files = (
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "SECURITY.md",
        ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md",
    )

    assert all((PROJECT_ROOT / path).is_file() for path in required_files)


def test_public_minimal_config_is_loadable():
    settings = load_runtime_settings(PROJECT_ROOT / "examples/config.minimal.toml")

    assert settings.allowed_user_id == 123456789
    assert settings.persona_catalog.default_persona_key == "companion"
    assert settings.web_search is not None
