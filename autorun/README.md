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

## 定时维护 auto

```bash
python3 main.py auto
```

单次执行后退出（由 crontab 每小时触发）：

1. 登录
2. 肉田维护（浇水/收获/种植）
3. 异次元 box（领取 / 挂公开箱 / 攻击）
4. 亲密点触（冷却中则跳过，不等待）
5. AFK 领取

结果：`logs/auto.log`、运行日志 `logs/auto_run.log`。

```bash
# crontab 示例（每小时）
0 * * * * /Users/xuzhengda/Documents/workspace/smbb/autorun/run_auto.sh
```


