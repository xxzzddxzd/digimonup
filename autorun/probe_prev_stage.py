#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from client.session import GameSession

s = GameSession()
s.client.log_enabled = False
s.run_login_pipeline()
info = s.battle_info or {}
print("FRONTIER", info, flush=True)
region = int(info.get("_region") or 1)
stage = int(info.get("_stage") or 1)
sector = int(info.get("_sector") or 1)

candidates = [
    ("stage-1 sec1 r0", region, stage - 1, 1, 0),
    ("stage-1 sec1 r1", region, stage - 1, 1, 1),
    ("stage-1 sec10 r0", region, stage - 1, 10, 0),
    ("stage-1 sec10 r1", region, stage - 1, 10, 1),
    ("stage-1 sec9 r0", region, stage - 1, 9, 0),
    ("stage-1 sec9 r1", region, stage - 1, 9, 1),
    ("same stage sector-1 r0", region, stage, max(1, sector - 1), 0),
    ("same stage sector-1 r1", region, stage, max(1, sector - 1), 1),
    ("same stage sector1 r1", region, stage, 1, 1),
    ("frontier r1", region, stage, sector, 1),
    ("frontier r0 control", region, stage, sector, 0),
]
for name, r, st, sec, rep in candidates:
    if st < 1 or sec < 1:
        print(name, "skip", flush=True)
        continue
    resp = s.battle_start(region=r, stage=st, sector=sec, repeat=rep, wave=0, state=0, attr=1)
    code = resp.get("_code")
    msg = resp.get("_message") or resp.get("_details")
    print(f"{name}: {st}-{sec} rep={rep} -> code={code} msg={msg}", flush=True)
    if code in (0, None):
        end = s.battle_end(region=r, reason=4, state=0, damage="0")
        print("  end-failed", end.get("_code"), end.get("_message"), end.get("_battle"), flush=True)
        s.init_game_data()
        print("  refresh", s.battle_info, flush=True)
print("DONE", flush=True)
