from __future__ import annotations

import unittest
from types import SimpleNamespace

from client.partner_care import ensure_active_lowest_level_partner


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return {"_code": 0}


class PartnerCareTests(unittest.TestCase):
    def test_switches_to_lowest_level_without_level_90_limit(self) -> None:
        client = FakeClient()
        session = SimpleNamespace(client=client)
        partners = [
            {"key": 10000010, "level": 99},
            {"key": 10000020, "level": 95},
            {"key": 10000030, "level": 97},
        ]

        result = ensure_active_lowest_level_partner(
            session,
            partners,
            current_key=10000010,
            log=lambda _line: None,
        )

        self.assertTrue(result["switched"])
        self.assertEqual(result["from_level"], 99)
        self.assertEqual(result["to_key"], 10000020)
        self.assertEqual(result["to_level"], 95)
        self.assertEqual(
            client.calls,
            [("/api/partner/change-character", {"_key": 10000020})],
        )

    def test_does_not_switch_when_current_partner_is_already_lowest(self) -> None:
        client = FakeClient()
        session = SimpleNamespace(client=client)
        partners = [
            {"key": 10000010, "level": 96},
            {"key": 10000020, "level": 95},
            {"key": 10000030, "level": 99},
        ]

        result = ensure_active_lowest_level_partner(
            session,
            partners,
            current_key=10000020,
            log=lambda _line: None,
        )

        self.assertFalse(result["switched"])
        self.assertEqual(result["from_level"], 95)
        self.assertEqual(result["reason"], "already lowest level=95")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
