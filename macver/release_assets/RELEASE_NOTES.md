## DIGIMON UP 1.0.2 — PlayCover/macOS 完整包

适用于 Apple Silicon Mac 和 PlayCover，游戏版本必须为 `1.0.2 (38)`。

### 包含内容

- `1.0.2.ipa`
- `PCMacProbe.dylib`
- arm64 原生 Mach-O 注入器
- 双击安装脚本和中文安装说明
- 第三方组件声明及 SHA-256 校验清单

### 使用方法

1. 解压 `DIGIMON_UP-1.0.2-PlayCover.zip`。
2. 使用 PlayCover 安装包内的 `1.0.2.ipa`。
3. 退出游戏，进入 `Mac插件` 文件夹。
4. 右键打开 `安装插件.command`。
5. 安装完成后从 PlayCover 启动游戏。

游戏内点击可拖动的“加速”按钮，可以设置 1x 至 10x 整数倍速。已完成的主界面任务会自动领取，默认开启，不设开关。
标题界面保留正常账号选择；进入主界面后会跳过 Notice、登录奖励、AFK 和限时礼包启动弹窗。
奖励弹窗会自动关闭，更优的生成器全息装备会自动装备，伙伴关怀事件会自动触发。
防火墙副本会跳过排行榜预加载；矿区可直接移动到当前视图格子；战败成长引导会自动关闭。
安装器现在会等待 PlayChain 账号库回写，备份 `.keyCover` 与游戏偏好，并保留 PlayCover
原始 entitlement 后再重新签名。

不需要越狱、关闭 SIP、Xcode、Python 或 Theos。

### SHA-256

`DIGIMON_UP-1.0.2-PlayCover.zip`

```text
f55599fa2527066eaa99a839a7ec02372bb2b82b1f6f63361a46097f6bcddfca
```
