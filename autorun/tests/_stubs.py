"""Shared test bootstrap for a bare interpreter.

`client` pulls in cryptography / requests / rich / textual at module scope, so
tests that import it cannot run on a machine without those wheels (for example
the jailbroken device used to capture protocol traffic).  Installing stub
modules keeps the pure-logic tests runnable everywhere.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

STUB_ROOTS = ("cryptography", "requests", "rich", "textual")


class _StubModule(types.ModuleType):
    """Module whose missing attributes are fresh subclassable dummy classes."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        attr = type(name, (), {"__init__": lambda self, *a, **k: None})
        setattr(self, name, attr)
        return attr


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = _StubModule(spec.name)
        module.__path__ = []  # mark as a package so submodule imports resolve
        return module

    def exec_module(self, module):
        pass


class StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in STUB_ROOTS:
            return None
        return importlib.machinery.ModuleSpec(fullname, _StubLoader())


def install_stubs() -> None:
    """Idempotently make `client` importable on a bare interpreter."""
    if not any(isinstance(finder, StubFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, StubFinder())
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
