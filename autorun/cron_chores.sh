#!/bin/zsh
# Combined qmd + afk for cron. Single login, no dual-session kick.
# qmd does NOT long-wait cooldown (skip if not ready); afk claims whatever is pending.
set -u

DIR="/Users/xuzhengda/Documents/workspace/smbb/autorun"
PY="/Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3"
LOG_DIR="$DIR/logs"
LOCK="$LOG_DIR/cron_chores.pid"
mkdir -p "$LOG_DIR"
cd "$DIR" || exit 1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [[ -f "$LOCK" ]]; then
  old=$(cat "$LOCK" 2>/dev/null || true)
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "[$(ts)] skip: already running pid=$old"
    exit 0
  fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

echo "===== [$(ts)] cron_chores start ====="
"$PY" -u - <<'PY'
from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from client.account_store import apply_account_to_config, load_account_file
from client.apis import afk as afk_api
from client.heartbeat import HeartbeatService
from client.partner_care import run_qmd
from client.session import GameSession

DUMP = Path("last_run.json")
result = {"ok": False, "mode": "cron_chores", "steps": {}}


def load_session() -> GameSession:
    session = GameSession()
    saved = load_account_file()
    if saved:
        apply_account_to_config(session.config, saved)
        session.client.data_no = session.config.account.data_no
    return session


def summarize_afk(tag: str, body: dict) -> None:
    print(f"[*] {tag} code={body.get('_code')} msg={body.get('_message')}")
    afk = body.get("_afk") if isinstance(body.get("_afk"), dict) else {}
    if afk:
        print(
            f"    afk count={afk.get('_rewardIntervalCount', afk.get('count'))} "
            f"adCount={afk.get('_adCount', afk.get('adCount'))} "
            f"lastRewardTime={afk.get('_lastRewardTime', afk.get('lastObtainTime'))}"
        )


hb = None
session = load_session()
# quiet http noise for cron; keep high-level prints
session.client.log_enabled = False
try:
    session.run_login_pipeline()
    print("[+] login pipeline ok")
    hb = HeartbeatService(session, log=print)
    hb.start()

    # qmd: no long wait — next cron tick will retry (~21min)
    care = run_qmd(session, wait_cooldown=False, log=print)
    result["steps"]["qmd"] = care
    print(f"[*] qmd ok={care.get('ok')} cooldown_sec={care.get('cooldown_sec')} err={care.get('error')}")

    # small gap before next API family
    time.sleep(1.0)

    listed = afk_api.reward_list(session.client)
    result["steps"]["afk_reward_list"] = {
        "code": listed.get("_code"),
        "message": listed.get("_message"),
    }
    summarize_afk("afk/reward-list", listed)

    obtained = afk_api.reward_obtain(session.client)
    result["steps"]["afk_reward"] = {
        "code": obtained.get("_code"),
        "message": obtained.get("_message"),
    }
    summarize_afk("afk/reward", obtained)

    ad = afk_api.ad_view(session.client)
    result["steps"]["afk_ad_view"] = {
        "code": ad.get("_code"),
        "message": ad.get("_message"),
    }
    summarize_afk("afk/ad-view", ad)

    # overall ok if neither hard-failed login; soft skips are fine
    qmd_ok = bool(care.get("ok")) or bool(care.get("error"))  # cooling is soft ok
    afk_ok = listed.get("_code", 0) in (0, None) and obtained.get("_code", 0) in (0, None)
    result["ok"] = bool(afk_ok)
    result["qmd_claimed"] = bool(care.get("ok"))
    print(f"[+] cron_chores done qmd_claimed={result['qmd_claimed']} afk_ok={afk_ok}")
except Exception as exc:
    result["error"] = str(exc)
    result["trace"] = traceback.format_exc()
    print("[-] FAILED:", exc)
    traceback.print_exc()
    raise SystemExit(1)
finally:
    try:
        if hb is not None:
            hb.stop()
    except Exception:
        pass
    try:
        DUMP.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] wrote {DUMP}")
    except Exception as e:
        print(f"[!] write dump failed: {e}")
PY
rc=$?
echo "===== [$(ts)] cron_chores end rc=$rc ====="
exit $rc
