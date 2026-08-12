.PHONY: test test-unit test-integration test-apscheduler3 fmt check build install docs-serve docs-build clean

# [apscheduler3] and [apscheduler4] are mutually exclusive (same distribution, see
# [tool.uv].conflicts), so the dev/test environment installs [all], which carries
# apscheduler4. Here the apscheduler3 adapter is covered by the AST conformance
# test and its behavioural tests skip; the `test-apscheduler3` CI job installs the
# conflicting extra in its own environment and runs them for real.
#
#   make test-apscheduler3   # reproduce that job locally, in a throwaway venv
EXTRAS := --extra all

install:
	uv sync --group dev $(EXTRAS)

fmt:
	uv run --no-sync ruff format .
	uv run --no-sync ruff check --fix .

check:
	uv run --no-sync ruff check .
	uv run --no-sync ruff format --check .
	uv run --no-sync mypy
	uv run --no-sync lint-imports

test-unit:
	uv run --no-sync pytest -m unit

# Reproduces the `test-apscheduler3` CI job: APScheduler 3.x cannot share an
# environment with the 4.x the main venv carries, so this builds its own.
test-apscheduler3:
	uv venv .venv-aps3 --python 3.12
	uv pip install --python .venv-aps3 . "apscheduler>=3.10,<4" pytest pytest-asyncio pytest-lazy-fixtures
	uv run --python .venv-aps3 --no-project pytest tests/unit/test_scheduler_aps3.py tests/unit/test_scheduler_conformance.py

test-integration:
	uv run --no-sync pytest -m integration

test:
	uv run --no-sync pytest --cov=servicewright --cov-report=term --cov-fail-under=90 --cov-report=xml:coverage.xml

build:
	uv build

docs-serve:
	python -c "import shutil; shutil.copy('CHANGELOG.md', 'docs/changelog.md')"
	uv run --no-dev --group docs zensical serve

docs-build:
	python -c "import shutil; shutil.copy('CHANGELOG.md', 'docs/changelog.md')"
	uv run --no-dev --group docs zensical build --clean

clean:
	python -c "import shutil, os, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build', 'site'] if os.path.exists(p)]; [os.remove(p) for p in ['.coverage', 'coverage.xml'] if os.path.exists(p)]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
