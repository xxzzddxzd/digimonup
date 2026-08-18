"""Quest APIs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient


def quest_list(client: "ApiClient") -> dict:
    """POST /api/quest/list — return the player's current quest rows."""
    return client.post_encrypted("/api/quest/list", {})


def quest_complete(client: "ApiClient", *, keys: Sequence[int]) -> dict:
    """POST /api/quest/complete {_keys:[...]} — claim completed quests."""
    return client.post_encrypted(
        "/api/quest/complete",
        {"_keys": [int(key) for key in keys]},
    )
