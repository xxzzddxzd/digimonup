# autorun

DIGIMON UP 协议端自动推图 / 定时维护。

## 安装

```bash
cd autorun
python3 -m pip install -r requirements.txt
```

## 导入账号

```bash
python3 main.py --input 你的抓包.chlsj
```

生成本地 `account.json`（已 gitignore，不进仓库）。

## 主要命令

| 命令 | 作用 |
| --- | --- |
| `python3 main.py --input FILE` | 从 Charles `.chlsj` / 抓包 JSON 导入账号 |
| `python3 main.py runloop` | TUI 无限刷当前可打关卡 |
| `python3 main.py auto` | 单次维护：肉田 → 训练 → 异次元 box → 亲密点触 → AFK |

无参数时打印 help 与示例。

### 自动推图

```bash
python3 main.py runloop
```

登录后 TUI + 无限 stay 刷当前登录进度关卡。`Ctrl+C` 停止。掉落统计写 `drop_stats.json`，摘要写 `last_run.json`。

### 定时维护 auto

```bash
python3 main.py auto
```

单次流程（外部 crontab 每小时调度，不在进程内长睡）：

1. 登录
2. 肉田维护（浇水等）
3. 训练 / Lab：有完成项则领取 → 重开同一训练 → 请求大家帮助
4. 异次元 box：领取红点 → 续上自己 box + 公开 box → 按规则攻击
5. 亲密点触（冷却中则跳过，不长等）
6. AFK 领取

结果追加到 `logs/auto.log`。遇会话踢出 `-19006` 会等待后重登并再跑完一轮。

## 辅助脚本

| 脚本 | 作用 |
| --- | --- |
| `./sync_cron_copy.sh` | 把本目录同步到 `~/cron-jobs/smbb-autorun`（改代码后跑） |
| `./install_cron_entry.sh` | cron 入口模板：在 Documents 外跑 `main.py auto` |
| `./run_auto.sh` | 先 sync（可读时）再执行 Documents 外入口 |
| `./kill_auto.sh` | 只杀 `main.py auto` / 旧 `qmdauto` |
| `./ensure_qmdauto.sh` | 兼容旧 keepalive，转调 hourly 入口 |

## macOS crontab（推荐）

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

## 本地文件

- `account.json`：账号（导入生成，不提交）
- `logs/`：运行日志
- `last_run.json` / `drop_stats.json`：最近一次运行摘要与掉落（gitignore）
