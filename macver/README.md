# DIGIMON_UP PlayCover/macOS plugin

This directory ports `../iosver/PCJBProbe.mm` to a PlayCover-compatible
embedded dylib. The hook backend is Dobby instead of MobileSubstrate. The
plugin is still compiled for the iOS arm64 ABI first, then its build-version
load command is converted to Mac Catalyst so that it can load in PlayCover.

The 1.0.2 Unity offsets are retained because `../102/UnityFramework` and the
framework inside `../102/1.0.2.ipa` have the same SHA-256 digest.

The vendored Dobby attribution and hashes are recorded in
`THIRD_PARTY_NOTICES.md`.

## In-game controls

The floating **加速** button (remembers last edge/Y; auto-collapses after ~2.5s idle) opens the plugin panel. The only user-facing
setting is an integer game-speed slider from 1x through 10x; the selected value
is persisted across launches. All other plugin behavior remains enabled by
default and has no panel switch.

Completed main-scene guide quests are claimed automatically. This uses the
same `UIGuideQuestInfo.SetData` / `PS_QuestComplete.Request` path as the iOS
plugin, is enabled by default, and suppresses duplicate requests for 10 seconds.

Notice, login-bonus, AFK, and time-deal startup popups are skipped after login;
their normal manual entry points remain available. The macOS plugin does not
force Guest login, so PlayCover users retain control of account selection.

Reward popups close two seconds after their completion animation. Better
hologram equipment produced by the item-spawner flow is equipped automatically,
without affecting ordinary item-selection screens. Partner-care events are
triggered automatically after their normal cooldown and visibility checks.

Firewall dungeons bypass ranking preloads after the normal ticket/content
checks and transition directly into battle. Every rendered mine cell is
selectable; farther moves reload the authoritative server-side mine view.
Growth guides opened by an ordinary battle defeat close after one second.

For game version 1.0.2, speed control hooks the confirmed IL2CPP wrapper for
`UnityEngine.Time.set_timeScale(float)` at UnityFramework offset `0x06A4C1E0`.

## Build

```sh
make -C macver embedded-mac-dylib
```

## Install and inject

PlayCover must already be installed. The default target is
`jp.co.bandainamcoent.BNEI0442`.

```sh
macver/install_and_inject.sh
```

For an app that is already installed through PlayCover:

```sh
macver/inject_installed.sh
```

The injection script waits for PlayChain account writeback, creates a
timestamped backup beside the installed app (including the `.keyCover` database
and game preferences), preserves the PlayCover executable entitlements, adds
`@executable_path/Frameworks/PCMacProbe.dylib`, and ad-hoc signs the modified
bundle.

## End-user package

For a user who has only PlayCover and `1.0.2.ipa`, build a source-free package:

```sh
macver/package_release.sh
```

The resulting zip under `macver/dist` contains the compiled dylib, a native
arm64 Mach-O injector, a double-clickable installer, and Chinese instructions.
The end user does not need Theos, Xcode, Python, or disabled SIP. See
[`end_user/安装说明.md`](end_user/安装说明.md).

To bundle `102/1.0.2.ipa` and all required macOS installer files into one
complete release archive:

```sh
macver/package_complete_release.sh
```

## Runtime verification

Start the game from the PlayCover library, then run:

```sh
macver/check_logs.sh
```

Do not open the `.app` inside PlayCover's container directly. Direct launch
bypasses PlayChain account-data unlock/writeback.

Logs are written under `Library/Caches/PCMacProbe` in the app container.

## Portrait window

If PlayCover opens the game in a landscape window, apply the tested portrait
profile (custom 900 x 1600 at the existing 2x scale):

```sh
macver/set_portrait.sh
```

This sets PlayCover's `displayRotation` to portrait and creates a timestamped
backup of the previous app settings before relaunching the game.
