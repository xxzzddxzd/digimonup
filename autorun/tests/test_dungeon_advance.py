from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:  # `python3 -m unittest tests.test_dungeon_advance`
    from ._stubs import install_stubs
except ImportError:  # `python3 tests/test_dungeon_advance.py`
    from _stubs import install_stubs

install_stubs()

import main as app  # noqa: E402
from client.dungeon_care import (  # noqa: E402
    ADVANCING_DUNGEON_CONFIG,
    SAFETY_MAX_CLEARS_PER_KEY,
    advancing_dungeon_progress,
    resolve_tower_dungeon_key,
    rotating_trial_progress,
    run_advancing_dungeon_clear,
    run_advancing_dungeon_sweep,
    run_daily_trial_sweep,
    run_dungeon_auto_care,
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

    def test_tower_selects_todays_trial_from_play_list(self) -> None:
        client = FakeApiClient(
            {
                "/api/dungeon/list": [
                    {
                        "_code": 0,
                        "_playList": {"_list": [7, 12, 2, 3, 4, 1, 5]},
                        "_dungeonList": {
                            "_list": [
                                {"_key": 6, "_level": 101, "_challengeLevel": 101},
                                {"_key": 7, "_level": 100, "_challengeLevel": 100},
                            ]
                        },
                    }
                ]
            }
        )
        session = SimpleNamespace(client=client, init_data={})

        progress = rotating_trial_progress(session)

        self.assertEqual(progress["key"], 7)
        self.assertEqual(progress["progress_key"], 7)
        self.assertEqual(progress["cleared_level"], 100)
        self.assertEqual(progress["challenge_level"], 100)
        self.assertEqual(progress["play_list"], [7, 12, 2, 3, 4, 1, 5])

    def test_tower_selects_job3_key8_from_play_list(self) -> None:
        client = FakeApiClient(
            {
                "/api/dungeon/list": [
                    {
                        "_code": 0,
                        "_playList": {"_list": [8, 12, 2, 3, 4, 1, 5]},
                        "_dungeonList": {
                            "_list": [
                                {"_key": 6, "_level": 10, "_challengeLevel": 10},
                                {"_key": 7, "_level": 20, "_challengeLevel": 20},
                                {"_key": 8, "_level": 33, "_challengeLevel": 12},
                            ]
                        },
                    }
                ]
            }
        )
        session = SimpleNamespace(client=client, init_data={})

        progress = rotating_trial_progress(session)

        self.assertEqual(progress["key"], 8)
        self.assertEqual(progress["progress_key"], 8)
        self.assertEqual(progress["cleared_level"], 33)
        self.assertEqual(progress["challenge_level"], 12)
        self.assertEqual(progress["next_level"], 34)
        self.assertEqual(progress["play_list"], [8, 12, 2, 3, 4, 1, 5])

    def test_tower_command_uses_resolved_key_and_level(self) -> None:
        session = FakeSession()
        clear_calls: list[tuple[int, int]] = []

        def clear(_session, *, key: int, level: int, log):
            clear_calls.append((key, level))
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
                    "rotating_trial_progress",
                    return_value={
                        "key": 6,
                        "progress_key": 6,
                        "cleared_level": 4,
                        "challenge_level": 4,
                        "next_level": 5,
                        "max_level": 100,
                        "play_list": [6, 12, 2, 3, 4, 1, 5],
                    },
                ),
                patch.object(app, "run_advancing_dungeon_clear", side_effect=clear),
                patch("builtins.print"),
            ):
                code = app.cmd_dungeon_advance(key="tower", count=1)
            saved = json.loads((tmp / "last_dungeon.json").read_text(encoding="utf-8"))
            return code, saved

        code, saved = self.run_in_temp(exercise)
        self.assertEqual(code, 0)
        self.assertEqual(clear_calls, [(6, 5)])
        self.assertEqual(saved["requested_key"], "tower")
        self.assertEqual(saved["key"], 6)
        self.assertEqual(saved["progress_before"]["cleared_level"], 4)

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


    def test_tower_prefers_job_trial_over_firewall(self) -> None:
        # Live playList often includes Firewall (11/12) alongside today's Job trial.
        self.assertEqual(resolve_tower_dungeon_key([11, 6, 3, 2, 4, 1, 5]), 6)
        self.assertEqual(resolve_tower_dungeon_key([12, 8, 2, 3, 4, 1, 5]), 8)

    def test_tower_falls_back_to_non_job_key_when_no_trial(self) -> None:
        self.assertEqual(resolve_tower_dungeon_key([11, 3, 2, 4, 1, 5]), 11)
        self.assertEqual(resolve_tower_dungeon_key([12, 2, 1]), 12)

    def test_advancing_config_covers_keys_6_to_12(self) -> None:
        self.assertEqual(sorted(ADVANCING_DUNGEON_CONFIG), list(range(6, 13)))
        self.assertEqual(ADVANCING_DUNGEON_CONFIG[8]["stage"], 7)
        self.assertEqual(ADVANCING_DUNGEON_CONFIG[9]["region"], 100000)
        self.assertEqual(ADVANCING_DUNGEON_CONFIG[11]["stage"], 9)
        self.assertEqual(ADVANCING_DUNGEON_CONFIG[12]["stage"], 10)


DUNGEON_META = {
    1: {"name": "GT_Dungeon_Lamp", "costType": "DungeonTicket_Lamp", "cost": 1, "stageKey": 10000},
    2: {"name": "GT_Dungeon_Lava", "costType": "DungeonTicket_Lava", "cost": 1, "stageKey": 10020},
    3: {"name": "GT_Dungeon_Flame", "costType": "DungeonTicket_Flame", "cost": 1, "stageKey": 10010},
}
DUNGEON_STAGES = {1: 10000, 2: 10020, 3: 10010}
DUNGEON_ROWS = [
    {"_key": 1, "_level": 429, "_challengeLevel": 0, "_isGetReward": True, "_adCount": 0},
    {"_key": 2, "_level": 428, "_challengeLevel": 0, "_isGetReward": True, "_adCount": 0},
    {"_key": 3, "_level": 366, "_challengeLevel": 0, "_isGetReward": True, "_adCount": 0},
]


def ad_care_result() -> dict:
    return {"ok": True, "total_ok": 0, "total_fail": 0, "skipped": [], "claimed": []}


class DungeonAutoBurnTests(unittest.TestCase):
    """`auto` burns ordinary dungeon tickets by sweeping, never by battle.

    The battle wire answers _code 0 for these dungeons in 1.5.0 but spends
    nothing, which used to fill the loop up to SAFETY_MAX_CLEARS_PER_KEY while
    every ticket stayed put.
    """

    def _run(self, *, stock: dict[str, int], sweep_side_effect):
        session = FakeSession()
        with (
            patch("client.dungeon_care.run_dungeon_ad_care", return_value=ad_care_result()),
            patch(
                "client.dungeon_care.run_daily_trial_sweep",
                return_value={"ok": True, "key": 7, "rewards": 0, "attempted": True},
            ),
            patch("client.dungeon_care.fetch_dungeon_rows", return_value=(DUNGEON_ROWS, {})),
            patch("client.dungeon_care.load_key_meta", return_value=DUNGEON_META),
            patch("client.dungeon_care.load_key_stage_map", return_value=DUNGEON_STAGES),
            patch("client.dungeon_care.fetch_goods_map", return_value={}),
            patch(
                "client.dungeon_care.ticket_stock_for_key",
                side_effect=lambda key, _goods=None, _meta=None: stock[str(key)],
            ),
            patch("client.dungeon_care.run_dungeon_sweep", side_effect=sweep_side_effect) as sweep_mock,
            patch("client.dungeon_care.run_dungeon_clear") as battle_mock,
        ):
            result = run_dungeon_auto_care(session, log=lambda _line: None)
        return result, sweep_mock, battle_mock

    def test_sweeps_ordinary_keys_until_stock_runs_out(self) -> None:
        stock = {"1": 17, "2": 17, "3": 2}

        def sweep(_session, *, key, log):
            stock[str(key)] -= 1
            return {"ok": True, "code": 0, "level": 366, "rewards": [1, 2, 3]}

        result, sweep_mock, battle_mock = self._run(stock=stock, sweep_side_effect=sweep)

        self.assertTrue(result["ok"])
        self.assertEqual([call.kwargs["key"] for call in sweep_mock.call_args_list], [3, 3])
        battle_mock.assert_not_called()
        self.assertEqual(result["total_sweeps"], 2)
        self.assertEqual(result["total_clears"], 2)
        # Tickets actually leave the account, so the loop ends on stock, not the cap.
        self.assertLess(result["total_sweeps"], SAFETY_MAX_CLEARS_PER_KEY)
        self.assertEqual(stock["3"], 0)

    def test_lamp_and_lava_tickets_are_reserved_for_the_series_quest(self) -> None:
        stock = {"1": 17, "2": 17, "3": 0}

        def sweep(_session, *, key, log):  # pragma: no cover - must not run
            raise AssertionError(f"key {key} must not be swept by auto")

        result, sweep_mock, _battle = self._run(stock=stock, sweep_side_effect=sweep)

        self.assertTrue(result["ok"])
        sweep_mock.assert_not_called()
        reserved = {
            entry["key"]: entry["reason"]
            for entry in result["skipped_battle"]
            if entry["reason"] == "reserved_for_series_quest"
        }
        self.assertEqual(reserved, {1: "reserved_for_series_quest", 2: "reserved_for_series_quest"})
        # Their stock is untouched for run_series_quest_care to spend.
        self.assertEqual((stock["1"], stock["2"]), (17, 17))

    def test_key_with_nothing_cleared_is_skipped_cleanly(self) -> None:
        stock = {"1": 0, "2": 0, "3": 5}

        def sweep(_session, *, key, log):
            return {"ok": False, "error": "no_cleared_level"}

        result, sweep_mock, _battle = self._run(stock=stock, sweep_side_effect=sweep)

        self.assertTrue(result["ok"])
        self.assertEqual(sweep_mock.call_count, 1)
        self.assertEqual(result["total_clear_fail"], 0)
        reasons = [entry["reason"] for entry in result["skipped_battle"]]
        self.assertIn("no_cleared_level", reasons)

    def test_rejected_sweep_stops_that_key_and_marks_failure(self) -> None:
        stock = {"1": 0, "2": 0, "3": 5}

        def sweep(_session, *, key, log):
            return {"ok": False, "code": -29004, "error": None}

        result, sweep_mock, _battle = self._run(stock=stock, sweep_side_effect=sweep)

        self.assertFalse(result["ok"])
        self.assertEqual(sweep_mock.call_count, 1)
        self.assertEqual(result["total_clear_fail"], 1)

    def test_auto_care_surfaces_the_daily_trial_result(self) -> None:
        stock = {"1": 0, "2": 0, "3": 0}
        session = FakeSession()

        with (
            patch("client.dungeon_care.run_dungeon_ad_care", return_value=ad_care_result()),
            patch(
                "client.dungeon_care.run_daily_trial_sweep",
                return_value={
                    "ok": True,
                    "key": 7,
                    "level": 100,
                    "attempted": True,
                    "rewards": 200,
                    "code": 0,
                    "skipped_reason": None,
                },
            ),
            patch("client.dungeon_care.fetch_dungeon_rows", return_value=(DUNGEON_ROWS, {})),
            patch("client.dungeon_care.load_key_meta", return_value=DUNGEON_META),
            patch("client.dungeon_care.load_key_stage_map", return_value=DUNGEON_STAGES),
            patch("client.dungeon_care.fetch_goods_map", return_value={}),
            patch(
                "client.dungeon_care.ticket_stock_for_key",
                side_effect=lambda key, _goods=None, _meta=None: stock[str(key)],
            ),
        ):
            result = run_dungeon_auto_care(session, log=lambda _line: None)

        self.assertEqual(result["trial"]["key"], 7)
        self.assertEqual(result["trial"]["rewards"], 200)
        self.assertTrue(result["trial"]["attempted"])


def trial_progress(*, key: int = 7, cleared: int = 100, max_level: int | None = 100,
                   play_list: list[int] | None = None) -> dict:
    return {
        "key": key,
        "progress_key": key,
        "cleared_level": cleared,
        "challenge_level": 0,
        "next_level": cleared,
        "max_level": max_level,
        "at_max_level": max_level is not None and cleared >= max_level,
        "progress": {"_key": key, "_level": cleared, "_challengeLevel": 0},
        "play_list": play_list if play_list is not None else [key],
    }


class DailyTrialSweepTests(unittest.TestCase):
    """The free once-a-day sweep of the rotating job trial (keys 6/7/8)."""

    def _run(self, *, progress, sweep_result=None, progress_error=None):
        session = FakeSession()
        ctx = []
        if progress_error is not None:
            ctx.append(
                patch("client.dungeon_care.rotating_trial_progress", side_effect=progress_error)
            )
        else:
            ctx.append(patch("client.dungeon_care.rotating_trial_progress", return_value=progress))
        ctx.append(
            patch(
                "client.dungeon_care.run_dungeon_sweep",
                return_value=sweep_result or {"ok": True, "code": 0, "rewards": [1, 2, 3]},
            )
        )
        with ctx[0], ctx[1] as sweep_mock:
            result = run_daily_trial_sweep(session, log=lambda _line: None)
        return result, sweep_mock

    def test_sweeps_todays_maxed_job_trial(self) -> None:
        # Live 2026-10-11: key 7 cleared 100/100, playList [7,4,3,12,2,1,5].
        result, sweep_mock = self._run(
            progress=trial_progress(play_list=[7, 4, 3, 12, 2, 1, 5])
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["attempted"])
        self.assertEqual(result["key"], 7)
        self.assertEqual(result["rewards"], 3)
        self.assertEqual(result["play_list"], [7, 4, 3, 12, 2, 1, 5])
        sweep_mock.assert_called_once()
        kwargs = sweep_mock.call_args.kwargs
        self.assertEqual((kwargs["key"], kwargs["level"], kwargs["sector"]), (7, 100, 1))

    def test_second_sweep_of_the_day_is_a_clean_skip(self) -> None:
        # Server answers -29004 던전 소탕이 불가능한 상태입니다 once today's sweep is used.
        result, sweep_mock = self._run(
            progress=trial_progress(),
            sweep_result={"ok": False, "code": -29004, "message": "던전 소탕이 불가능한 상태입니다."},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_reason"], "already_swept_today")
        self.assertEqual(sweep_mock.call_count, 1)

    def test_not_maxed_defers_to_dungeon_tower(self) -> None:
        result, sweep_mock = self._run(progress=trial_progress(cleared=42, max_level=100))

        self.assertTrue(result["ok"])
        self.assertFalse(result["attempted"])
        self.assertEqual(result["skipped_reason"], "not_at_max_level")
        sweep_mock.assert_not_called()

    def test_non_job_tower_key_is_left_to_dungeon_tower(self) -> None:
        result, sweep_mock = self._run(
            progress=trial_progress(key=12, cleared=31, max_level=None, play_list=[12, 4])
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_reason"], "no_job_trial_today")
        sweep_mock.assert_not_called()

    def test_unresolvable_playlist_is_a_clean_skip(self) -> None:
        result, sweep_mock = self._run(
            progress=None, progress_error=RuntimeError("playList empty")
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_reason"], "tower_unresolved:RuntimeError")
        sweep_mock.assert_not_called()

    def test_unexpected_sweep_error_is_reported(self) -> None:
        result, _sweep = self._run(
            progress=trial_progress(),
            sweep_result={"ok": False, "code": -99999, "message": "boom"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], -99999)


if __name__ == "__main__":
    unittest.main()
