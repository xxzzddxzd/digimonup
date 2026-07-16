# DIGIMON UP 插件工程

本工程用于调试和扩展 DIGIMON UP 1.0.2 的 Unity/IL2CPP 客户端，包含 iOS 越狱设备版和 PlayCover/macOS 版插件。当前目标 Bundle ID 为 `jp.co.bandainamcoent.BNEI0442`，代码中的 UnityFramework 偏移对应游戏 1.0.2。

## 目录

- `iosver`：iOS arm64 tweak。保留广告卡生效、自动打开间隔 0.5 秒，以及越狱检测、退出和 Unity 日志诊断。使用 `deploy.sh` 编译后通过本机 `2224` 端口部署到越狱设备。
- `macver`：PlayCover/macOS 插件。使用 Dobby 作为 hook 后端，提供 1x 至 10x 游戏速度控制及安装、注入和日志检查脚本。

## Mac 使用方法

适用环境：Apple Silicon Mac、安装在 `/Applications` 中的 PlayCover，以及
`DIGIMON_UP 1.0.2 (38)` 的 `1.0.2.ipa`。不需要越狱、关闭 SIP、Xcode、Python 或
Theos。

> 单独把 `PCMacProbe.dylib` 拖入 PlayCover 不会生效。游戏主程序需要加入 dylib
> 加载命令并重新签名，请使用发布包中的安装脚本。

1. 从 [v1.0.2 Release](https://github.com/xxzzddxzd/digimonup/releases/tag/v1.0.2)
   下载并解压 `DIGIMON_UP-1.0.2-PlayCover.zip`。
2. 打开 PlayCover，双击 `1.0.2.ipa` 或将它拖入 PlayCover 完成安装。
3. 确认 `DIGIMON_UP` 已出现在 PlayCover 资料库，然后退出正在运行的游戏。
4. 右键点击插件包内的 `安装插件.command`，选择“打开”并确认运行。
5. 终端显示“安装完成”后，从 PlayCover 启动游戏。

进入游戏后，点击屏幕边缘可拖动的“加速”按钮打开插件面板。滑块可设置 1x 至
10x 的整数倍速，选择值会自动保存，其他插件功能默认启用。

安装脚本会在修改前创建带时间戳的备份，验证游戏版本，注入
`@executable_path/Frameworks/PCMacProbe.dylib`，完成本机临时签名并检查安装结果。
脚本可以重复运行；PlayCover 更新或重新安装游戏后，需要再次运行脚本。

如果 macOS 阻止脚本，不要全局关闭 Gatekeeper。在终端进入解压后的插件目录并执行：

```sh
xattr -dr com.apple.quarantine .
chmod +x 安装插件.command pc_macho_inject
./安装插件.command
```

完整的兼容范围、卸载方法和故障处理见
[`macver/end_user/安装说明.md`](macver/end_user/安装说明.md)。

运行日志和本地编译产物不纳入版本控制。插件日志统一以 `#pc  ` 开头，便于过滤。
