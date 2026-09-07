"""Extra-gated subpackages name their extra when the extra is not installed.

Regression cover for issue #51: ``servicewright.adapters.fastapi`` raised
``ModuleNotFoundError: No module named 'starlette'`` in a bare install, because
several modules in the subpackage imported their third-party symbols directly
instead of through ``_imports.py``. Whichever unguarded import ran first was the
message the user got, and ``starlette`` is a name that appears in no extras
table. The contract (``docs/agents.md``, the adapters section and the errors
table) is that importing one of these without its extra raises an ``ImportError``
naming what to install.

The dev environment installs every extra, so absence is simulated in a fresh
interpreter: a ``sys.meta_path`` finder refuses everything the extra puts on the
path, which is what the import machinery does when the extra is genuinely
missing. Nothing is patched in this process, so no test can leak a half-imported
adapter into the next one.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

# The subpackage, the extra its message must name, and the top-level packages
# the extra puts on the path -- blocking all of them is the bare install.
_GATED_SUBPACKAGES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "fastapi",
        "fastapi",
        ("fastapi", "starlette", "uvicorn", "pydantic", "deadline_budget", "prometheus_fastapi_instrumentator"),
    ),
    ("litestar", "litestar", ("litestar", "uvicorn")),
    ("grpc", "grpc", ("grpc", "grpc_health", "grpc_server_kit")),
    ("apscheduler4", "apscheduler4", ("apscheduler",)),
    ("apscheduler3", "apscheduler3", ("apscheduler",)),
    ("dishka", "dishka", ("dishka",)),
    ("settings", "settings", ("pydantic", "pydantic_settings")),
]

# Imports ``sys.argv[1]`` with the comma-separated top-level packages in
# ``sys.argv[2]`` made unimportable, and prints the exception type and message.
_PROBE = """
import sys


class Blocker:
    def __init__(self, blocked):
        self._blocked = blocked

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in self._blocked:
            raise ModuleNotFoundError("No module named " + repr(fullname), name=fullname)
        return None


module, blocked = sys.argv[1], set(sys.argv[2].split(","))
sys.meta_path.insert(0, Blocker(blocked))
for name in [n for n in sys.modules if n.partition(".")[0] in blocked]:
    del sys.modules[name]

try:
    __import__(module)
except ImportError as exc:
    print(type(exc).__name__ + ": " + str(exc))
else:
    print("imported, with the extra blocked")
"""


def _import_without(module: str, blocked: tuple[str, ...]) -> str:
    """Return what importing ``module`` raises in an interpreter without ``blocked``."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _PROBE, module, ",".join(blocked)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("subpackage", "extra", "blocked"),
    _GATED_SUBPACKAGES,
    ids=[subpackage for subpackage, _, _ in _GATED_SUBPACKAGES],
)
def test__gated_subpackage__imported_without_its_extra__raises_import_error_naming_the_extra(
    subpackage: str, extra: str, blocked: tuple[str, ...]
) -> None:
    # Act
    raised = _import_without(f"servicewright.adapters.{subpackage}", blocked)

    # Assert: an ImportError of its own, not the bare ModuleNotFoundError the
    # machinery raises for a third-party package the user never asked for.
    assert raised.startswith("ImportError: "), raised
    assert f"servicewright[{extra}]" in raised, raised
