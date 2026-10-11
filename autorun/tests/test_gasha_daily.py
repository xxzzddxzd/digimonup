"""Daily 3000-Crystal gasha pulled from `auto`.

Replays the live 1.5.0 request captured on 2026-10-11: the client asks
`/api/gasha/list`, then posts `{_key, _count: 10, _isDaily: true}` for every
banner whose `_isDailyCanGet` is still true.  Selection is dynamic, so these
tests pin the behaviour rather than a hardcoded banner list.
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:  # `python3 -m unittest tests.test_gasha_daily`
    from ._stubs import install_stubs
except ImportError:  # `python3 tests/test_gasha_daily.py`
    from _stubs import install_stubs

install_stubs()

from client.apis.gasha import GASHA_PARTNER, GASHA_SP  # noqa: E402
from client.gasha_care import (  # noqa: E402
    GASHA_DAILY_COUNT,
    SessionKicked,
    daily_gasha_keys,
    run_gasha_daily,
)


def _list_payload(*entries: dict) -> dict:
    return {"_code": 0, "_gashaList": {"_list": list(entries)}}


def _banner(key: int, *, daily: bool, count: int = 30) -> dict:
    return {
        "_key": key,
        "_count": count,
        "_startTime": 1790748000000,
        "_endTime": 1791957599000,
        "_isDailyCanGet": daily,
    }


class FakeClient:
    """Records requests; returns the queued spawn responses in order."""

    def __init__(self, infos: dict, spawn_results: list | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._infos = infos
        self._spawn_results = list(spawn_results or [])

    def post_encrypted(self, path: str, body: dict) -> dict:
        self.calls.append((path, dict(body)))
        if path == "/api/gasha/list":
            return self._infos
        if path == "/api/gasha/spawn":
            if self._spawn_results:
                return self._spawn_results.pop(0)
            return {"_code": 0}
        raise AssertionError(f"unexpected request: {path} {body}")

    @property
    def spawn_bodies(self) -> list[dict]:
        return [b for p, b in self.calls if p == "/api/gasha/spawn"]


class FakeSession:
    def __init__(self, client: FakeClient) -> None:
        self.client = client


class GashaDailySelectionTests(unittest.TestCase):
    def test_daily_gasha_keys_keeps_only_available_banners(self) -> None:
        payload = _list_payload(
            _banner(20007, daily=True),
            _banner(30006, daily=True),
            _banner(40004, daily=True, count=10),
            _banner(50000, daily=False),
        )

        keys = [entry["key"] for entry in daily_gasha_keys(payload)]

        self.assertEqual(keys, [20007, 30006, 40004])

    def test_daily_flag_accepts_numeric_truthy(self) -> None:
        # Some payloads carry `_isDailyCanGet` as 1 instead of true.
        payload = _list_payload({"_key": 20007, "_isDailyCanGet": 1})

        self.assertEqual([e["key"] for e in daily_gasha_keys(payload)], [20007])

    def test_entries_without_key_are_ignored(self) -> None:
        payload = _list_payload({"_isDailyCanGet": True}, _banner(20007, daily=True))

        self.assertEqual([e["key"] for e in daily_gasha_keys(payload)], [20007])


class GashaDailyRunTests(unittest.TestCase):
    def test_pulls_every_daily_banner_with_live_request_shape(self) -> None:
        client = FakeClient(
            _list_payload(
                _banner(20007, daily=True),
                _banner(30006, daily=True),
                _banner(40004, daily=True, count=10),
                _banner(50000, daily=False),
            )
        )
        session = FakeSession(client)

        result = run_gasha_daily(session, log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["targets"], [20007, 30006, 40004])
        self.assertEqual(len(result["pulled"]), 3)
        self.assertEqual(result["failed"], [])
        self.assertEqual(
            client.spawn_bodies,
            [
                {"_key": 20007, "_count": GASHA_DAILY_COUNT, "_isDaily": True},
                {"_key": 30006, "_count": GASHA_DAILY_COUNT, "_isDaily": True},
                {"_key": 40004, "_count": GASHA_DAILY_COUNT, "_isDaily": True},
            ],
        )

    def test_banner_keys_are_configurable_and_not_hardcoded(self) -> None:
        # A brand-new banner with no entry in GASHA_LABELS is still pulled.
        client = FakeClient(_list_payload(_banner(99999, daily=True)))

        result = run_gasha_daily(FakeSession(client), log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["targets"], [99999])
        self.assertEqual(client.spawn_bodies[0]["_key"], 99999)

    def test_no_daily_banner_is_a_clean_skip(self) -> None:
        client = FakeClient(_list_payload(_banner(20007, daily=False)))

        result = run_gasha_daily(FakeSession(client), log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_reason"], "no_daily_available")
        self.assertEqual(client.spawn_bodies, [])

    def test_failed_banner_is_skipped_without_aborting_the_batch(self) -> None:
        client = FakeClient(
            _list_payload(
                _banner(20007, daily=True),
                _banner(30006, daily=True),
                _banner(40004, daily=True, count=10),
            ),
            spawn_results=[
                {"_code": -11006, "_message": "not enough crystal"},
                {"_code": 0},
                {"_code": 0},
            ],
        )

        result = run_gasha_daily(FakeSession(client), log=lambda _line: None)

        # Short on Crystal for one banner must not stop the other two.
        self.assertEqual(len(client.spawn_bodies), 3)
        self.assertFalse(result["ok"])
        self.assertEqual([entry["key"] for entry in result["pulled"]], [30006, 40004])
        self.assertEqual([entry["key"] for entry in result["failed"]], [20007])
        self.assertEqual(result["failed"][0]["code"], -11006)
        self.assertEqual(result["fail_count"], 1)
        self.assertEqual(result["ok_count"], 2)

    def test_transport_error_on_one_banner_does_not_abort_the_batch(self) -> None:
        client = FakeClient(
            _list_payload(_banner(20007, daily=True), _banner(30006, daily=True))
        )
        original = client.post_encrypted
        state = {"spawn": 0}

        def flaky(path: str, body: dict) -> dict:
            if path == "/api/gasha/spawn":
                state["spawn"] += 1
                if state["spawn"] == 1:
                    client.calls.append((path, dict(body)))
                    raise RuntimeError("connection reset")
            return original(path, body)

        client.post_encrypted = flaky  # type: ignore[assignment]

        result = run_gasha_daily(FakeSession(client), log=lambda _line: None)

        self.assertEqual([entry["key"] for entry in result["failed"]], [20007])
        self.assertEqual([entry["key"] for entry in result["pulled"]], [30006])

    def test_rewards_are_counted(self) -> None:
        client = FakeClient(
            _list_payload(_banner(GASHA_PARTNER, daily=True)),
            spawn_results=[
                {
                    "_code": 0,
                    "_gasha": {"_key": GASHA_PARTNER, "_isDailyCanGet": False},
                    "_rewardAllList": {
                        "_rewardList": {
                            "_list": [
                                {"_type": 2, "_value": 200400, "_count": 1},
                                {"_type": 4, "_value": 250000, "_count": 1},
                            ]
                        }
                    },
                }
            ],
        )

        result = run_gasha_daily(FakeSession(client), log=lambda _line: None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reward_total"], 2)
        self.assertFalse(result["pulled"][0]["after"]["isDailyCanGet"])

    def test_session_kick_on_list_propagates(self) -> None:
        client = FakeClient({"_code": -19006})

        with self.assertRaises(SessionKicked):
            run_gasha_daily(FakeSession(client), log=lambda _line: None)

    def test_session_kick_on_spawn_propagates(self) -> None:
        client = FakeClient(
            _list_payload(_banner(GASHA_SP, daily=True)),
            spawn_results=[{"_code": -19006}],
        )

        with self.assertRaises(SessionKicked):
            run_gasha_daily(FakeSession(client), log=lambda _line: None)


if __name__ == "__main__":
    unittest.main()
