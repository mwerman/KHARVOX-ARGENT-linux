# KHARVOX: ARGENT (DOOM Eternal VR) on Linux

Runs [CactusVRStudios/KHARVOX-ARGENT](https://github.com/CactusVRStudios/KHARVOX-ARGENT) on Linux through
Proton, started from Steam, and streamed to a Steam Frame (or any SteamVR headset) from the PC.
The approach follows [Monkellie/tf2vr-linux](https://github.com/Monkellie/tf2vr-linux): the official
Windows release is downloaded and verified, installed next to the game, and launched under Proton.

This repo contains no game or mod files. ARGENT is a Windows x64 mod (MSVC, MASM, MinHook) that hooks the
Windows build of DOOM Eternal, so nothing is recompiled for Linux: the Windows build runs inside the
game's own Proton prefix.

## Requirements

- An **x86-64 Linux PC** with a GPU that runs DOOM Eternal. The Steam Frame itself is ARM and cannot run this;
  the game runs on the PC and is streamed.
- DOOM Eternal **on Steam**, started once normally so Proton creates its prefix.
- **Proton Experimental** forced for DOOM Eternal (Properties -> Compatibility).
- **SteamVR** as the active OpenXR runtime (SteamVR -> Settings -> OpenXR -> Set SteamVR as OpenXR runtime).
- `python3` (Arch/CachyOS: `sudo pacman -S python`). Nothing else: download, SHA-256 check and unzip use Python's standard library.

## Install

```
git clone https://github.com/<you>/kharvox-argent-linux.git   # or unpack the release tarball
cd kharvox-argent-linux
./install.sh --check      # optional: checks Steam, DOOM Eternal, Proton tool, OpenXR runtime; changes nothing
./install.sh
```

The installer:

1. finds Steam and the library DOOM Eternal is in (any library, any drive);
2. downloads the latest upstream release zip and checks it against the SHA-256 GitHub publishes for the asset;
3. checks that `ArgentLauncher.exe` is a 64-bit Windows executable and that the files it needs are beside it;
4. installs ARGENT to `~/.local/share/kharvox-argent/ARGENT` (never into the game folder, as upstream requires);
5. installs the `argent-launch` wrapper to `~/.local/bin/argent-launch`;
6. prints the exact Launch Options line to paste into Steam.

Options: `--tag vX` (specific upstream release), `--zip FILE` (offline install), `--force` (wipe the installed
ARGENT folder first), `--uninstall`. `STEAM_DIR=...` and `ARGENT_DIR=...` override the detected locations.
Setting `GITHUB_TOKEN` avoids GitHub API rate limits.

## Add to Steam

DOOM Eternal -> Properties -> General -> Launch Options:

```
/home/<you>/.local/bin/argent-launch %command%
```

(The installer prints the exact line for your system.) Steam expands `%command%` to the full Proton command for
`DOOMEternalx64vk.exe`; the wrapper swaps that one executable for `ArgentLauncher.exe`. The launcher therefore runs in the
same prefix, container and Steam app context (appid 782330) as the normal game, which is what Steam's DRM and Proton's
OpenXR bridge expect. Remove the launch option to play flat DOOM Eternal again.

## Play on the Steam Frame

1. On the PC: start SteamVR and connect the Frame (Steam Link PC VR streaming).
2. Press **Play** on DOOM Eternal in Steam, from the PC or from the Frame's Steam library.
3. `ArgentLauncher` opens **on the PC desktop**. First run: select
   `Z:\...\DOOMEternalx64vk.exe` (the installer prints the exact path; Wine's `Z:` is Linux `/`),
   choose your VR options, press **PLAY**. Keep the game window focused for input (upstream requirement).
4. SteamVR streaming to the Frame shows the game. Under SteamVR, set resolution in SteamVR, not in the launcher
   (upstream: the SteamVR path ignores the launcher's FSR resolution reduction).

## Updating and uninstalling

```
git pull && ./install.sh     # installs the newest upstream release over the old one; launcher settings are kept
./install.sh --uninstall     # removes ARGENT, the wrapper and its config; never touches the game
```

Then clear the Launch Options in Steam.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Game starts flat | The launch option is missing or wrong. Re-paste the line the installer printed. |
| `argent-launch: no DOOMEternal*.exe in the command Steam passed` | Steam's command did not contain the game exe. See `~/.local/state/kharvox-argent/launch.log`. |
| Launcher window never appears | Run Steam from a terminal and read the output. Add `PROTON_LOG=1` before the wrapper in Launch Options (`PROTON_LOG=1 /path/argent-launch %command%`); the log lands at `~/steam-782330.log`. |
| Crash in `MSVCP140.dll` | Install the VC++ runtime into the prefix: `protontricks 782330 vcrun2022` (the same fix tf2vr-linux documents). |
| Headset shows nothing / OpenXR errors | Check SteamVR is the active OpenXR runtime and running. Other runtimes (Monado, WiVRn) need their socket exposed to Steam's container, e.g. `PRESSURE_VESSEL_FILESYSTEMS_RW=$XDG_RUNTIME_DIR/monado_comp_ipc`. |
| `install.sh` cannot find Steam | `STEAM_DIR=/path/to/Steam ./install.sh` |
| Installed outside `$HOME` and game cannot see it | Add the folder to `STEAM_COMPAT_MOUNTS` in the Launch Options. |

ARGENT's own log: enable **Extended Logging** under Rendering in the launcher before pressing PLAY.

## What is and is not verified

Verified by the tests in this repo (`tests/`, run by CI): Steam/library/appmanifest/compat-tool detection, zip handling
(including zips with backslash paths and zip-slip attempts), the 64-bit PE check, SHA-256 comparison, install,
update, uninstall, and the wrapper's argument rewriting.

**Not verified:** ARGENT's launcher and Vulkan/OpenXR layer under Proton. Upstream states it was tested with VDXR,
SteamVR and Meta runtimes on Windows. This is expected to work the way tf2vr-linux does, but it is experimental.
Fixes tf2vr-linux needs that are **not** ported, because they are specific to the Titanfall 2 mod: the `mmdevapi.dll`
audio-loopback patch, forcing a newer VC++ runtime (ARGENT's release bundles the CRT per its packaging script),
and setting `SteamGameId` by hand (starting from Steam's own Play button already provides it).
If you hit one of those symptoms, open an issue with the logs.

## Credits and license

[CactusVRStudios](https://github.com/CactusVRStudios/KHARVOX-ARGENT) for ARGENT (MIT);
[Monkellie/tf2vr-linux](https://github.com/Monkellie/tf2vr-linux) for the method. Not affiliated with id Software,
Bethesda, Valve or the ARGENT authors. MIT licensed, see `LICENSE`. A legally acquired copy of DOOM Eternal is required.
