# autorun

DIGIMON UP 自动推图 / 维护。

## 安装

```bash
cd autorun
python3 -m pip install -r requirements.txt
```

## 导入账号

```bash
python3 main.py --input 你的抓包.chlsj
```

生成本地 `account.json`（不进 git）。

## 自动推图

```bash
python3 main.py runloop
```

TUI + 无限刷当前可打关卡。`Ctrl+C` 停止。

## 定时维护 auto

```bash
python3 main.py auto
```

单次：登录 → 肉田 → 异次元 box → 亲密点触（冷却则跳过）→ AFK，然后退出。

### macOS crontab（推荐）

cron **读不了** `~/Documents`（`Operation not permitted`），入口必须放在 Documents 外：

```bash
# 1) 从终端同步代码+账号到 cron 副本（改代码后执行）
./sync_cron_copy.sh

# 2) 安装入口（只需一次）
mkdir -p ~/cron-jobs
cp install_cron_entry.sh ~/cron-jobs/run_smbb_auto.sh
chmod +x ~/cron-jobs/run_smbb_auto.sh

# 3) crontab
0 * * * * /Users/xuzhengda/cron-jobs/run_smbb_auto.sh
```

- 运行目录：`~/cron-jobs/smbb-autorun`
- 日志：`~/cron-jobs/smbb-auto-cron.log`、`~/cron-jobs/smbb-autorun/logs/`
- 改代码后务必再跑一次 `./sync_cron_copy.sh`

## 其他

```bash
python3 main.py afk   # AFK 奖励
python3 main.py qmd   # 亲密点触（未到冷却会等待）
```
