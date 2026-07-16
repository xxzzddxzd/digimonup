# DIGIMON_UP 1.0.2 PlayCover 完整包

本发布包用于 Apple Silicon Mac，包含原始游戏安装包和 PlayCover/macOS 插件安装工具。

## 内容

```text
DIGIMON_UP-1.0.2-PlayCover/
├── 1.0.2.ipa
├── Mac插件/
│   ├── PCMacProbe.dylib
│   ├── pc_macho_inject
│   ├── 安装插件.command
│   ├── 安装说明.md
│   └── THIRD_PARTY_NOTICES.md
├── README.md
└── SHA256SUMS.txt
```

## 使用方法

1. 安装并打开 PlayCover。
2. 双击 `1.0.2.ipa`，或将它拖入 PlayCover 完成安装。
3. 确认 `DIGIMON_UP` 已出现在 PlayCover 资料库，然后退出游戏。
4. 打开 `Mac插件` 文件夹，右键点击 `安装插件.command`，选择“打开”。
5. 终端显示安装完成后，从 PlayCover 启动游戏。

进入游戏后，点击可拖动的“加速”悬浮按钮，可设置 1x 至 10x 整数倍速。

不需要越狱、关闭 SIP、Xcode、Python 或 Theos。详细兼容范围、Gatekeeper 处理、
卸载和故障排查请阅读 `Mac插件/安装说明.md`。

本插件只支持 `DIGIMON_UP 1.0.2 (38)`，请勿注入其他游戏版本。
