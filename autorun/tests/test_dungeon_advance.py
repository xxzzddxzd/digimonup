from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main as app
from client.dungeon_care import (
    advancing_dungeon_progress,
    run_advancing_dungeon_clear,
    run_advancing_dungeon_sweep,
)


class FakeSession:
    def __init__(self) -> None:
        self.client = type("Client", (), {"log_enabled": True})()
        self.heartbeats = 0

    def run_login_pipeline(self) -> dict:
        return {}

    def ensure_heartbeat(self) -> None:
        self.heartbeats += 1


class FakeApiClient:
    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = {path: list(items) for path, items in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        queue = self.responses.get(path)
        if not queue:
            raise AssertionError(f"unexpected request: {path} {body}")
        return queue.pop(0)


class DungeonAdvanceTests(unittest.TestCase):
    def run_in_temp(self, callback):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                return callback(Path(tmp))
            finally:
                os.chdir(previous)

    def test_advances_to_max_then_repeats_until_count(self) -> None:
        session = FakeSession()
        clear_levels: list[int] = []
        sweep_calls: list[tuple[int, bool]] = []

        def clear(_session, *, key: int, level: int, log):
            clear_levels.append(level)
            return {"ok": True, "key": key, "level": level, "cleared_level": level}

        def sweep(_session, *, key: int, level: int, reset_before: bool, log):
            sweep_calls.append((level, reset_before))
            return {"ok": True, "key": key, "level": level, "mode": "max_level_sweep"}

        def exercise(tmp: Path):
            with (
                patch.object(app, "_load_session", return_value=session),
                patch.object(
                    app,
                    "advancing_dungeon_progress",
                    return_value={
                        "key": 6,
                        "cleared_level": 98,
                        "next_level": 99,
                        "max_level": 100,
                    },
                ),
                patch.object(app, "run_advancing_dungeon_clear", side_effect=clear),
                patch.object(app, "run_advancing_dungeon_sweep", side_effect=sweep),
                patch("builtins.print"),
            ):
                code = app.cmd_dungeon_advance(key=6, count=4)
            saved = json.loads((tmp / "last_dungeon.json").read_text(encoding="utf-8"))
            return code, saved

        code, saved = self.run_in_temp(exercise)
        self.assertEqual(code, 0)
        self.assertEqual(clear_levels, [99, 100])
        self.assertEqual(sweep_calls, [(100, True), (100, True)])
        self.assertEqual(saved["completed"], 4)
        self.assertEqual(saved["stop_reason"], "count_reached")
        self.assertEqual(session.heartbeats, 3)

    def test_at_max_starts_from_max_instead_of_101(self) -> None:
        session = FakeSession()
        clear_levels: list[int] = []
        sweep_calls: list[tuple[int, bool]] = []

        def clear(_session, *, key: int, level: int, log):
            clear_levels.append(level)
            return {"ok": True, "key": key, "level": level, "cleared_level": 100}

        def sweep(_session, *, key: int, level: int, reset_before: bool, log):
            sweep_calls.append((level, reset_before))
            return {"ok": True, "key": key, "level": level, "mode": "max_level_sweep"}

        def exercise(_tmp: Path):
            with (
                patch.object(app, "_load_session", return_value=session),
                patch.object(
                    app,
                    "advancing_dungeon_progress",
                    return_value={
                        "key": 6,
                        "cleared_level": 100,
                        "next_level": 100,
                        "max_level": 100,
                        "progress": {"_challengeLevel": 100},
                    },
                ),
                patch.object(app, "run_advancing_dungeon_clear", side_effect=clear),
                patch.object(app, "run_advancing_dungeon_sweep", side_effect=sweep),
                patch("builtins.print"),
            ):
                return app.cmd_dungeon_advance(key=6, count=2)

        code = self.run_in_temp(exercise)
        self.assertEqual(code, 0)
        self.assertEqual(clear_levels, [])
        self.assertEqual(sweep_calls, [(100, True), (100, True)])

    def test_without_count_keeps_repeating_until_cancelled(self) -> None:
        session = FakeSession()
        sweep_calls: list[tuple[int, bool]] = []

        def sweep(_session, *, key: int, level: int, reset_before: bool, log):
            sweep_calls.append((level, reset_before))
            if len(sweep_calls) >= 3:
                raise KeyboardInterrupt
            return {"ok": True, "key": key, "level": level, "mode": "max_level_sweep"}

        def exercise(_tmp: Path):
            with (
                patch.object(app, "_load_session", return_value=session),
                patch.object(
                    app,
                    "advancing_dungeon_progress",
                    return_value={
                        "key": 6,
                        "cleared_level": 100,
                        "next_level": 100,
                        "max_level": 100,
                    },
                ),
                patch.object(app, "run_advancing_dungeon_sweep", side_effect=sweep),
                patch("builtins.print"),
            ):
                return app.cmd_dungeon_advance(key=6)

        code = self.run_in_temp(exercise)
        self.assertEqual(code, 130)
        self.assertEqual(sweep_calls, [(100, False), (100, True), (100, True)])

    def test_repeat_resets_trial_state_before_next_sweep(self) -> None:
        client = FakeApiClient(
            {
                "/api/dungeon/trial-reset": [{"_code": 0, "_dungeon": {"_key": 6}}],
                "/api/dungeon/list": [
                    {
                        "_code": 0,
                        "_dungeonList": {
                            "_list": [
                                {
                                    "_key": 6,
                                    "_level": 100,
                                    "_challengeLevel": 0,
                                    "_isGetReward": True,
                                    "_adCount": 0,
                                }
                            ]
                        },
                    }
                ],
                "/api/dungeon/sweep": [
                    {
                        "_code": 0,
                        "_dungeon": {
                            "_key": 6,
                            "_level": 100,
                            "_challengeLevel": 100,
                            "_isGetReward": True,
                            "_adCount": 0,
                        },
                        "_rewardAllList": {
                            "_rewardList": {
                                "_list": [{"_type": 1, "_value": 250, "_count": 6450}]
                            }
                        },
                    }
                ],
            }
        )
        session = SimpleNamespace(client=client, init_data={})

        result = run_advancing_dungeon_sweep(
            session,
            key=6,
            level=100,
            reset_before=True,
            log=lambda _line: None,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reset"]["code"], 0)
        self.assertEqual(
            client.calls,
            [
                ("/api/dungeon/trial-reset", {"_key": 6}),
                ("/api/dungeon/list", {}),
                ("/api/dungeon/sweep", {"_key": 6, "_sector": 1, "_level": 100}),
            ],
        )

    def test_repeat_stops_before_sweep_when_reset_item_is_missing(self) -> None:
        client = FakeApiClient(
            {
                "/api/dungeon/trial-reset": [
                    {"_code": -31002, "_message": "insufficient goods"}
                ]
            }
        )
        session = SimpleNamespace(client=client, init_data={})

        result = run_advancing_dungeon_sweep(
            session,
            key=6,
            level=100,
            reset_before=True,
            log=lambda _line: None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dungeon_trial_reset_failed")
        self.assertEqual(client.calls, [("/api/dungeon/trial-reset", {"_key": 6})])

    def test_dungeon_7_uses_progress_key_7_and_battle_stage_6(self) -> None:
        client = FakeApiClient(
            {
                "/api/dungeon/list": [
                    {
                        "_code": 0,
                        "_dungeonList": {
                            "_list": [
                                {
                                    "_key": 6,
                                    "_level": 101,
                                    "_challengeLevel": 101,
                                },
                                {
                                    "_key": 7,
                                    "_level": 2,
                                    "_challengeLevel": 2,
                                },
                            ]
                        },
                    }
                ],
                "/api/dungeon/start": [
                    {
                        "_code": 0,
                        "_spawnMobList": {
                            "_list": [
                                {
                                    "_wave": 1,
                                    "_mobList": {"_list": [{"_uid": "mob-3"}]},
                                }
                            ]
                        },
                    }
                ],
                "/api/battle/kill-mob": [{"_code": 0}],
                "/api/dungeon/end": [
                    {
                        "_code": 0,
                        "_dungeon": {
                            "_key": 7,
                            "_level": 3,
                            "_challengeLevel": 3,
                        },
                        "_rewardAllList": {"_rewardList": {"_list": []}},
                    }
                ],
            }
        )
        session = SimpleNamespace(client=client, init_data={})

        progress = advancing_dungeon_progress(session, key=7)
        clear = run_advancing_dungeon_clear(
            session,
            key=7,
            level=3,
            log=lambda _line: None,
        )

        self.assertEqual(progress["progress_key"], 7)
        self.assertEqual(progress["cleared_level"], 2)
        self.assertEqual(progress["challenge_level"], 2)
        self.assertTrue(clear["ok"])
        self.assertEqual(clear["cleared_level"], 3)
        self.assertEqual(
            client.calls[1],
            (
                "/api/dungeon/start",
                {
                    "_region": 10000,
                    "_stage": 6,
                    "_sector": 1,
                    "_repeat": 3,
                    "_wave": 0,
                    "_state": 0,
                    "_attr": 3,
                },
            ),
        )

    def test_fixed_level_defaults_to_one_run(self) -> None:
        session = FakeSession()
        clear_levels: list[int] = []

        def clear(_session, *, key: int, level: int, log):
            clear_levels.append(level)
            return {
                "ok": True,
                "key": key,
                "level": level,
                "cleared_level": level,
                "progress_after": {"_challengeLevel": level},
            }

        def exercise(tmp: Path):
            with (
                patch.object(app, "_load_session", return_value=session),
                patch.object(
                    app,
                    "advancing_dungeon_progress",
                    return_value={
                        "key": 7,
                        "progress_key": 7,
                        "cleared_level": 2,
                        "challenge_level": 2,
                        "next_level": 3,
                        "max_level": 100,
                    },
                ),
                patch.object(app, "run_advancing_dungeon_clear", side_effect=clear),
                patch.object(app, "run_advancing_dungeon_sweep") as sweep,
                patch("builtins.print"),
            ):
                code = app.cmd_dungeon_advance(key=7, level=3)
            saved = json.loads((tmp / "last_dungeon.json").read_text(encoding="utf-8"))
            return code, saved, sweep

        code, saved, sweep = self.run_in_temp(exercise)
        self.assertEqual(code, 0)
        self.assertEqual(clear_levels, [3])
        sweep.assert_not_called()
        self.assertEqual(saved["count"], 1)
        self.assertEqual(saved["completed"], 1)


if __name__ == "__main__":
    unittest.main()
