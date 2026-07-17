# autorun — DIGIMON UP protocol client (1.0.2)

Python reimplementation of the content-server protocol from:
- Charles capture `dm-content.dgup.channel.or.jp.chlsj`
- IL2CPP headers under `../102/Cpp2IL/DiffableCs`
- IDA decompile of `PacketEncryptUtil` / `PacketManager` / battle packets

## Protocol summary

1. `POST /api/app-version/check` (plain)
2. `POST /api/account/public-key` (plain) → RSA public key
3. Client generates AES-256 key (32B hex) + IV (16B hex)
4. RSA-OAEP(SHA1) encrypt `{"key":hexKey,"iv":hexIv}` → `_encryptedKey`
5. `POST /api/account/auth` (plain) with device/account fields from capture
6. Subsequent APIs: AES-CBC/PKCS7 encrypt request JSON, body:
   `{"_dataNo": "...", "_data": "<base64>"}`
7. `Authorization: Bearer <sessionKey>`

## Run

```bash
cd autorun
python3 -m pip install -r requirements.txt
python3 main.py
```

Options:
```bash
python3 main.py --skip-battle
python3 main.py --region 1 --stage 1 --sector 0
```

Account defaults are loaded from the capture (`client/config.py`).


## Farm + drop stats

```bash
# farm from capture stage (23-2), fallback to previous sector/stage on fail
python3 main.py --farm --count 10 --stats drop_stats.json

# infinite farm (Ctrl+C to stop)
python3 main.py --farm --count 0 --stats drop_stats.json

# force stage/sector
python3 main.py --farm --stage 23 --sector 2 --count 20
```

Drop aggregation reads `battle/end._rewardAllList` and writes `drop_stats.json`.


## Stay farm (login frontier)

```bash
# farm current startable frontier from login (repeat=0)
python3 main.py --stay --count 0

# on non-progress fail code=-XXXX: wait 600s, re-login, retarget frontier
# invalid stage/sector/repeat (-28002/-28003/-28004) auto-retarget without wait
python3 main.py --stay --count 0 --recover-wait 600
```

Server only accepts the **current frontier** with `repeat=0`. Previous stage/sector
returns `-28002/-28003`; `repeat=1` returns `-28004`. After Clear the server advances,
so stay mode follows the new frontier.


## Default

```bash
python3 main.py
```

Defaults to **TUI + infinite stay farm** on the **current login frontier**.

## TUI dashboard

```bash
# live dashboard, farm latest stage
python3 main.py --tui --count 0

# same as --tui --farm --stay
python3 main.py --tui --stay --count 20 --stats drop_stats.json
```

Panels: Session / Current Stage / Drop Stats / HTTP / Events.


## Import account from capture

```bash
# parse Charles export, overwrite local account.json, then exit
python3 main.py --input dm-content.dgup.channel.or.jp.chlsj

# normal run uses account.json if present
python3 main.py --skip-battle
```

Local account file: `account.json` (auto-loaded on startup).
