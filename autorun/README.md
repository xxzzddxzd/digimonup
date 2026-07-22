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
| `python3 main.py auto` | 单次维护：肉田 → 训练 → 探查 → 异次元 box → 亲密点触 → AFK |
| `python3 main.py ts` | **数码世界 / 探索** Textual 交互 TUI：鼠标点格行走 / 钻头 / 冲锋 / 领里程（`mine` 为别名） |
| `python3 main.py zb` | **开装备**：spawn-and-sell（默认每批 8）；`--info` 看炉子快照 |

无参数时打印 help 与示例。需要 `ts` 时请安装依赖：`pip install -r requirements.txt`（含 `textual`）。

### 数码世界交互 ts

```bash
python3 main.py ts
```

登录后进入 Textual 界面：

- **点击空地/道具**：在合法范围内行走（col±1 或 row±1）
- **点击岩石**：合法范围内默认使用钻头（无需开关；无钻头会提示）
- **冲锋 [f]**：同 lane 向更深 row+3（消耗冲锋）
- **领里程 [c]**：尝试领取距离里程奖励
- **刷新 [r]** / **退出 [q]**（`[d]` 钻头模式开关已可选，点岩默认即钻）

`auto` 里的自动探查逻辑不变；`ts` 仅手动游玩。

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
4. 探查数码世界 / Mine：耗尽体力捡特训芯片，可冲锋/钻头，尝试里程奖励
5. 异次元 box：领取红点 → 续上自己 box + 公开 box → 按规则攻击
6. 亲密点触：遍历 **所有伙伴**（各自独立冷却）；就绪的先 `change-character` 再 `relation-exp`；全冷却则跳过，不长等
7. AFK 领取

结果追加到 `logs/auto.log`。遇会话踢出 `-19006` 会等待后重登并再跑完一轮。



### 开装备 zb

```bash
python3 main.py zb              # 开 1 批装备（数量=当前炉子 SpawnCount，lv17=8）
python3 main.py zb --batches 5  # 连续 5 批
python3 main.py zb --info       # 只查炉子快照 / 升级所需 bit（不操作）
```

开装备走 `POST /api/item/spawn-and-sell`。炉子维护（查 info / 投 bit / 满了建造 / 建造完成）在 `auto` 里由 `run_item_spawner_care` 自动跑，不进 `zb`。

本地表：`item_spawner_table.json`。剩余 bit = `Gold * (GoldCount - _count)`。

## 训练配置

手动编辑 `lab_config.json`（与 `main.py` 同目录）：

```json
{
  "default_max_level": 10,
  "max_level": {
    "14": 1, "20": 1, "26": 1, "34": 1,
    "33": 999, "35": 999, "36": 999
  },
  "priority": [11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
}
```

- `max_level`：每个训练点 `_key` 的等级上限（满级后 auto 会换下一个）
  - 默认 10；`14/20/26/34` 为 1；`33/35/36` 为 999
- `priority`：选下一点时的优先顺序
- `default_max_level`：未写明的 key 默认上限

## 辅助脚本

| 脚本 | 作用 |
| --- | --- |
| `./run_auto.sh` | 在本目录执行一次 `main.py auto` |
| `./install_cron_entry.sh` | 同上（带 skip-if-running + cron 日志） |
| `./kill_auto.sh` | 只杀 `main.py auto` / 旧 `qmdauto` |
| `./ensure_qmdauto.sh` | 兼容旧名，转调本目录 auto |

## macOS crontab（与 dqsg 相同：直接跑本仓库）

```bash
# crontab -e 加一行（每小时）
0 * * * * cd /Users/xuzhengda/Documents/workspace/smbb/autorun && /Users/xuzhengda/.pyenv/versions/3.12.8/bin/python3 main.py auto >> logs/auto_cron.log 2>&1
```

- 运行目录就是 `Documents/workspace/smbb/autorun`，**改代码后无需 sync**
- 日志：`logs/auto.log`、`logs/auto_run.log`（若用 `install_cron_entry.sh`）、`logs/auto_cron.log`

## 本地文件

- `account.json`：账号（导入生成，不提交）
- `logs/`：运行日志
- `last_run.json` / `drop_stats.json`：最近一次运行摘要与掉落（gitignore）
