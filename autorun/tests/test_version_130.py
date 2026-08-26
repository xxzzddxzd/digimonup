from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from client import item_spawner_care
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

    def test_zb_rounds_50_up_to_one_serial_250_super_batch(self) -> None:
        client = FakeClient()
        session = SimpleNamespace(client=client)

        with (
            patch.object(item_spawner_care, "load_spawner_table", return_value={}),
            patch.object(item_spawner_care, "fetch_info", return_value=({}, {})),
            patch.object(
                item_spawner_care,
                "fetch_item_ticket_stock",
                return_value=({}, 0),
            ),
        ):
            result = item_spawner_care.run_spawn_batches(
                session,
                total=50,
                item_ticket_start=250,
                auto_equip=False,
                auto_sell=False,
                log=lambda _line: None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["is_super"])
        self.assertEqual(result["batch_count"], 250)
        self.assertEqual(result["target_items"], 250)
        self.assertEqual(result["items_ok"], 250)
        self.assertEqual(
            client.calls,
            [
                (
                    "/api/item/spawn-and-sell",
                    {
                        "_count": 250,
                        "_filterGrade": 10,
                        "_filterMatchCount": 2,
                        "_filterStatTypeList": [10, 20, 13],
                        "_isSuper": True,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
