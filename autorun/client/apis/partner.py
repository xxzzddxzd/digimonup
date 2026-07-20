"""Partner care / relation APIs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..http_client import ApiClient


def collect_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/partner/collect-list", {})


def relation_exp(client: "ApiClient") -> dict:
    """Feed / touch current partner for relation exp. Empty body."""
    return client.post_encrypted("/api/partner/relation-exp", {})


def relation_reward(client: "ApiClient", *, key: int) -> dict:
    return client.post_encrypted("/api/partner/relation-reward", {"_key": int(key)})


def growth_complete(client: "ApiClient", *, base_key: int) -> dict:
    return client.post_encrypted("/api/partner/growth-complete", {"_baseKey": int(base_key)})
