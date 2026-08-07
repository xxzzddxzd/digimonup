"""Rune / 纹章 APIs.

The rune inventory is cursor-paginated by the current server.  Reinforcement
uses the target rune UID plus a list of ingredient UIDs; the iOS client sends
that list in small pages and repeats the request until all selected
ingredients have been accepted.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

if TYPE_CHECKING:
    from ..http_client import ApiClient


RUNE_LIST_PATH = "/api/rune/list"
RUNE_LEVEL_UP_PATH = "/api/rune/level-up"

# The client-side PAGE_RANGE is obfuscated in the binary.  Ten IDs is a
# conservative page size accepted by the live endpoint and keeps request
# bodies small when an account has hundreds of runes.
REINFORCE_PAGE_SIZE = 10


def rune_list(
    client: "ApiClient",
    *,
    cursor: str = "",
    filter_grade: int | None = None,
    filter_stat_type_list: Sequence[int] | None = None,
    filter_match_count: int | None = None,
) -> dict:
    """Fetch one page from ``/api/rune/list``.

    ``_cursor`` is required by the current server, including for the first
    page (where it must be the empty string).
    """
    body: dict[str, Any] = {"_cursor": str(cursor or "")}
    if filter_grade is not None:
        body["_filterGrade"] = int(filter_grade)
    if filter_stat_type_list is not None:
        body["_filterStatTypeList"] = [int(v) for v in filter_stat_type_list]
    if filter_match_count is not None:
        body["_filterMatchCount"] = int(filter_match_count)
    return client.post_encrypted(RUNE_LIST_PATH, body)


def rune_list_all(
    client: "ApiClient",
    *,
    filter_grade: int | None = None,
    filter_stat_type_list: Sequence[int] | None = None,
    filter_match_count: int | None = None,
    max_pages: int = 10000,
) -> list[dict]:
    """Return every rune in the inventory by following ``_nextCursor``."""
    rows: list[dict] = []
    cursor = ""
    seen: set[str] = set()
    for _page in range(max(1, int(max_pages))):
        if cursor in seen:
            raise RuntimeError(f"rune list cursor repeated: {cursor!r}")
        seen.add(cursor)
        body = rune_list(
            client,
            cursor=cursor,
            filter_grade=filter_grade,
            filter_stat_type_list=filter_stat_type_list,
            filter_match_count=filter_match_count,
        )
        code = int(body.get("_code", 0) or 0) if isinstance(body, dict) else 0
        if code != 0:
            raise RuntimeError(
                f"rune list failed code={code} "
                f"message={body.get('_message') if isinstance(body, dict) else None}"
            )
        box = body.get("_runeList") if isinstance(body, dict) else None
        box = box if isinstance(box, dict) else {}
        page_rows = box.get("_list") or []
        rows.extend(row for row in page_rows if isinstance(row, dict))
        next_cursor = str(box.get("_nextCursor") or "")
        if not next_cursor:
            return rows
        cursor = next_cursor
    raise RuntimeError(f"rune list exceeded max_pages={max_pages}")


def rune_level_up(
    client: "ApiClient",
    *,
    rune_uid: str,
    ingredients_uids: Iterable[str],
) -> dict:
    """Send one reinforcement request.

    The server consumes the ingredient UIDs in this request and returns the
    updated target in ``_rune`` plus the consumed IDs in ``_ingredientsUID``.
    """
    uid = str(rune_uid or "").strip()
    if not uid:
        raise ValueError("rune_uid must not be empty")
    ingredients = [str(value).strip() for value in ingredients_uids if str(value).strip()]
    if not ingredients:
        raise ValueError("ingredients_uids must not be empty")
    return client.post_encrypted(
        RUNE_LEVEL_UP_PATH,
        {"_runeUID": uid, "_ingredientsUID": ingredients},
    )


def rune_level_up_all(
    client: "ApiClient",
    *,
    rune_uid: str,
    ingredients_uids: Iterable[str],
    page_size: int = REINFORCE_PAGE_SIZE,
    on_page: Callable[[int, int, dict], None] | None = None,
) -> dict:
    """Reinforce a target with all supplied ingredients in pages.

    ``page_size`` is intentionally configurable for testing and for future
    server versions.  The return value contains every page response and the
    latest target response; callers can inspect ``consumed_uids`` to verify
    what the server actually removed.
    """
    size = int(page_size)
    if size < 1:
        raise ValueError(f"page_size must be >= 1, got {page_size}")
    ingredients = []
    seen: set[str] = set()
    for value in ingredients_uids:
        uid = str(value or "").strip()
        if uid and uid not in seen:
            ingredients.append(uid)
            seen.add(uid)
    if not ingredients:
        raise ValueError("ingredients_uids must not be empty")

    pages: list[dict] = []
    consumed: list[str] = []
    total = (len(ingredients) + size - 1) // size
    latest: dict | None = None
    for index in range(total):
        chunk = ingredients[index * size : (index + 1) * size]
        response = rune_level_up(
            client,
            rune_uid=rune_uid,
            ingredients_uids=chunk,
        )
        pages.append(response)
        latest = response
        response_consumed = response.get("_ingredientsUID") if isinstance(response, dict) else None
        if isinstance(response_consumed, list):
            consumed.extend(str(value) for value in response_consumed if value)
        if on_page is not None:
            on_page(index + 1, total, response)
        code = int(response.get("_code", 0) or 0) if isinstance(response, dict) else 0
        if code != 0:
            break

    return {
        "ok": bool(latest) and all(
            int(page.get("_code", 0) or 0) == 0
            for page in pages
            if isinstance(page, dict)
        ),
        "target_uid": str(rune_uid),
        "requested_uids": ingredients,
        "consumed_uids": consumed,
        "pages": pages,
        "latest": latest or {},
    }
