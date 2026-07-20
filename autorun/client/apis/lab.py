"""Lab / 训练 (garden lab) APIs.

Capture (2026-07-20):
  POST /api/lab/info     {}
  POST /api/lab/list     {}                 # tech levels
  POST /api/lab/complete {"_key": int}
  POST /api/lab/run      {"_key": int}
  POST /api/camp/help    {"_helpContentType": 2}  # Lab ask-help

IL2CPP:
  PS_LabInfos / PS_LabRunComplete / PS_LabRun
  PS_GuildAssist_Help(_helpContentType) with E_CAMP_HELP_CONTENT_TYPES.Lab = 2
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..http_client import ApiClient

# E_CAMP_HELP_CONTENT_TYPES
HELP_ITEM_SPAWNER = 1
HELP_LAB = 2
HELP_FARM = 3


def lab_info(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/lab/info", {})


def lab_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/lab/list", {})


def lab_complete(client: "ApiClient", *, key: int) -> dict:
    return client.post_encrypted("/api/lab/complete", {"_key": int(key)})


def lab_run(client: "ApiClient", *, key: int) -> dict:
    return client.post_encrypted("/api/lab/run", {"_key": int(key)})


def camp_help(client: "ApiClient", *, help_content_type: int = HELP_LAB) -> dict:
    """Ask camp members to help accelerate content (Lab=2)."""
    return client.post_encrypted(
        "/api/camp/help",
        {"_helpContentType": int(help_content_type)},
    )
