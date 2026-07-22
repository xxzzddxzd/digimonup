"""Gasha (gacha) APIs.

IL2CPP:
  PS_GashaInfos  POST /api/gasha/list   {}
  PS_GashaSpawn  POST /api/gasha/spawn   {_key, _count, _isDaily}

1.0.2 GameData.Gasha:
  20000 Supporter   (伙伴)  ga1
  30000 HolyWeapon  (SP)    ga2
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient

# Banner keys from GameData.json (1.0.2)
GASHA_PARTNER = 20000  # Supporter / 伙伴
GASHA_SP = 30000       # HolyWeapon / SP


def gasha_infos(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/gasha/list", {})


def gasha_spawn(
    client: "ApiClient",
    *,
    key: int,
    count: int = 1,
    is_daily: bool = False,
) -> dict:
    return client.post_encrypted(
        "/api/gasha/spawn",
        {
            "_key": int(key),
            "_count": int(count),
            "_isDaily": bool(is_daily),
        },
    )
