# Contributing to servicewright

Thank you for your interest in contributing! This document covers everything you need to get started.

## Development setup

```bash
git clone https://github.com/bedrock-python/servicewright.git
cd servicewright
uv sync --group dev
uv run pre-commit install --hook-type commit-msg
```

## Running checks

```bash
make check            # ruff lint + format check + mypy
make test-unit        # unit tests, no Docker required
make test-integration # integration tests, requires Docker
make test             # full suite with 90% coverage threshold
```

## Code style

- **Type hints** on all functions and methods, including tests
- **Docstrings** on public API only — Google style
- **Line length** — 120 characters (ruff enforced)
- **Quotes** — double quotes (ruff enforced)
- **No comments** unless the *why* is non-obvious

## Testing

- **One behavior per test** — a test that needs "and" in its name is two tests
- **AAA pattern** — Arrange → Act → Assert, in that order, separated by blank lines
- **Fixtures for repeated setup** — never copy-paste arrangement between tests
- **Parametrize to deduplicate** — tests differing only by input/output become one
  `pytest.mark.parametrize`; use `pytest_lazy_fixtures.lf()` to pass fixtures as params
- **Naming** — `test__subject__condition__expectedresult` (double underscores between the
  three parts)
- **Fixture placement** — `conftest.py` only for fixtures shared across multiple modules;
  otherwise keep fixtures in the test file that uses them

```python
import pytest
from pytest_lazy_fixtures import lf


@pytest.fixture
def uuid_request_id() -> str:
    return "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (lf("uuid_request_id"), True),
        ("tenant_42", True),
        ("header\ninjection", False),
        ("", False),
    ],
)
def test__is_safe_context_id__correlation_id_value__reports_whether_it_is_loggable(
    value: str,
    expected: bool,
) -> None:
    # Act
    result = is_safe_context_id(value)

    # Assert
    assert result is expected
```

Every test module declares `pytestmark = pytest.mark.unit` (or `integration`) explicitly —
the suite is selected by marker in CI, so an unmarked module silently stops running.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) are enforced by pre-commit:

| Prefix | Use for |
|--------|---------|
| `feat:` | New feature or behaviour |
| `fix:` | Bug fix |
| `docs:` | Documentation only |
| `test:` | Test additions or changes |
| `refactor:` | Code restructure, no behaviour change |
| `perf:` | Performance improvement |
| `chore:` | Build, tooling, CI |

Breaking changes: add `!` after the type (`feat!:`) or include a `BREAKING CHANGE:` footer.

## Pull requests

1. Fork the repository
2. Create a branch from `master`: `git checkout -b feat/my-feature`
3. Make your changes with tests
4. Run `make check && make test-unit` locally
5. Open a PR against `master`

## The agents page

`docs/agents.md` is the whole library on one page, written for a coding assistant: the
public API, the startup and shutdown ordering that breaks code when it is broken, which
batteries are opt-in, the mistakes models make, and a map of which page to fetch for the
rest. People hand it to an assistant instead of the site, which is what makes a stale one
worse than none — it teaches a model an API that no longer exists.

It is part of the public API, so it changes in the same pull request the API does: a name
added, renamed or removed, a changed default or signature, a new rule a caller has to obey,
a new extra. A new docs page means a new row in the documentation map. The review check is
mechanical — if the diff changes the public surface and `docs/agents.md` is untouched, the
pull request is not finished.

## Releasing (maintainers only)

Releases are fully automated via [Release Please](https://github.com/googleapis/release-please).
Merge a PR with conventional commits → Release Please creates a release PR → merge it → PyPI publish happens automatically.
