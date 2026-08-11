# SOP：从设备拉 PCJBProbe 日志

目标：快速拿到明文请求（`#pc Crypto.REQ plain=...`），对照协议参数（尤其 `zb` / spawn-and-sell）。

## 前置

- 越狱设备已通过 USB / `iproxy` 映射到本机 **2224**
  ```bash
  nc -z 127.0.0.1 2224 && echo ok
  ```
- 已部署 `iosver/PCJBProbe.dylib`：
  ```bash
  cd iosver && ./deploy.sh
  ```
- 至少启动过一次 `DIGIMONUP`，才会生成：
  - `.../Library/Caches/PCJBProbe/PCJBProbe-current.log`
  - `session-crypto.json`

可选环境变量：

```bash
export PCJB_DEVICE=root@127.0.0.1
export PCJB_PORT=2224
```

## 标准流程（只用脚本，不要手写 find/scp）

```bash
cd iosver

# 1) 日常首选：只拉当前日志
./pull_logs.sh --latest logs/scratch

# 2) 拉当前日志并直接搜关键词
./pull_logs.sh --grep filterGrade logs/zb-filter
./pull_logs.sh --grep filterStatTypeList logs/zb-filter

# 3) 完整拉取（current + previous + crash + 最近 ips）
./pull_logs.sh logs/full-$(date +%Y%m%d-%H%M%S)
```

路径缓存：首次 `find` 稍慢，结果写入 `iosver/.pcjb_log_dir`（已 gitignore）。  
换机 / 重装 App / 缓存失效：

```bash
./pull_logs.sh --refresh-path
./pull_logs.sh --latest
```

## 看开装备参数（spawn-and-sell）

```bash
./pull_logs.sh --grep filterGrade logs/zb-check
# 或本地
rg -n 'filterGrade|filterStatTypeList|filterMatchCount' iosver/logs/zb-check/PCJBProbe-current.log | tail
```

实机 1.2.4 捕获（2026-08-11）：

```json
{
  "_count": 10,
  "_filterGrade": 10,
  "_filterMatchCount": 2,
  "_filterStatTypeList": [10, 20, 13]
}
```

| 字段 | 含义 |
| --- | --- |
| `_filterGrade` | 品质门槛（10） |
| `_filterMatchCount` | 需命中词条数（2） |
| `_filterStatTypeList` | `E_STAT`：10=CriticalRate，20=StunRate，13=SkillCriticalRate |

autorun `zb` 默认已对齐上表，见 `autorun/client/item_spawner_care.py`：

- `DEFAULT_FILTER_GRADE = 10`
- `DEFAULT_FILTER_MATCH_COUNT = 2`
- `DEFAULT_FILTER_STAT_TYPE_LIST = [10, 20, 13]`
- 默认串行；可选 `-j 2` 波次并发

覆盖示例：

```bash
cd autorun
python3 main.py zb
python3 main.py zb -j 2
python3 main.py zb --filter-grade 0 --filter-match 0 --filter-stat ""
```

## 不要这样做

- 不要每次手写 `find /var/mobile/Containers/...`（慢，易和 scp 叠车）
- 不要一次 scp 目录里全部历史大文件；用 `--latest` / 脚本白名单
- 设备无响应时先确认 `2224`，再 pull；卡住就 Ctrl+C 后 `--latest` 重试

## 相关命令

| 动作 | 命令 |
| --- | --- |
| 部署插件 | `cd iosver && ./deploy.sh` |
| 拉日志 | `cd iosver && ./pull_logs.sh --latest` |
| 搜筛选参数 | `./pull_logs.sh --grep filterGrade` |
| 开装备 | `cd autorun && python3 main.py zb` |

明文来源：`PCJBProbe` hook `PacketManager.GetEncryptData` → 日志行 `Crypto.REQ plain=`。
