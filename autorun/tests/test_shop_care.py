"""Daily shop auto-buy: delisted targets must not look like a failed run."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

try:  # `python3 -m unittest tests.test_shop_care`
    from ._stubs import install_stubs
except ImportError:  # `python3 tests/test_shop_care.py`
    from _stubs import install_stubs

install_stubs()

from client.shop_care import DAILY_BUY_TARGETS, run_shop_daily_buy  # noqa: E402


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


def shop_list(*, only_key: int | None = None) -> dict:
    """Every target consumed, except `only_key` which still has one left."""
    items = []
    for key, _limit, _currency, _cost in DAILY_BUY_TARGETS:
        items.append({"_key": key, "_buyCount": 1 if key == only_key else 0})
    return {"_code": 0, "_shopList": {"_list": items}}


class ShopDailyBuyTests(unittest.TestCase):
    def test_delisted_target_is_skipped_not_failed(self) -> None:
        # Live 1.5.0: 202213 still advertises _buyCount 1 but the server
        # answers -32012 판매 중인 상품이 아닙니다.
        client = FakeClient(
            {
                "/api/shop/list": [shop_list(only_key=202213)],
                "/api/shop/buy": [{"_code": -32012, "_message": "판매 중인 상품이 아닙니다."}],
            }
        )
        session = SimpleNamespace(client=client)

        result = run_shop_daily_buy(session, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], [])
        self.assertEqual([e["key"] for e in result["bought"]], [])
        skipped = {e["key"]: e.get("reason") for e in result["skipped"]}
        self.assertEqual(skipped.get(202213), "not_purchasable")

    def test_real_error_still_fails(self) -> None:
        client = FakeClient(
            {
                "/api/shop/list": [shop_list(only_key=202213)],
                "/api/shop/buy": [{"_code": -99999, "_message": "boom"}],
            }
        )
        session = SimpleNamespace(client=client)

        result = run_shop_daily_buy(session, log=lambda _line: None)

        self.assertFalse(result["ok"])
        self.assertEqual([e["key"] for e in result["failed"]], [202213])

    def test_successful_buy_reports_after_count(self) -> None:
        client = FakeClient(
            {
                "/api/shop/list": [shop_list(only_key=202213)],
                "/api/shop/buy": [{"_code": 0, "_shop": {"_buyCount": 0}}],
            }
        )
        session = SimpleNamespace(client=client)

        result = run_shop_daily_buy(session, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["bought"][0]["key"], 202213)
        self.assertEqual(result["bought"][0]["after_buyCount"], 0)


if __name__ == "__main__":
    unittest.main()
