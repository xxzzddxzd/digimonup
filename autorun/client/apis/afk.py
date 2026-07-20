"""AFK reward APIs: list / claim / ad-view.

From IL2CPP:
  PS_AFKRewardList   -> /api/afk/reward-list
  PS_AFKRewardObtain -> /api/afk/reward
  PS_AFKRewardADView -> /api/afk/ad-view
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient


def reward_list(client: "ApiClient") -> dict:
    """Query pending AFK rewards (ContentsRewardParam _afk)."""
    return client.post_encrypted("/api/afk/reward-list", {})


def reward_obtain(client: "ApiClient") -> dict:
    """Claim AFK rewards (_afk + _rewardAllList)."""
    return client.post_encrypted("/api/afk/reward", {})


def ad_view(client: "ApiClient") -> dict:
    """Watch-ad style AFK bonus (_adCount + _rewardAllList)."""
    return client.post_encrypted("/api/afk/ad-view", {})
