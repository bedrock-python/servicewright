"""APScheduler 3.x <-> 4.x adapter conformance (AST-only; no SDK import).

The two scheduler adapters MUST expose an identical public surface so a service
migrates 3<->4 by changing one import line + one extra. This test parses the
source of both adapters via :mod:`ast` (it never imports the SDK-bound entrypoint
modules, so neither adapter's import guard fires) and asserts symbol-set +
signature parity.

Behavioural parity is verified separately by a 2-env CI matrix (one venv per
APScheduler major); it cannot run in a single environment because APScheduler 3.x
and 4.x are the same distribution.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pathlib

import pytest

import servicewright

pytestmark = pytest.mark.unit

_ADAPTERS = pathlib.Path(servicewright.__file__).parent / "adapters"
_A3 = _ADAPTERS / "apscheduler3"
_A4 = _ADAPTERS / "apscheduler4"


def _module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _dunder_all(path: pathlib.Path) -> list[str]:
    for node in ast.walk(_module(path)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(node.value, ast.List):
                    return sorted(
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )
    return []


def _classdef(path: pathlib.Path, name: str) -> ast.ClassDef:
    for node in ast.walk(_module(path)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found in {path}")


def _public_methods(cls: ast.ClassDef) -> set[str]:
    out: set[str] = set()
    for n in cls.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and (
            not n.name.startswith("_") or n.name == "__init__"
        ):
            out.add(n.name)
    return out


def _init_params(cls: ast.ClassDef) -> list[str]:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "__init__":
            a = n.args
            return [arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return []


def _dataclass_fields(path: pathlib.Path, name: str) -> list[str]:
    cls = _classdef(path, name)
    return [n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]


def test__scheduler_adapters__public_exports__are_identical() -> None:
    a3 = _dunder_all(_A3 / "__init__.py")
    assert a3, "apscheduler3 __init__ exports nothing"
    assert a3 == _dunder_all(_A4 / "__init__.py")


def test__scheduled_job__dataclass_fields__are_identical() -> None:
    assert _dataclass_fields(_A3 / "config.py", "ScheduledJob") == _dataclass_fields(_A4 / "config.py", "ScheduledJob")


def test__scheduler_entrypoint__init_signature__is_identical() -> None:
    e3 = _classdef(_A3 / "entrypoint.py", "SchedulerEntrypoint")
    e4 = _classdef(_A4 / "entrypoint.py", "SchedulerEntrypoint")
    assert _init_params(e3) == _init_params(e4)


def test__scheduler_entrypoint__public_methods__are_identical() -> None:
    e3 = _classdef(_A3 / "entrypoint.py", "SchedulerEntrypoint")
    e4 = _classdef(_A4 / "entrypoint.py", "SchedulerEntrypoint")
    assert _public_methods(e3) == _public_methods(e4)


def test__scheduler_plugin__present_in_both_adapters__has_the_same_name() -> None:
    assert _classdef(_A3 / "entrypoint.py", "SchedulerPlugin").name == "SchedulerPlugin"
    assert _classdef(_A4 / "entrypoint.py", "SchedulerPlugin").name == "SchedulerPlugin"


@pytest.mark.skipif(
    importlib.util.find_spec("apscheduler.schedulers") is not None,
    reason="the guard can only fire where APScheduler 3.x is absent",
)
def test__apscheduler3_package__imported_without_the_extra__raises_with_the_install_hint() -> None:
    # The gated adapter package requires the extra: with APScheduler 4.x installed,
    # importing it fails on 3.x's AsyncIOScheduler and the guard re-raises a friendly
    # ImportError pointing at the extra (same hard-raise contract as apscheduler4).
    with pytest.raises(ImportError, match=r"servicewright\[apscheduler3\]"):
        importlib.import_module("servicewright.adapters.apscheduler3")
