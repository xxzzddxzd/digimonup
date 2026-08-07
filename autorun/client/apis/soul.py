"""Soul / 纹章 APIs.

The game's crest-like items are the Soul feature (their data icons are
``Crest_*``).  Both inventory listing and level-up are cursor/page based.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from ..http_client import ApiClient


SOUL_LIST_PATH = "/api/soul/list"
SOUL_EQUIP_LIST_PATH = "/api/soul/equip-list"
SOUL_LEVEL_UP_PATH = "/api/soul/level-up"

# The iOS PAGE_RANGE is obfuscated in the binary.  Ten ingredient UIDs is a
# conservative live-compatible page size and is small enough to avoid large
# encrypted request bodies for accounts with thousands of Souls.
LEVEL_UP_PAGE_SIZE = 10


def soul_list(client: "ApiClient", *, cursor: str = "") -> dict:
    """Fetch one page; the current server requires ``_cursor`` even initially."""
    return client.post_encrypted(SOUL_LIST_PATH, {"_cursor": str(cursor or "")})


def iter_soul_list(
    client: "ApiClient",
    *,
    max_pages: int = 100000,
    on_page: Callable[[int, int], None] | None = None,
) -> Iterable[dict]:
    """Yield every Soul row while following ``_nextCursor``."""
    cursor = ""
    seen: set[str] = set()
    for page in range(1, max(1, int(max_pages)) + 1):
        if cursor in seen:
            raise RuntimeError(f"soul list cursor repeated: {cursor!r}")
        seen.add(cursor)
        body = soul_list(client, cursor=cursor)
        code = int(body.get("_code", 0) or 0) if isinstance(body, dict) else 0
        if code != 0:
            raise RuntimeError(
                f"soul list failed code={code} "
                f"message={body.get('_message') if isinstance(body, dict) else None}"
            )
        box = body.get("_soulList") if isinstance(body, dict) else None
        box = box if isinstance(box, dict) else {}
        rows = [row for row in (box.get("_list") or []) if isinstance(row, dict)]
        if on_page is not None:
            on_page(page, len(rows))
        yield from rows
        cursor = str(box.get("_nextCursor") or "")
        if not cursor:
            return
    raise RuntimeError(f"soul list exceeded max_pages={max_pages}")


def soul_list_all(client: "ApiClient", *, max_pages: int = 100000) -> list[dict]:
    return list(iter_soul_list(client, max_pages=max_pages))


def soul_equip_list(client: "ApiClient") -> dict:
    return client.post_encrypted(SOUL_EQUIP_LIST_PATH, {})


def soul_level_up(
    client: "ApiClient",
    *,
    soul_uid: str,
    ingredients_uids: Iterable[str],
) -> dict:
    """Send one ``/api/soul/level-up`` page."""
    target = str(soul_uid or "").strip()
    if not target:
        raise ValueError("soul_uid must not be empty")
    ingredients = [str(value).strip() for value in ingredients_uids if str(value).strip()]
    if not ingredients:
        raise ValueError("ingredients_uids must not be empty")
    return client.post_encrypted(
        SOUL_LEVEL_UP_PATH,
        {"_soulUID": target, "_ingredientsUID": ingredients},
    )
