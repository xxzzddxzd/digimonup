# autorun

DIGIMON UP 自动推图。

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

若执行中被手机顶号（`-19006`）：等待 10 分钟 → 全量重登 → 继续完成 `qmd`+`afk`。


## 领取 AFK 奖励

```bash
python3 main.py afk
```

登录后请求 `/api/afk/reward-list` → `/api/afk/reward`，并尝试 `/api/afk/ad-view`。

## 亲密点触 / 喂养

```bash
python3 main.py qmd
```

流程：

1. `collect-list` 读 `nextRelationExpTime`，未到则等待
2. `relation-exp`（空 body）加亲密度经验
3. 成功后冷却约 **20 分钟**（1200s），期间再点返回 `-24016`
4. 有可领奖励时用 `relation-reward`，`_key` = 伙伴 baseKey

## 亲密点触自动循环

```bash
python3 main.py qmdauto
```

流程（循环）：

1. 登录，读 `nextRelationExpTime`
2. 未到点则下线并 sleep 到点（按服务器时间，不靠固定 21 分钟）
3. 重新登录，维护肉田（可收则收、空地种植）+ 执行 `qmd` + `afk`
4. 再读下次时间，继续 sleep

本流程**不开心跳**；仅在业务请求返回 `-19006` 时重登恢复。

结果写入 `logs/qmdauto.log`（日期时间 + 当次结果）。

`Ctrl+C` 停止。

若执行中被手机顶号（`-19006`）：等待 10 分钟 → 全量重登 → 继续完成 `qmd`+`afk`。


