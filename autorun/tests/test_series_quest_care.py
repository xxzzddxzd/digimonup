from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from client.apis import battle as battle_api
from client import qmd_auto
from client.series_quest_care import (
    _run_item_spawn,
    _run_mob_kills,
    load_series_definitions,
    quest_target,
    run_series_quest_care,
)


class FakeClient:
    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.responses = {path: list(items) for path, items in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        queue = self.responses.get(path)
        if not queue:
            raise AssertionError(f"unexpected request: {path} {body}")
        return queue.pop(0)


def quest_row(key: int, value: int, *, level: int = 1) -> dict:
    return {"_key": key, "_level": level, "_value": value, "_isGetReward": False}


def quest_list(*rows: dict) -> dict:
    return {"_code": 0, "_questList": {"_list": list(rows)}}


class FakeBattleSession:
    def __init__(self) -> None:
        self.client = SimpleNamespace()
        self.battle_info = {"_region": 1, "_stage": 23, "_sector": 2, "_repeat": 1}
        self.kill_calls: list[dict] = []
        self.end_calls: list[dict] = []

    def init_game_data(self) -> dict:
        return {}

    def ensure_heartbeat(self) -> None:
        return None

    def battle_start(self, **_kwargs) -> dict:
        return {
            "_code": 0,
            "_spawnMobList": {
                "_list": [
                    {
                        "_wave": 1,
                        "_mobList": {"_list": [{"_uid": "a"}, {"_uid": "b"}, {"_uid": "c"}]},
                    },
                    {"_wave": 2, "_mobList": {"_list": [{"_uid": "boss"}]}},
                ]
            },
        }

    def battle_kill_mob(self, **kwargs) -> dict:
        self.kill_calls.append(kwargs)
        return {"_code": 0}

    def battle_end(self, **kwargs) -> dict:
        self.end_calls.append(kwargs)
        return {"_code": 0}


class SeriesQuestCareTests(unittest.TestCase):
    def test_item_spawn_target_50_runs_one_250_super_batch(self) -> None:
        session = SimpleNamespace()
        log = lambda _line: None
        with (
            patch(
                "client.series_quest_care.fetch_item_ticket_stock",
                return_value=({}, 750),
            ),
            patch(
                "client.series_quest_care.run_spawn_batches",
                return_value={"ok": True, "items_ok": 250, "batches_ok": 1},
            ) as spawn_mock,
        ):
            result = _run_item_spawn(session, remaining=50, log=log)

        self.assertTrue(result["ok"])
        self.assertEqual(result["requested"], 50)
        self.assertEqual(result["planned"], 250)
        self.assertEqual(result["opened"], 250)
        spawn_mock.assert_called_once_with(
            session,
            batches=1,
            item_ticket_start=750,
            auto_equip=True,
            auto_sell=True,
            log=log,
        )

    def test_item_spawn_does_not_send_partial_super_batch(self) -> None:
        session = SimpleNamespace()
        with (
            patch(
                "client.series_quest_care.fetch_item_ticket_stock",
                return_value=({}, 249),
            ),
            patch("client.series_quest_care.run_spawn_batches") as spawn_mock,
        ):
            result = _run_item_spawn(session, remaining=50, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertFalse(result["attempted"])
        self.assertEqual(result["planned"], 0)
        self.assertEqual(result["stop_reason"], "item_ticket_exhausted")
        spawn_mock.assert_not_called()

    def test_no_current_series_task_is_a_clean_skip(self) -> None:
        client = FakeClient({"/api/quest/list": [quest_list()]})
        result = run_series_quest_care(
            SimpleNamespace(client=client), log=lambda _line: None
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped_reason"], "no_series_task")
        self.assertEqual(result["claimed_count"], 0)
        self.assertEqual(client.calls, [("/api/quest/list", {})])

    def test_claims_completed_task_then_skips_zero_ticket_dungeon(self) -> None:
        client = FakeClient(
            {
                "/api/quest/list": [
                    quest_list(quest_row(9006, 2)),
                    quest_list(quest_row(9007, 0)),
                    quest_list(quest_row(9007, 0)),
                ],
                "/api/quest/complete": [{"_code": 0}],
                "/api/goods/list": [
                    {"_code": 0, "_goodsList": {"_list": [{"_type": 102, "_value": 0}]}}
                ],
            }
        )
        result = run_series_quest_care(
            SimpleNamespace(client=client, init_data={}), log=lambda _line: None
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["claimed"], [9006])
        self.assertEqual(result["stopped_reason"], "dungeon_no_attempts")
        self.assertTrue(result["dungeon_no_attempts"])
        self.assertNotIn("/api/dungeon/start", [path for path, _ in client.calls])
        self.assertIn(("/api/quest/complete", {"_keys": [9006]}), client.calls)

    def test_skill_spawn_uses_ticket_shop_and_claims(self) -> None:
        client = FakeClient(
            {
                "/api/quest/list": [
                    quest_list(quest_row(9010, 0)),
                    quest_list(quest_row(9010, 35)),
                    quest_list(quest_row(9010, 35)),
                    quest_list(),
                ],
                "/api/goods/list": [
                    {"_code": 0, "_goodsList": {"_list": [{"_type": 51, "_value": 30}]}}
                ],
                "/api/shop/spawn": [{"_code": 0}],
                "/api/quest/complete": [{"_code": 0}],
            }
        )
        result = run_series_quest_care(
            SimpleNamespace(client=client), log=lambda _line: None
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["claimed"], [9010])
        self.assertEqual(result["stopped_reason"], "no_series_task")
        self.assertIn(("/api/shop/spawn", {"_key": 12}), client.calls)
        self.assertIn(("/api/quest/complete", {"_keys": [9010]}), client.calls)

    def test_sector_target_increases_with_cycle_level(self) -> None:
        definition = load_series_definitions()[9014]
        self.assertEqual(quest_target(definition, 66), 540)

    def test_kill_task_skips_boss_and_does_not_advance_story(self) -> None:
        session = FakeBattleSession()
        result = _run_mob_kills(session, remaining=2, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["killed"], 2)
        self.assertEqual(
            session.kill_calls,
            [{"wave": 1, "mob_uid_list": ["a", "b"], "reason": battle_api.REASON_NONE}],
        )
        self.assertEqual(session.end_calls[0]["reason"], battle_api.REASON_ALL_DEAD)
        self.assertEqual(session.end_calls[0]["state"], battle_api.STATE_FAILED_BOSS)

    def test_allow_list_contains_all_known_recurring_task_kinds(self) -> None:
        definitions = load_series_definitions()
        kinds = {(row.get("Type"), row.get("SubType1")) for row in definitions.values()}
        self.assertEqual(len(definitions), 15)
        self.assertEqual(
            kinds,
            {
                ("Spawn", "Item"),
                ("Spawn", "Skill"),
                ("Spawn", "Member"),
                ("KillMobCount", None),
                ("DungeonClear", "Dungeon_Lamp"),
                ("DungeonClear", "Dungeon_Lava"),
                ("SectorClear", None),
            },
        )

    def test_auto_continues_after_dungeon_no_attempts_and_records_phase(self) -> None:
        session = SimpleNamespace(client=SimpleNamespace(log_enabled=True))
        series_result = {
            "ok": True,
            "start_task": {"key": 9006},
            "end_task": {"key": 9006},
            "claimed_count": 0,
            "action_count": 1,
            "stopped_reason": "dungeon_no_attempts",
            "dungeon_no_attempts": True,
        }
        cooling = SimpleNamespace(
            ready=False,
            total_count=1,
            left_sec=10.0,
            next_str="soon",
        )
        with (
            TemporaryDirectory() as tmp,
            patch.object(qmd_auto, "_login", return_value=1.0),
            patch.object(qmd_auto, "run_farm_maintain", return_value={"ok": True}),
            patch.object(qmd_auto, "run_shop_daily_buy", return_value={"ok": True}),
            patch.object(qmd_auto, "run_guild_auto_care", return_value={"ok": True}),
            patch.object(
                qmd_auto,
                "run_dungeon_auto_care",
                return_value={"ok": True, "ad": {}},
            ),
            patch.object(
                qmd_auto, "run_series_quest_care", return_value=series_result
            ) as series_mock,
            patch.object(qmd_auto, "run_lab_care", return_value={"ok": True}) as lab_mock,
            patch.object(qmd_auto, "run_mine_care", return_value={"ok": True}),
            patch.object(qmd_auto, "run_dbox_care", return_value={"ok": True}),
            patch.object(qmd_auto, "run_item_spawner_care", return_value={"ok": True}),
            patch.object(qmd_auto, "run_pvp_care", return_value={"ok": True}),
            patch.object(qmd_auto, "_care_status", return_value=(cooling, {})),
            patch.object(
                qmd_auto,
                "_run_afk",
                return_value={
                    "reward_list": {"code": 0},
                    "reward": {"code": 0},
                    "ad_view": {"code": 0},
                },
            ),
        ):
            log_path = Path(tmp) / "auto.log"
            code = qmd_auto.run_auto_once(
                lambda: session,
                log=lambda _line: None,
                http_log=False,
                result_log_path=log_path,
            )
            written = log_path.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        series_mock.assert_called_once_with(session, log=unittest.mock.ANY)
        lab_mock.assert_called_once()
        self.assertIn("result=series", written)
        self.assertIn("stop=dungeon_no_attempts", written)
        self.assertIn("result=done", written)


if __name__ == "__main__":
    unittest.main()
