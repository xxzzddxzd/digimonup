# autorun

DIGIMON UP 自动推图。

## 安装

```bash
cd autorun
python3 -m pip install -r requirements.txt
```

## 导入账号

用 Charles 导出的抓包（`.chlsj` / 含 `/api/account/auth` 的 JSON）：

```bash
python3 main.py --input 你的抓包.chlsj
```

会生成本地 `account.json`（不进 git）。之后启动自动读取。

## 自动推图

```bash
python3 main.py
```

默认：TUI + 无限刷当前可打关卡（login 后的 frontier）。  
`Ctrl+C` 停止。

掉落统计写入 `drop_stats.json`。
