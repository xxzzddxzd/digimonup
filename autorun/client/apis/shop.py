"""Shop-related APIs and GameData catalog helpers."""
from __future__ import annotations

import base64
import csv
import json
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from ..http_client import ApiClient

# tp.GameDataCrypto static key/iv (AES-256-CBC + raw deflate)
GAMEDATA_AES_KEY_HEX = "113df5b62b349fc0384a10f3bcc06fad7bcb4b58fb3d232b4fab0bcce959fae3"
GAMEDATA_AES_IV_HEX = "294f993d6aae96d25bd829642c5689c7"

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def shop_list(client: "ApiClient") -> dict:
    """POST /api/shop/list — player buy/reset state for shop keys."""
    return client.post_encrypted("/api/shop/list", {})


def shop_buy(client: "ApiClient", *, key: int, count: int = 1) -> dict:
    """POST /api/shop/buy {_key,_count} — PS_ShopBuy."""
    return client.post_encrypted(
        "/api/shop/buy",
        {"_key": int(key), "_count": int(count)},
    )


def shop_level_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/shop-level/list", {})


def goods_list(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/goods/list", {})


def purchase_limit(client: "ApiClient") -> dict:
    return client.post_encrypted("/api/purchase/limit", {})


def decrypt_gamedata_text(encrypted_b64: str, *, key_hex: str = GAMEDATA_AES_KEY_HEX, iv_hex: str = GAMEDATA_AES_IV_HEX) -> str:
    """Base64 -> AES-CBC -> PKCS7 unpad -> raw deflate -> UTF-8 JSON text."""
    ct = base64.b64decode(encrypted_b64.strip())
    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    pad = pt[-1]
    if 1 <= pad <= 16 and pt.endswith(bytes([pad]) * pad):
        pt = pt[:-pad]
    return zlib.decompress(pt, -zlib.MAX_WBITS).decode("utf-8")


def load_gamedata(path: Optional[Path] = None) -> dict:
    """Load decrypted GameData JSON (autorun/data/gamedata_dec.json by default)."""
    p = path or (DATA_DIR / "gamedata_dec.json")
    return json.loads(p.read_text(encoding="utf-8"))


def build_shop_catalog(gamedata: dict, shop_list_resp: dict | None = None) -> dict[str, Any]:
    gd = gamedata["GameData"] if "GameData" in gamedata else gamedata
    shop = gd["Shop"]
    package = gd.get("ShopPackage") or {}
    group = gd.get("ShopGroup") or {}
    cat = gd.get("ShopCategory") or {}
    vgroup = gd.get("ShopVerticalGroup") or {}
    live_map: dict[int, dict] = {}
    if shop_list_resp:
        for item in (shop_list_resp.get("_shopList") or {}).get("_list") or []:
            live_map[int(item["_key"])] = item

    rows = []
    for key_s, info in sorted(shop.items(), key=lambda kv: int(kv[0])):
        key = int(key_s)
        pkg_key = info.get("PackageKey")
        pkg = package.get(str(pkg_key), {}) if pkg_key is not None else {}
        grp_key = pkg.get("GroupKey")
        grp = group.get(str(grp_key), {}) if grp_key is not None else {}
        cat_key = grp.get("CategoryKey")
        cati = cat.get(str(cat_key), {}) if cat_key is not None else {}
        vg_key = info.get("ShopVerticalGroupKey")
        vg = vgroup.get(str(vg_key), {}) if vg_key is not None else {}
        st = live_map.get(key, {})
        limit = info.get("BuyCount")
        # Server `_buyCount` is remaining quota, not used count.
        remain_raw = st.get("_buyCount")
        remain = None
        used = None
        if remain_raw is not None:
            try:
                remain = max(0, int(remain_raw))
            except Exception:
                remain = None
            if limit is not None and remain is not None:
                try:
                    used = max(0, int(limit) - int(remain))
                except Exception:
                    used = None
        rows.append(
            {
                "key": key,
                "active": info.get("Active"),
                "name": info.get("Name") or "",
                "productName": info.get("ProductName") or "",
                "desc": info.get("Desc") or "",
                "shopType": info.get("ShopType") or "",
                "buyType": info.get("BuyType") or "",
                "buyValue": info.get("BuyValue", ""),
                "buyValueYen": info.get("BuyValueYen", ""),
                "buyValueWon": info.get("BuyValueWon", ""),
                "buyValueDollar": info.get("BuyValueDollar", ""),
                "buyCountLimit": limit if limit is not None else "",
                "productCount": info.get("ProductCount", ""),
                "resetType": info.get("ResetType", ""),
                "rewardKey": info.get("RewardKey", ""),
                "packageKey": pkg_key if pkg_key is not None else "",
                "packageUITag": pkg.get("UITag", ""),
                "groupKey": grp_key if grp_key is not None else "",
                "groupUITag": grp.get("UITag", ""),
                "categoryKey": cat_key if cat_key is not None else "",
                "categoryUITag": cati.get("UITag", ""),
                "verticalGroupKey": vg_key if vg_key is not None else "",
                "verticalGroupName": vg.get("Name", ""),
                "inLiveShopList": key in live_map,
                "liveBuyCount": used if used is not None else "",
                "liveRemain": remain if remain is not None else "",
                "liveResetTime": st.get("_resetTime", "") if st else "",
                "liveStartTime": st.get("_startTime", "") if st else "",
                "liveEndTime": st.get("_endTime", "") if st else "",
                "liveExpiredTime": st.get("_expiredTime", "") if st else "",
            }
        )
    return {
        "dataNo": gamedata.get("DataNo"),
        "version": gamedata.get("Version"),
        "liveShopCount": len(live_map),
        "catalogCount": len(rows),
        "categories": cat,
        "groups": group,
        "packages": package,
        "verticalGroups": vgroup,
        "items": rows,
    }


def write_shop_catalog(catalog: dict, out_dir: Optional[Path] = None) -> tuple[Path, Path]:
    out = out_dir or DATA_DIR
    out.mkdir(parents=True, exist_ok=True)
    jp = out / "shop_catalog.json"
    cp = out / "shop_catalog.csv"
    jp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    items = catalog.get("items") or []
    if items:
        with cp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(items[0].keys()))
            w.writeheader()
            w.writerows(items)
    return jp, cp
