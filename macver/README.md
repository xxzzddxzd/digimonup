# DIGIMON_UP PlayCover/macOS plugin

This directory ports `../iosver/PCJBProbe.mm` to a PlayCover-compatible
embedded dylib. The hook backend is Dobby instead of MobileSubstrate. The
plugin is still compiled for the iOS arm64 ABI first, then its build-version
load command is converted to Mac Catalyst so that it can load in PlayCover.

The 1.0.2 Unity offsets are retained because `../102/UnityFramework` and the
framework inside `../102/1.ipa` have the same SHA-256 digest.

The vendored Dobby attribution and hashes are recorded in
`THIRD_PARTY_NOTICES.md`.

## In-game controls

The floating **加速** button opens the plugin panel. The only user-facing
setting is an integer game-speed slider from 1x through 10x; the selected value
is persisted across launches. All other plugin behavior remains enabled by
default and has no panel switch.

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

The injection script creates a timestamped backup beside the installed app,
adds `@executable_path/Frameworks/PCMacProbe.dylib` to the main executable,
and ad-hoc signs the modified bundle.

## Runtime verification

```sh
open "$HOME/Library/Containers/io.playcover.PlayCover/Applications/jp.co.bandainamcoent.BNEI0442.app"
macver/check_logs.sh
```

Logs are written under `Library/Caches/PCMacProbe` in the app container.

## Portrait window

If PlayCover opens the game in a landscape window, apply the tested portrait
profile (custom 900 x 1600 at the existing 2x scale):

```sh
macver/set_portrait.sh
```

This sets PlayCover's `displayRotation` to portrait and creates a timestamped
backup of the previous app settings before relaunching the game.
