from __future__ import annotations

import unittest
from types import SimpleNamespace

from client.guild_care import run_guild_auto_care


class FakeClient:
    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = {path: list(items) for path, items in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        queue = self.responses.get(path)
        if not queue:
            raise AssertionError(f"unexpected request: {path} {body}")
        return queue.pop(0)


def reward_response() -> dict:
    return {
        "_code": 0,
        "_rewardAllList": {
            "_rewardList": {"_list": [{"_type": 359, "_value": 0, "_count": 100}]}
        },
    }


class GuildCareTests(unittest.TestCase):
    def test_claims_and_sweeps_exact_live_remaining_counts(self) -> None:
        client = FakeClient(
            {
                "/api/camp/info": [{"_code": 0, "_camp": {"_isAttendance": False}}],
                "/api/camp/attendance": [reward_response()],
                "/api/boss-damage/info": [
                    {"_code": 0, "_bossDamage": {"_type": 4}, "_isGetRewardBox": False},
                    {"_code": 0, "_bossDamage": {"_type": 5}, "_isGetRewardBox": False},
                ],
                "/api/boss-damage/reward-list": [
                    {"_code": 0, "_isGetRewardBox": False},
                    {"_code": 0, "_isGetRewardBox": False},
                ],
                "/api/boss-damage/reward": [reward_response(), reward_response()],
                "/api/goods/list": [
                    {
                        "_code": 0,
                        "_goodsList": {
                            "_list": [
                                {"_type": 107, "_value": "2"},
                                {"_type": 108, "_value": "1"},
                            ]
                        },
                    }
                ],
                "/api/dungeon/camp-sweep": [
                    reward_response(),
                    reward_response(),
                    reward_response(),
                ],
            }
        )
        session = SimpleNamespace(client=client)

        result = run_guild_auto_care(session, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertTrue(result["attendance"]["claimed"])
        self.assertTrue(result["rewards"]["training"]["claimed"])
        self.assertTrue(result["rewards"]["dojo"]["claimed"])
        self.assertEqual(result["sweeps"]["training"]["completed"], 2)
        self.assertEqual(result["sweeps"]["dojo"]["completed"], 1)
        self.assertEqual(result["total_completed"], 3)

        sweep_bodies = [body for path, body in client.calls if path == "/api/dungeon/camp-sweep"]
        self.assertEqual(sweep_bodies, [{"_key": 15}, {"_key": 15}, {"_key": 16}])
        reward_bodies = [body for path, body in client.calls if path == "/api/boss-damage/reward"]
        self.assertEqual(reward_bodies, [{"_type": 4}, {"_type": 5}])

    def test_already_done_and_zero_remaining_do_not_write(self) -> None:
        client = FakeClient(
            {
                "/api/camp/info": [{"_code": 0, "_isAttendance": True}],
                "/api/boss-damage/info": [
                    {"_code": 0, "_isGetRewardBox": True},
                    {"_code": 0, "_isGetRewardBox": True},
                ],
                "/api/goods/list": [
                    {
                        "_code": 0,
                        "_goodsList": {
                            "_list": [
                                {"_type": 107, "_value": "0"},
                                {"_type": 108, "_value": "0"},
                            ]
                        },
                    }
                ],
            }
        )
        session = SimpleNamespace(client=client)

        result = run_guild_auto_care(session, log=lambda _line: None)

        self.assertTrue(result["ok"])
        called_paths = [path for path, _body in client.calls]
        self.assertNotIn("/api/camp/attendance", called_paths)
        self.assertNotIn("/api/boss-damage/reward-list", called_paths)
        self.assertNotIn("/api/boss-damage/reward", called_paths)
        self.assertNotIn("/api/dungeon/camp-sweep", called_paths)


if __name__ == "__main__":
    unittest.main()
