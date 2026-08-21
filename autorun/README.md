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
| `python3 main.py auto` | 单次维护：肉田 → 公会 → 副本 → 系列任务 → 训练 → 探查 → 异次元 box → 炉子 → 竞技场 PVP → 亲密点触 → AFK |
| `python3 main.py pvp` | **竞技场**：常规+赛季，各选战力最低挑战，直到两种票都耗尽 |
| `python3 main.py ts` | **数码世界 / 探索** Textual 交互 TUI：鼠标点格行走 / 钻头 / 冲锋 / 领里程（`mine` 为别名） |
| `python3 main.py zb` | **开装备**：读取并开完当前装备生成券；`--info` 看炉子快照 |
| `python3 main.py fb 1` / `fb 2` / `fb 3` | 清一次副本（有通关层优先 sweep；否则 start/end） |
| `python3 main.py dungeon tower [-l N] [-c N]` | 自动识别当天 playList 中 key 6-12（优先职业试炼）并推进；`-l` 指定关卡，`-c` 限制次数 |
| `python3 main.py slzt` | 从失落之塔当前进度的下一层持续推进，`Ctrl+C` 停止 |
| `python3 main.py slzt -l 4 -t 2` | 连续清指定层；`-l` 为层数，`-t` 为次数 |
| `python3 main.py gacha 1` / `gacha 2` | **抽卡**：复刻系列任务的生成动作，`1` 技能池(shop key 12)，`2` 成员池(shop key 112)；`-c N` = N 次 30 连（每次耗 30 票，默认 1 次） |

无参数时打印 help 与示例。需要 `ts` 时请安装依赖：`pip install -r requirements.txt`（含 `textual`）。

`zb` 开始后从 `/api/goods/list` 读取 `ItemTicket`（goods type 50）的当前
`_value`，默认将它固定为本次尚需开启的总数量。单行动态进度条显示本次已经
成功开启的数量；显式指定 `--total` 或 `--batches` 时才覆盖默认总数。
登录、HTTP、批次日志及结果文件提示静默，详细结果仍保存到 `last_zb.json`。
`zb --info` 保持原有的炉子信息输出。
正常未命中筛选的批次只发送 `spawn-and-sell`；`item/list` 仅在启动清理、
筛选命中或服务端返回 `-35004` 需要恢复时调用。

### 自动推进轮换职业试炼 dungeon tower

```bash
python3 main.py dungeon tower              # 从今日副本当前进度一直推进，Ctrl+C 停止
python3 main.py dungeon tower -c 10        # 从今日副本当前进度打 10 次
python3 main.py dungeon tower -l 3         # 指定第 3 关，默认只打 1 次
python3 main.py dungeon tower -l 100 -c 10 # 指定第 100 关打 10 次
```

启动后从 `/api/dungeon/list._playList` 中动态选择当天开放的 key 6-12：
优先职业试炼 key 6/7/8（`_stage=5/6/7`）；没有试炼时再回退到 9/10/11/12
（失落之塔 / 竞技场 / 防火墙）。随后读取所选 key 自己的
`_level`（历史最高已通关关卡）和 `_challengeLevel`（本轮重置后的当前进度），
未指定 `-l` 时从 `_level+1` 开始推进，各 key 进度不会混用。
`-l N` 指定已解锁关卡：下一未通关关卡走正常战斗，已通关关卡走 sweep；
只指定 `-l` 默认执行 1 次，配合 `-c` 才重复执行。

正常推进从 `_level+1` 开始逐关推进。职业试炼实机协议使用 `region=10000`、
`stage=5|6|7`、`sector=1`、`attr=3`；key 9 用失落之塔 sector 轮换，key 11/12 用 stage 9/10。
当前关卡放在 `repeat`；每关依次调用
`dungeon/start`、共享的 `battle/kill-mob`（固定 `wave=0`）和
`dungeon/end`。终端每关只显示关卡与合并后的奖励。
1.2.0 实测第 100 关为有效上限（服务端不存在第 101 关的
`dungeonTrialInfoLevelMap`），因此通关 100 后固定重复第 100 关，不再请求
第 101 关。封顶前使用 `start/kill-mob/end` 推进，封顶后切换到
`dungeon/sweep` 重复领取第 100 关奖励；当 `_challengeLevel` 已到顶时，先用
`dungeon/trial-reset` 重置再 sweep（重置道具不足会停止并显示服务端错误）。
`-c N` 指定本次成功次数；不指定时持续刷到 `Ctrl+C` 或资源不足。详细结果
写入 `last_dungeon.json`。

### 失落之塔 slzt

```bash
python3 main.py slzt
python3 main.py slzt -l 4 -t 2
```

不带参数时读取失落之塔 key 9 的最高通关层，从下一层开始持续推进，
直到按 `Ctrl+C`。`-l x` 指定固定层，`-t x` 指定挑战次数；只带 `-l`
时默认打 1 次，只带 `-t` 时从当前进度向前推进指定次数。多次挑战复用
同一次登录会话，任意一次失败后停止。

slzt 开打后，终端只显示当前层数和当前次数；指定 `-t` 时使用单行动态
进度条，并显示 `当前次数/总次数`。掉落、HTTP、登录、状态码、汇总及结果文件提示均静默，
详细结算仍保存在 `last_slzt.json`。1.1.1 实测协议固定使用
`region=100000`、`stage=1`、`attr=3`；每 10 层只轮换 `sector`：
`sector=((层数-1)%10)+1`，`repeat` 保留完整层数。
例如第 11 层为 `stage=1, sector=1, repeat=11`。流程为
`dungeon/start` → 按 start 返回的 mob UID
调用共享的 `battle/kill-mob` → `dungeon/end`；不会把持续中的主战斗请求或
每日灵魂奖励请求并入该流程。

### 抽卡 gacha

```bash
python3 main.py gacha 1        # 技能池，1 次 30 连（消耗 30 张技能票）
python3 main.py gacha 2        # 成员池，1 次 30 连（消耗 30 张成员票）
python3 main.py gacha 2 -c 3   # 成员池，3 次 30 连（共 90 票）
```

复刻 `auto` 系列任务里的两个生成动作（`series_quest_care.SPAWN_SHOP`）：

- `gacha 1` = Spawn:Skill → `POST /api/shop/spawn {_key:12}`（技能票 goods 51）
- `gacha 2` = Spawn:Member → `POST /api/shop/spawn {_key:112}`（成员票 goods 52）

`-c N` 表示连续 N 次 30 连：每次调用一条 spawn 请求、耗 30 票出 35 个，
多次之间自动续心跳。启动时先从 `/api/goods/list` 读对应票余量，不足 N 次
时按余量截断并在汇总里标 `stop_reason`。详细结果写入 `last_gacha.json`。

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
python3 main.py runloop --noboss
```

登录后 TUI + 无限 stay 刷当前登录进度关卡。`Ctrl+C` 停止。掉落统计写 `drop_stats.json`，摘要写 `last_run.json`。

`--noboss`：只打小怪波次（跳过最后一波 boss），`battle/end` 以失败结算，**不推进关卡**；随后 **重开同一关** 继续循环。注意：服务端在非通关结算下通常不给通关掉落。

### 定时维护 auto

```bash
python3 main.py auto
```

单次流程（外部 crontab 每小时调度，不在进程内长睡）：

1. 登录
2. 肉田维护（有广告次数先领种子/浇水器，再浇水收获补种）
3. 公会：未出席则出席；训练场/道场奖励未领取则领取；读取实时门票 `107/108`，分别按剩余次数执行 `camp-sweep`（请求只传 `_key`）
4. 副本：先按 `_adCount` 领广告票；普通副本用门票连续挑战 `_level+1`，每次成功后继续下一关。下一关在 `dungeon/start` 阶段被拒绝时直接停止，不再回退扫荡当前最高层。**10000/10020（key1/2）仍只领广告、不战斗；Firewall 保持特殊规则**
5. 系列任务：检查 Guide 循环链 `9000–9014`，自动执行装备生成、无 Boss 小怪计数、技能/成员生成，以及 Lamp/Lava 两个副本任务；已完成的任务会调用 `quest/complete` 并继续下一项。区域通关任务只在累计值已经满足时领取，不主动推进主线。遇 Lamp/Lava 门票为 0 时记录 `dungeon_no_attempts` 并跳过本阶段，后续 `auto` 继续运行
6. 训练 / Lab：有完成项则领取 → 重开同一训练 → 请求大家帮助
7. 探查数码世界 / Mine：耗尽体力捡特训芯片，可冲锋/钻头，尝试里程奖励
8. 异次元 box：有红点必处理（可领/单次挂满/被干→召回重上）→ 有额度就挂满（自己1+搜索他人）→ 攻击
9. 炉子维护（投 bit / 建造，不开放装备）
10. 竞技场 PVP：常规(356) + 赛季(357) 两种票都打完；各列表选战力最低 → battle
11. 亲密点触：遍历 **所有伙伴**（各自独立冷却）；就绪的先 `change-character` 再 `relation-exp`；全冷却则跳过，不长等
12. AFK 领取

结果追加到 `logs/auto.log`。遇会话踢出 `-19006` 会等待后重登并再跑完一轮。



### 竞技场 pvp

```bash
python3 main.py pvp                 # 打完常规票(356) + 赛季票(357)
```

逻辑（`pvp` / `auto` 相同）：

1. **常规竞技场** goods `356`：`/api/arena/matching` → 战力最低 → `/api/arena/battle`
2. **赛季竞技场** goods `357`：`/api/arena-season/matching`（`_isRefresh:false`）→ 战力最低 → `/api/arena-season/battle`
3. `_stage` 从 `dungeon_key_stage.json` 按 costType 解析（`PVPTicket` / `PVPTicket_Season` → stageKey），不写死
4. 上报固定 **`_isWin=false`（认输）**，常规/赛季一致

两种票都耗尽才结束。

### 开装备 zb

```bash
python3 main.py zb              # 开完全部 ItemTicket；默认带客户端筛选参数
python3 main.py zb --batches 5  # 连续 5 批
python3 main.py zb --info       # 只查炉子快照 / 升级所需 bit（不操作）
python3 main.py zb --filter-grade 10 --filter-match 2 --filter-stat 10,20,13
python3 main.py zb --filter-grade 0 --filter-match 0 --filter-stat ""   # 关闭筛选
python3 main.py zb -j 2            # 可选：2 线程波次并发
```

开装备走 `POST /api/item/spawn-and-sell`。默认筛选对齐实机客户端（2026-08-11 / 1.2.4）：

- `_filterGrade=10`
- `_filterMatchCount=2`
- `_filterStatTypeList=[10,20,13]`（暴击率 / 眩晕率 / 技能暴击率）
- 默认串行；可选 `-j 2` 波次并发 `spawn-and-sell`（换装/出售仍串行）

炉子维护（查 info / 投 bit / 满了建造 / 建造完成）在 `auto` 里由 `run_item_spawner_care` 自动跑，不进 `zb`。
设备日志对照见 `iosver/SOP_DEVICE_LOGS.md`。
