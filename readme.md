# DIGIMON UP 插件工程

本工程用于调试和扩展 DIGIMON UP 1.0.2 的 Unity/IL2CPP 客户端，包含 iOS 越狱设备版和 PlayCover/macOS 版插件。当前目标 Bundle ID 为 `jp.co.bandainamcoent.BNEI0442`，代码中的 UnityFramework 偏移对应游戏 1.0.2。

## 目录

- `iosver`：iOS arm64 tweak。保留广告卡生效、自动打开间隔 0.5 秒，以及越狱检测、退出和 Unity 日志诊断。使用 `deploy.sh` 编译后通过本机 `2224` 端口部署到越狱设备。
- `macver`：PlayCover/macOS 插件。使用 Dobby 作为 hook 后端，提供 1x 至 10x 游戏速度控制及安装、注入和日志检查脚本。

运行日志和本地编译产物不纳入版本控制。插件日志统一以 `#pc  ` 开头，便于过滤。
