from __future__ import annotations

import unittest

from client.account_store import apply_account_to_config
from client.apis.item_spawner import item_spawn_and_sell
from client.config import ClientConfig


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return {"_code": 0}


class Version130Tests(unittest.TestCase):
    def test_stale_account_capture_does_not_downgrade_client_profile(self) -> None:
        config = ClientConfig(load_saved_account=False)

        apply_account_to_config(
            config,
            {
                "version": "1.0.2",
                "unity_version": "2021.3.0f1",
                "account": {"device_model": "iPhone15,3"},
            },
        )

        self.assertEqual(config.version, "1.3.0")
        self.assertEqual(config.unity_version, "6000.3.11f1")
        self.assertEqual(config.account.device_model, "iPhone15,3")

    def test_spawn_and_sell_sends_new_is_super_field(self) -> None:
        client = FakeClient()

        item_spawn_and_sell(
            client,
            count=9,
            filter_grade=10,
            filter_match_count=2,
            filter_stat_type_list=[10, 20, 13],
        )

        self.assertEqual(
            client.calls,
            [
                (
                    "/api/item/spawn-and-sell",
                    {
                        "_count": 9,
                        "_filterGrade": 10,
                        "_filterMatchCount": 2,
                        "_filterStatTypeList": [10, 20, 13],
                        "_isSuper": False,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
