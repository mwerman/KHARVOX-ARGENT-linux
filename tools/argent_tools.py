#!/usr/bin/env python3
"""Helper for running KHARVOX: ARGENT (DOOM Eternal VR) on Linux through Proton.

Subcommands
  install   check the setup, download the official ARGENT release, install it together
            with the Steam launch wrapper (this is what ./install.sh runs)
  fetch     download + verify + extract the official release into a folder (used by CI)

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

UPSTREAM_REPO = "CactusVRStudios/KHARVOX-ARGENT"
GAME_APPID = "782330"  # DOOM Eternal on Steam
GAME_EXE = "DOOMEternalx64vk.exe"
LAUNCHER_EXE = "ArgentLauncher.exe"
EXPECTED_FILES = ("ArgentLayer.dll", "ArgentLayer.json", "openxr_loader.dll")
MARKER = ".kharvox-argent-linux"
MAX_UNZIPPED = 4 * 1024**3
USER_AGENT = "kharvox-argent-linux"

REPO_ROOT = Path(__file__).resolve().parent.parent


class ToolError(Exception):
    """A problem the user can act on; printed without a traceback."""


def info(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- paths


def data_root() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "kharvox-argent"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kharvox-argent"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "kharvox-argent"


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


# --------------------------------------------------------------------------- VDF (Steam text config)

_VDF_TOKEN = re.compile(r'\s+|//[^\n]*|"((?:[^"\\]|\\.)*)"|([{}])', re.S)
_VDF_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _vdf_unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _VDF_ESCAPES.get(m.group(1), "\\" + m.group(1)), text, flags=re.S)


def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format into nested dicts of strings."""
    root: dict = {}
    stack = [root]
    key = None
    pos = 0
    while pos < len(text):
        m = _VDF_TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"VDF syntax error at offset {pos}")
        pos = m.end()
        if m.group(1) is not None:
            s = _vdf_unescape(m.group(1))
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
        elif m.group(2) == "{":
            if key is None:
                raise ValueError("VDF: '{' without a key")
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif m.group(2) == "}":
            if key is not None or len(stack) == 1:
                raise ValueError("VDF: unbalanced '}'")
            stack.pop()
    if key is not None or len(stack) != 1:
        raise ValueError("VDF: unexpected end of file")
    return root


def vget(data, *path):
    """Case-insensitive nested lookup; None when anything is missing."""
    cur = data
    for wanted in path:
        if not isinstance(cur, dict):
            return None
        for k, v in cur.items():
            if k.lower() == wanted.lower():
                cur = v
                break
        else:
            return None
    return cur


def read_vdf(path: Path):
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- Steam discovery


def steam_candidates():
    explicit = os.environ.get("STEAM_DIR")
    if explicit:
        yield Path(explicit).expanduser()
        return
    home = Path.home()
    xdg = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    yield xdg / "Steam"
    yield home / ".steam" / "steam"
    yield home / ".steam" / "root"
    yield home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"


def find_steam() -> Path:
    for cand in steam_candidates():
        if (cand / "steamapps").is_dir():
            return cand.resolve()
    if os.environ.get("STEAM_DIR"):
        raise ToolError(f"STEAM_DIR={os.environ['STEAM_DIR']} has no steamapps folder")
    raise ToolError("Steam not found; point the installer at it with STEAM_DIR=/path/to/Steam ./install.sh")


def steam_libraries(steam: Path) -> list[Path]:
    libs = [steam]
    data = read_vdf(steam / "steamapps" / "libraryfolders.vdf")
    folders = vget(data, "libraryfolders") if data else None
    if not isinstance(folders, dict):
        return libs
    for key, value in folders.items():
        if isinstance(value, dict):
            path = value.get("path")
        elif key.isdigit():  # old format: "1" "/path"
            path = value
        else:
            path = None
        if not path:
            continue
        lib = Path(path)
        if (lib / "steamapps").is_dir() and lib.resolve() not in [p.resolve() for p in libs]:
            libs.append(lib)
    return libs


def find_game(steam: Path):
    """Return (library, game_dir) for DOOM Eternal, or None when it is not installed."""
    for lib in steam_libraries(steam):
        manifest = lib / "steamapps" / f"appmanifest_{GAME_APPID}.acf"
        if manifest.is_file():
            data = read_vdf(manifest)
            installdir = vget(data, "AppState", "installdir") if data else None
            return lib, lib / "steamapps" / "common" / (installdir or "DOOMEternal")
    return None


def find_file_ci(folder: Path, name: str):
    """Case-insensitive file lookup in one folder (Windows files on a case-sensitive fs)."""
    try:
        for entry in folder.iterdir():
            if entry.name.lower() == name.lower():
                return entry
    except OSError:
        pass
    return None


def compat_tool(steam: Path):
    data = read_vdf(steam / "config" / "config.vdf")
    mapping = vget(data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping") if data else None
    if not isinstance(mapping, dict):
        return None
    for key in (GAME_APPID, "0"):  # "0" is the global default tool
        entry = vget(mapping, key)
        if isinstance(entry, dict) and entry.get("name"):
            return entry["name"]
    return None


def openxr_runtime():
    """Return (path, resolved_path) of the active OpenXR runtime manifest, or None."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for cand in (xdg / "openxr" / "1" / "active_runtime.json", Path("/etc/xdg/openxr/1/active_runtime.json")):
        if cand.exists():
            return cand, cand.resolve()
    return None


# --------------------------------------------------------------------------- release download


def api_get(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        hint = " (GitHub rate limit? set GITHUB_TOKEN)" if e.code in (403, 429) else ""
        raise ToolError(f"GitHub API {url} -> HTTP {e.code}{hint}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ToolError(f"could not reach {url}: {e}") from e


def pick_asset(release: dict) -> dict:
    zips = [a for a in release.get("assets", []) if str(a.get("name", "")).lower().endswith(".zip")]
    if not zips:
        raise ToolError(f"release {release.get('tag_name')} has no .zip asset")
    if len(zips) > 1:
        preferred = [
            a
            for a in zips
            if "argent" in a["name"].lower() and not re.search(r"symbol|pdb|debug|source|src", a["name"], re.I)
        ]
        zips = preferred or zips
        warn(f"several .zip assets, using {zips[0]['name']}")
    return zips[0]


def download(url: str, dest: Path) -> str:
    """Download url to dest (no auth header: the redirect target is a signed URL); return sha256."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            mark = 10
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if total and got * 100 // total >= mark:
                    info(f"  {got * 100 // total}%")
                    mark = (got * 100 // total // 10 + 1) * 10
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolError(f"download failed: {e}") from e
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_digest(asset: dict, actual_sha256: str) -> str:
    """Compare against the digest GitHub publishes for the asset. Returns a short status."""
    published = str(asset.get("digest") or "")
    if published.lower().startswith("sha256:"):
        expected = published.split(":", 1)[1].strip().lower()
        if expected != actual_sha256.lower():
            raise ToolError(
                f"SHA-256 mismatch for {asset.get('name')}: GitHub says {expected}, downloaded file is {actual_sha256}"
            )
        return "verified against the SHA-256 GitHub publishes for the asset"
    warn("GitHub did not publish a SHA-256 for this asset, so the download could not be verified")
    return "no published digest to compare"


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip. Backslashes count as separators (zips made by Windows PowerShell use them)."""
    dest = dest.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ToolError(f"{zip_path.name} is not a valid zip file") from e
    with zf:
        members = []
        total = 0
        for entry in zf.infolist():
            name = entry.filename.replace("\\", "/")
            total += entry.file_size
            if name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if dest not in target.parents:
                raise ToolError(f"unsafe path in zip: {entry.filename}")
            members.append((entry, target))
        if total > MAX_UNZIPPED:
            raise ToolError("zip is unexpectedly large; refusing to extract")
        for entry, target in members:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def pe_machine(path: Path) -> int:
    with path.open("rb") as f:
        head = f.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            raise ToolError(f"{path.name} is not a Windows executable")
        (pe_offset,) = struct.unpack_from("<I", head, 0x3C)
        f.seek(pe_offset)
        sig = f.read(6)
    if len(sig) < 6 or sig[:4] != b"PE\0\0":
        raise ToolError(f"{path.name} has no PE header")
    return struct.unpack("<H", sig[4:6])[0]


def find_launcher(root: Path) -> Path:
    hits = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.name.lower() == LAUNCHER_EXE.lower()),
        key=lambda p: len(p.parts),
    )
    if not hits:
        raise ToolError(f"{LAUNCHER_EXE} not found in the release; the layout of the upstream zip changed")
    if len(hits) > 1:
        warn(f"several {LAUNCHER_EXE} found, using {hits[0].relative_to(root)}")
    return hits[0]


def verify_payload(root: Path) -> Path:
    """Check an extracted ARGENT folder; return the path of ArgentLauncher.exe."""
    launcher = find_launcher(root)
    machine = pe_machine(launcher)
    if machine != 0x8664:
        raise ToolError(f"{launcher.name} is not a 64-bit x86 executable (machine 0x{machine:04x})")
    for name in EXPECTED_FILES:
        if find_file_ci(launcher.parent, name) is None:
            warn(f"{name} is not next to {launcher.name}; the upstream layout may have changed")
    return launcher


def fetch_payload(dest: Path, tag: str | None = None, zip_path: Path | None = None) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ToolError(f"{dest} is not empty")
    with tempfile.TemporaryDirectory(prefix="argent-dl-") as tmp:
        if zip_path is not None:
            archive = Path(zip_path)
            if not archive.is_file():
                raise ToolError(f"{archive} does not exist")
            release_tag, asset_name, sha, status = "local", archive.name, sha256_file(archive), "local file, not verified"
        else:
            url = f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/" + (
                f"tags/{urllib.parse.quote(tag)}" if tag else "latest"
            )
            release = api_get(url)
            asset = pick_asset(release)
            release_tag, asset_name = release.get("tag_name", "?"), asset["name"]
            info(f"Downloading {asset_name} ({release_tag}) ...")
            archive = Path(tmp) / "payload.zip"
            sha = download(asset["browser_download_url"], archive)
            status = check_digest(asset, sha)
        info(f"Extracting {asset_name} ...")
        safe_extract(archive, dest)
    launcher = verify_payload(dest)
    return {
        "tag": release_tag,
        "asset": asset_name,
        "sha256": sha,
        "verification": status,
        "launcher": str(launcher),
        "launcher_dir": str(launcher.parent),
    }


# --------------------------------------------------------------------------- checks


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def ok(self, msg: str) -> None:
        info(f"  ok    {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        info(f"  WARN  {msg}")

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        info(f"  FAIL  {msg}")


def run_checks(rep: Report):
    """Inspect the system. Returns (steam, library, game_dir, game_exe) or None when unusable."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        rep.ok(f"CPU architecture {machine}")
    else:
        rep.fail(
            f"CPU architecture is {machine}. This runs the x86-64 Windows build under Proton, so run it on the "
            "x86-64 PC that streams to the headset, not on the headset itself"
        )
    try:
        steam = find_steam()
    except ToolError as e:
        rep.fail(str(e))
        return None
    rep.ok(f"Steam at {steam}")
    if "com.valvesoftware.Steam" in str(steam):
        rep.warn("Flatpak Steam is untested: the installed files must be reachable from inside its sandbox")

    game = find_game(steam)
    if game is None:
        rep.fail(f"DOOM Eternal (appid {GAME_APPID}) is not installed in any Steam library")
        return None
    library, game_dir = game
    exe = find_file_ci(game_dir, GAME_EXE)
    if exe is None:
        rep.fail(f"{GAME_EXE} not found in {game_dir}; let Steam finish installing or verify the game files")
        return None
    rep.ok(f"DOOM Eternal at {game_dir}")

    if (library / "steamapps" / "compatdata" / GAME_APPID / "pfx" / "drive_c").is_dir():
        rep.ok("DOOM Eternal Proton prefix exists")
    else:
        rep.warn("no Proton prefix for DOOM Eternal yet: start the game once from Steam (flat), then quit")

    tool = compat_tool(steam)
    if tool is None:
        rep.warn("DOOM Eternal has no Steam Play compatibility tool selected; choose Proton Experimental")
    elif "experimental" in tool.lower():
        rep.ok(f"compatibility tool {tool}")
    else:
        rep.warn(f"compatibility tool is {tool}; Proton Experimental is the one this setup targets (OpenXR bridge)")

    runtime = openxr_runtime()
    if runtime is None:
        rep.warn("no active OpenXR runtime found; in SteamVR use Settings -> OpenXR -> Set SteamVR as OpenXR runtime")
    elif "steamvr" in str(runtime[1]).lower() or "steamxr" in str(runtime[1]).lower():
        rep.ok("SteamVR is the active OpenXR runtime")
    else:
        rep.warn(f"active OpenXR runtime is {runtime[1]}, not SteamVR (SteamVR is what streams to the Steam Frame)")
    return steam, library, game_dir, exe


# --------------------------------------------------------------------------- install / uninstall


def read_config_value(name: str):
    cfg = config_dir() / "config"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                parts = shlex.split(line.split("=", 1)[1])
                return parts[0] if parts else None
    except (OSError, ValueError):
        pass
    return None


def under(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def cmd_install(args: argparse.Namespace) -> int:
    if args.uninstall:
        return uninstall()

    info("Checking the setup:")
    rep = Report()
    ctx = run_checks(rep)
    if args.check:
        info("")
        info(f"{len(rep.errors)} problem(s), {len(rep.warnings)} warning(s)")
        return 1 if rep.errors else 0
    if rep.errors or ctx is None:
        raise ToolError("fix the problems above and run ./install.sh again")
    steam, library, game_dir, game_exe = ctx

    root = data_root()
    argent_dir = Path(os.environ.get("ARGENT_DIR") or root / "ARGENT").expanduser().resolve()
    if under(argent_dir, game_dir):
        raise ToolError(f"ARGENT must not live inside the DOOM Eternal folder ({argent_dir}); pick another ARGENT_DIR")
    if not under(argent_dir, Path.home()) and not any(under(argent_dir, lib) for lib in steam_libraries(steam)):
        warn(
            f"{argent_dir} is outside your home and your Steam libraries; Steam's container may not see it "
            "(add it to STEAM_COMPAT_MOUNTS / PRESSURE_VESSEL_FILESYSTEMS_RW in the launch options)"
        )
    if argent_dir.exists() and not argent_dir.is_dir():
        raise ToolError(f"{argent_dir} exists and is not a folder")
    if argent_dir.is_dir() and any(argent_dir.iterdir()) and not (argent_dir / MARKER).exists():
        raise ToolError(f"{argent_dir} exists and was not created by this installer; choose another ARGENT_DIR")
    wrapper_src = REPO_ROOT / "argent-launch"
    if not wrapper_src.is_file():
        raise ToolError(f"{wrapper_src} is missing; re-extract the release")

    info("")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as staging:
        payload = fetch_payload(Path(staging) / "payload", tag=args.tag, zip_path=args.zip)
        info(f"  {payload['verification']}; sha256 {payload['sha256']}")
        if args.force and (argent_dir / MARKER).exists():
            shutil.rmtree(argent_dir)
        shutil.copytree(payload["launcher_dir"], argent_dir, dirs_exist_ok=True)
    (argent_dir / MARKER).write_text(f"installed from upstream release {payload['tag']}\n", encoding="utf-8")
    launcher = find_file_ci(argent_dir, LAUNCHER_EXE)
    if launcher is None:  # cannot happen after a successful copy, but never write a broken config
        raise ToolError(f"{LAUNCHER_EXE} missing after install")

    wrapper_dst = root / "argent-launch"
    shutil.copyfile(wrapper_src, wrapper_dst)
    wrapper_dst.chmod(0o755)
    bin_dir().mkdir(parents=True, exist_ok=True)
    link = bin_dir() / "argent-launch"
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.exists():
        raise ToolError(f"{link} exists and is not a file; remove it and run again")
    link.symlink_to(wrapper_dst)

    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "config").write_text(
        "# Written by kharvox-argent-linux; sourced by argent-launch\n"
        f"ARGENT_DIR={shlex.quote(str(argent_dir))}\n"
        f"ARGENT_LAUNCHER={shlex.quote(str(launcher))}\n"
        f"GAME_DIR={shlex.quote(str(game_dir))}\n",
        encoding="utf-8",
    )

    win_path = "Z:" + str(game_exe).replace("/", "\\")
    info("")
    info(f"Installed ARGENT {payload['tag']} to {argent_dir}")
    info("")
    info("Next steps:")
    info("  1. Steam -> DOOM Eternal -> Properties -> Compatibility -> force Proton Experimental.")
    info("  2. Steam -> DOOM Eternal -> Properties -> General -> Launch Options, paste exactly:")
    info("")
    info(f"       {shlex.quote(str(link))} %command%")
    info("")
    info("  3. SteamVR -> Settings -> OpenXR -> Set SteamVR as OpenXR runtime; start SteamVR streaming to the Steam Frame.")
    info("  4. Press Play on DOOM Eternal in Steam. ArgentLauncher opens on the PC desktop.")
    info("     On the first run pick this file in the launcher, set your VR options, then press PLAY:")
    info(f"       {win_path}")
    info("")
    info(f"Launch log: {state_dir() / 'launch.log'}")
    return 0


def uninstall() -> int:
    root = data_root()
    removed: list[str] = []
    argent_dir = Path(read_config_value("ARGENT_DIR") or root / "ARGENT")
    if (argent_dir / MARKER).exists():
        shutil.rmtree(argent_dir)
        removed.append(str(argent_dir))
    elif argent_dir.exists():
        info(f"left in place (not created by this installer): {argent_dir}")
    link = bin_dir() / "argent-launch"
    if link.is_symlink() and link.resolve() == (root / "argent-launch").resolve():
        link.unlink()
        removed.append(str(link))
    wrapper = root / "argent-launch"
    if wrapper.is_file():
        wrapper.unlink()
        removed.append(str(wrapper))
    if root.is_dir():
        try:
            root.rmdir()
        except OSError:
            pass
    if config_dir().is_dir():
        shutil.rmtree(config_dir())
        removed.append(str(config_dir()))
    for item in removed:
        info(f"removed {item}")
    if not removed:
        info("nothing to remove")
    info("")
    info("Now clear the Launch Options of DOOM Eternal in Steam so it starts the normal game again.")
    info(f"Logs were kept in {state_dir()}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    result = fetch_payload(Path(args.dest), tag=args.tag, zip_path=args.zip)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="install or update ARGENT for Proton")
    p_install.add_argument("--check", action="store_true", help="only check the setup; change nothing")
    p_install.add_argument("--uninstall", action="store_true", help="remove what this installer installed")
    p_install.add_argument("--force", action="store_true", help="wipe the installed ARGENT folder first")
    p_install.add_argument("--tag", help="install this upstream release tag instead of the latest")
    p_install.add_argument("--zip", type=Path, help="use a local ARGENT release zip instead of downloading")
    p_install.set_defaults(func=cmd_install)

    p_fetch = sub.add_parser("fetch", help="download, verify and extract the release into --dest")
    p_fetch.add_argument("--dest", required=True, help="empty or new folder to extract into")
    p_fetch.add_argument("--tag", help="upstream release tag (default: latest)")
    p_fetch.add_argument("--zip", type=Path, help="use a local zip instead of downloading")
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
