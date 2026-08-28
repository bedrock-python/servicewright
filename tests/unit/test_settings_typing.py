"""Type-level tests: a settings model may narrow its observability sections.

The section protocols are structural, and a mutable attribute matches
invariantly — so a narrowed field (issue #8) can only be pinned by running a
type checker over a reproduction and reading its verdict.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

mypy = pytest.importorskip("mypy")

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_TEMPLATE = """\
from typing import Literal

from servicewright import BaseServiceSettingsProtocol

{sections}

class Settings:
{fields}

    def get_app_version(self) -> str:
        return "1.0.0"


def takes_protocol(settings: BaseServiceSettingsProtocol) -> None: ...


takes_protocol(Settings())
"""

_NARROWED_LOGGING = _TEMPLATE.format(
    sections="""\
class Logging:
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    use_json: bool = True
""",
    fields="""\
    logging: Logging = Logging()
    metrics: None = None
    error_tracking: None = None
    tracing: None = None\
""",
)

_NARROWED_ERROR_TRACKING = _TEMPLATE.format(
    sections="""\
class ErrorTracking:
    dsn: str = "https://key@example.invalid/1"
    environment: Literal["local", "prod"] = "local"
    traces_sample_rate: float = 0.1
    profiles_sample_rate: float = 0.0
    debug: bool = False
""",
    fields="""\
    logging: None = None
    metrics: None = None
    error_tracking: ErrorTracking = ErrorTracking()
    tracing: None = None\
""",
)

_WIDE_LOGGING = _TEMPLATE.format(
    sections="""\
class Logging:
    level: str = "INFO"
    use_json: bool = True
""",
    fields="""\
    logging: Logging = Logging()
    metrics: None = None
    error_tracking: None = None
    tracing: None = None\
""",
)

_INCOMPLETE_LOGGING = _TEMPLATE.format(
    sections="""\
class Logging:
    level: str = "INFO"
""",
    fields="""\
    logging: Logging = Logging()
    metrics: None = None
    error_tracking: None = None
    tracing: None = None\
""",
)


def _run_mypy(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    snippet = tmp_path / "settings_snippet.py"
    snippet.write_text(source)

    return subprocess.run(  # noqa: S603 - fixed argv, only the snippet path varies
        [sys.executable, "-m", "mypy", "--config-file", str(ROOT / "pyproject.toml"), str(snippet)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_NARROWED_LOGGING, id="literal-log-level"),
        pytest.param(_NARROWED_ERROR_TRACKING, id="non-optional-dsn-and-literal-environment"),
        pytest.param(_WIDE_LOGGING, id="wide-str-log-level"),
    ],
)
def test__settings_model__sections_declare_types_at_least_as_narrow__satisfies_the_protocol(
    source: str,
    tmp_path: Path,
) -> None:
    # Act
    result = _run_mypy(source, tmp_path)

    # Assert
    assert result.returncode == 0, result.stdout
    assert "Success:" in result.stdout, result.stdout


def test__settings_model__section_missing_a_declared_member__fails_the_protocol(tmp_path: Path) -> None:
    # Act
    result = _run_mypy(_INCOMPLETE_LOGGING, tmp_path)

    # Assert
    assert result.returncode != 0, result.stdout
