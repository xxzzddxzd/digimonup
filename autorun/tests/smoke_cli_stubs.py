"""Import-level smoke test for main.py CLI wiring without real dependencies.

Installs recursive stub modules for cryptography / requests / rich / textual so
`main` can be imported on a bare interpreter, then exercises the gacha argument
validation paths (which return before any network/session work).
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

STUB_ROOTS = ("cryptography", "requests", "rich", "textual")


class StubModule(types.ModuleType):
    """Module whose missing attributes are fresh subclassable dummy classes."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        attr = type(name, (), {"__init__": lambda self, *a, **k: None})
        setattr(self, name, attr)
        return attr


class StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root not in STUB_ROOTS:
            return None
        return importlib.machinery.ModuleSpec(fullname, StubLoader())


class StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = StubModule(spec.name)
        module.__path__ = []  # mark as package so submodule imports resolve
        return module

    def exec_module(self, module):
        pass


def main() -> int:
    sys.meta_path.insert(0, StubFinder())
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    sys.argv = ["main.py"]
    import importlib

    main_mod = importlib.import_module("main")

    failures: list[str] = []

    # 1) banner validation rejects before touching the network
    for argv, expect in (
        (["gacha"], 2),              # missing banner -> 2
        (["gacha", "3"], 2),         # unknown banner -> 2
        (["gacha", "1", "-c", "0"], 2),  # non-positive count -> 2
    ):
        sys.argv = ["main.py"] + argv
        try:
            code = main_mod.main()
        except SystemExit as exc:  # argparse errors exit(2)
            code = int(exc.code or 0)
        status = "ok" if code == expect else "FAIL"
        if status == "FAIL":
            failures.append(f"{argv}: got {code}, want {expect}")
        print(f"[{status}] main.py {' '.join(argv)} -> {code}")

    # 2) banner mapping + pull arithmetic
    cases = {
        "1": main_mod.gasha_api.GASHA_PARTNER,
        "2": main_mod.gasha_api.GASHA_SP,
    }
    for banner, key in cases.items():
        got = main_mod.GACHA_BANNERS.get(banner)
        status = "ok" if got == key else "FAIL"
        if status == "FAIL":
            failures.append(f"banner {banner}: got {got}, want {key}")
        print(f"[{status}] GACHA_BANNERS[{banner}] = {got}")

    status = "ok" if main_mod.GACHA_PULL_SIZE == 30 else "FAIL"
    if status == "FAIL":
        failures.append(f"GACHA_PULL_SIZE={main_mod.GACHA_PULL_SIZE}")
    print(f"[{status}] GACHA_PULL_SIZE = {main_mod.GACHA_PULL_SIZE}")

    # 3) gasha_care alias resolution still works for legacy aliases
    from client.gasha_care import GASHA_ALIASES, resolve_key

    for alias, key in (
        ("1", main_mod.gasha_api.GASHA_PARTNER),
        ("2", main_mod.gasha_api.GASHA_SP),
        ("ga1", main_mod.gasha_api.GASHA_PARTNER),
        ("ga2", main_mod.gasha_api.GASHA_SP),
    ):
        got = resolve_key(alias)
        status = "ok" if got == key else "FAIL"
        if status == "FAIL":
            failures.append(f"resolve_key({alias})={got}, want {key}")
        print(f"[{status}] resolve_key({alias}) = {got}")
    assert set(GASHA_ALIASES) >= {"1", "2", "ga1", "ga2"}

    # 4) help text mentions gacha examples
    import io
    import contextlib

    buf = io.StringIO()
    sys.argv = ["main.py"]
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            main_mod.main()
        except SystemExit:
            pass
    help_text = buf.getvalue()
    for needle in ("gacha 1", "gacha 2 -c 3"):
        status = "ok" if needle in help_text else "FAIL"
        if status == "FAIL":
            failures.append(f"help missing example: {needle}")
        print(f"[{status}] help contains '{needle}'")

    if failures:
        print("\nSMOKE FAILED:")
        for line in failures:
            print(" -", line)
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
