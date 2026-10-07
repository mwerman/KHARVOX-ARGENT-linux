"""Offline tests: a fake Steam tree and a fake ARGENT release zip. No network, no Steam, no Wine."""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import argent_tools as at  # noqa: E402

TOOL = ROOT / "tools" / "argent_tools.py"
WRAPPER = ROOT / "argent-launch"


def fake_pe(machine: int = 0x8664) -> bytes:
    """Smallest thing pe_machine() accepts: DOS header -> PE signature -> machine field."""
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    return bytes(dos) + b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 32


def make_release_zip(path: Path, prefix: str = "ARGENT/", sep: str = "/", machine: int = 0x8664) -> None:
    names = {
        "ArgentLauncher.exe": fake_pe(machine),
        "ArgentLayer.dll": fake_pe(),
        "ArgentLayer.json": b"{}",
        "openxr_loader.dll": fake_pe(),
        "assets/hand_models.cfg": b"cfg",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in names.items():
            zf.writestr((prefix + name).replace("/", sep), data)


class FakeSystem:
    """A temporary HOME with a Steam install that has DOOM Eternal in a second library."""

    def __init__(self, tmp: Path, with_game: bool = True, compat_tool: str = "proton_experimental") -> None:
        self.home = tmp / "home"
        self.steam = self.home / ".local" / "share" / "Steam"
        self.lib2 = tmp / "games"
        (self.steam / "steamapps").mkdir(parents=True)
        (self.steam / "config").mkdir()
        (self.lib2 / "steamapps").mkdir(parents=True)
        (self.steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n'
            '\t\t"apps"\n\t\t{\n\t\t\t"782330"\t\t"1"\n\t\t}\n\t}\n}\n' % (self.steam, self.lib2)
        )
        (self.steam / "config" / "config.vdf").write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
            '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"782330"\n\t\t\t\t\t{\n'
            '\t\t\t\t\t\t"name"\t\t"%s"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n' % compat_tool
        )
        self.game_dir = self.lib2 / "steamapps" / "common" / "DOOMEternal"
        if with_game:
            (self.lib2 / "steamapps" / "appmanifest_782330.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"782330"\n\t"installdir"\t\t"DOOMEternal"\n}\n'
            )
            self.game_dir.mkdir(parents=True)
            (self.game_dir / "DOOMEternalx64vk.exe").write_bytes(fake_pe())
            (self.lib2 / "steamapps" / "compatdata" / "782330" / "pfx" / "drive_c").mkdir(parents=True)

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "STEAM", "ARGENT"))}
        env.update(HOME=str(self.home), STEAM_DIR=str(self.steam), PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), *args], env=self.env(**extra_env), capture_output=True, text=True
        )


class VdfTests(unittest.TestCase):
    def test_nested_comments_and_escapes(self):
        data = at.parse_vdf('// comment\n"a" { "b" "c\\\\d" "url" "http://x/y" "n" { "k" "v" } }\n')
        self.assertEqual(data["a"]["b"], "c\\d")
        self.assertEqual(data["a"]["url"], "http://x/y")
        self.assertEqual(at.vget(data, "A", "N", "K"), "v")
        self.assertIsNone(at.vget(data, "a", "missing"))

    def test_rejects_unbalanced(self):
        for bad in ('"a" {', '"a" "b" }', '"a"', "garbage"):
            with self.assertRaises(ValueError, msg=bad):
                at.parse_vdf(bad)

    def test_old_style_libraryfolders(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            steam, other = tmp / "Steam", tmp / "other"
            (steam / "steamapps").mkdir(parents=True)
            (other / "steamapps").mkdir(parents=True)
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                '"LibraryFolders" { "TimeNextStatsReport" "123" "1" "%s" }' % other
            )
            self.assertEqual([p.resolve() for p in at.steam_libraries(steam)], [steam.resolve(), other.resolve()])


class PayloadTests(unittest.TestCase):
    def test_pe_machine(self):
        with tempfile.TemporaryDirectory() as t:
            ok, bad, notpe = Path(t, "a.exe"), Path(t, "b.exe"), Path(t, "c.exe")
            ok.write_bytes(fake_pe(0x8664))
            bad.write_bytes(fake_pe(0x014C))
            notpe.write_bytes(b"hello")
            self.assertEqual(at.pe_machine(ok), 0x8664)
            self.assertEqual(at.pe_machine(bad), 0x014C)
            with self.assertRaises(at.ToolError):
                at.pe_machine(notpe)

    def test_backslash_zip_from_powershell(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            make_release_zip(tmp / "r.zip", prefix="ARGENT/", sep="\\")
            result = at.fetch_payload(tmp / "out", zip_path=tmp / "r.zip")
            self.assertEqual(Path(result["launcher_dir"]), (tmp / "out" / "ARGENT").resolve())
            self.assertTrue((tmp / "out" / "ARGENT" / "assets" / "hand_models.cfg").is_file())

    def test_flat_zip_without_top_folder(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            make_release_zip(tmp / "r.zip", prefix="")
            result = at.fetch_payload(tmp / "out", zip_path=tmp / "r.zip")
            self.assertEqual(Path(result["launcher_dir"]), (tmp / "out").resolve())

    def test_zip_slip_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with zipfile.ZipFile(tmp / "evil.zip", "w") as zf:
                zf.writestr("..\\..\\evil.txt", "x")
                zf.writestr("../evil2.txt", "x")
            with self.assertRaises(at.ToolError):
                at.safe_extract(tmp / "evil.zip", tmp / "out")
            self.assertFalse((tmp / "evil.txt").exists() or (tmp.parent / "evil.txt").exists())

    def test_32bit_launcher_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            make_release_zip(tmp / "r.zip", machine=0x014C)
            with self.assertRaises(at.ToolError):
                at.fetch_payload(tmp / "out", zip_path=tmp / "r.zip")

    def test_missing_launcher_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with zipfile.ZipFile(tmp / "r.zip", "w") as zf:
                zf.writestr("readme.txt", "x")
            with self.assertRaises(at.ToolError):
                at.fetch_payload(tmp / "out", zip_path=tmp / "r.zip")

    def test_digest_check(self):
        self.assertIn("verified", at.check_digest({"name": "a.zip", "digest": "sha256:ABC123"}, "abc123"))
        with self.assertRaises(at.ToolError):
            at.check_digest({"name": "a.zip", "digest": "sha256:abc123"}, "def456")
        self.assertIn("no published digest", at.check_digest({"name": "a.zip"}, "def456"))

    def test_pick_asset_ignores_source_archives_and_symbols(self):
        release = {
            "tag_name": "v1",
            "assets": [
                {"name": "ARGENT-1.0-symbols.zip"},
                {"name": "ARGENT-1.0.zip"},
                {"name": "notes.txt"},
            ],
        }
        self.assertEqual(at.pick_asset(release)["name"], "ARGENT-1.0.zip")
        with self.assertRaises(at.ToolError):
            at.pick_asset({"tag_name": "v1", "assets": [{"name": "x.tar.gz"}]})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.zip = self.tmp / "release.zip"
        make_release_zip(self.zip, sep="\\")

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_passes_on_good_system_and_changes_nothing(self):
        sysm = FakeSystem(self.tmp)
        res = sysm.run("install", "--check")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("compatibility tool proton_experimental", res.stderr)
        self.assertFalse((sysm.home / ".local" / "share" / "kharvox-argent").exists())
        self.assertFalse((sysm.home / ".config" / "kharvox-argent").exists())

    def test_check_fails_without_game(self):
        res = FakeSystem(self.tmp, with_game=False).run("install", "--check")
        self.assertEqual(res.returncode, 1)
        self.assertIn("DOOM Eternal (appid 782330) is not installed", res.stderr)

    def test_check_warns_about_other_proton(self):
        res = FakeSystem(self.tmp, compat_tool="proton_9").run("install", "--check")
        self.assertEqual(res.returncode, 0)
        self.assertIn("proton_9", res.stderr)

    def test_full_install_update_uninstall(self):
        sysm = FakeSystem(self.tmp)
        res = sysm.run("install", "--zip", str(self.zip))
        self.assertEqual(res.returncode, 0, res.stderr)

        share = sysm.home / ".local" / "share" / "kharvox-argent"
        argent = share / "ARGENT"
        self.assertTrue((argent / "ArgentLauncher.exe").is_file())
        self.assertTrue((argent / "assets" / "hand_models.cfg").is_file())
        self.assertTrue((argent / at.MARKER).is_file())
        link = sysm.home / ".local" / "bin" / "argent-launch"
        self.assertTrue(link.is_symlink())
        self.assertTrue(os.access(link, os.X_OK))
        config = (sysm.home / ".config" / "kharvox-argent" / "config").read_text()
        self.assertIn(f"ARGENT_LAUNCHER={argent / 'ArgentLauncher.exe'}", config)
        self.assertIn(f"GAME_DIR={sysm.game_dir}", config)
        self.assertIn(f"{link} %command%", res.stderr)
        self.assertIn("Z:" + str(sysm.game_dir / "DOOMEternalx64vk.exe").replace("/", "\\"), res.stderr)
        self.assertFalse(list(share.glob(".staging-*")), "staging dir must be cleaned up")

        # user setting written by the launcher survives an update
        (argent / "user.ini").write_text("keep me")
        again = sysm.run("install", "--zip", str(self.zip))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((argent / "user.ini").read_text(), "keep me")
        # --force starts clean
        forced = sysm.run("install", "--zip", str(self.zip), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse((argent / "user.ini").exists())

        # the installed wrapper, resolved through the symlink, works end to end
        wrap = subprocess.run(
            [str(link), "reaper", "SteamLaunch", "--", "proton", "waitforexitandrun", str(sysm.game_dir / "DOOMEternalx64vk.exe")],
            env=sysm.env(ARGENT_DRY_RUN="1", SteamGameId="782330"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(wrap.returncode, 0, wrap.stderr)
        self.assertIn(str(argent / "ArgentLauncher.exe"), wrap.stdout)

        gone = sysm.run("install", "--uninstall")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertFalse(share.exists())
        self.assertFalse(link.is_symlink())
        self.assertFalse((sysm.home / ".config" / "kharvox-argent").exists())
        self.assertTrue(sysm.game_dir.joinpath("DOOMEternalx64vk.exe").is_file(), "game files must never be touched")

    def test_refuses_to_install_inside_game_folder(self):
        sysm = FakeSystem(self.tmp)
        res = sysm.run("install", "--zip", str(self.zip), ARGENT_DIR=str(sysm.game_dir / "ARGENT"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("must not live inside the DOOM Eternal folder", res.stderr)

    def test_refuses_foreign_non_empty_folder(self):
        sysm = FakeSystem(self.tmp)
        foreign = sysm.home / "Documents"
        foreign.mkdir(parents=True)
        (foreign / "precious.txt").write_text("x")
        res = sysm.run("install", "--zip", str(self.zip), ARGENT_DIR=str(foreign))
        self.assertEqual(res.returncode, 1)
        self.assertIn("was not created by this installer", res.stderr)
        self.assertTrue((foreign / "precious.txt").exists())

    def test_bad_zip_leaves_system_untouched(self):
        sysm = FakeSystem(self.tmp)
        make_release_zip(self.zip, machine=0x014C)
        res = sysm.run("install", "--zip", str(self.zip))
        self.assertEqual(res.returncode, 1)
        self.assertFalse((sysm.home / ".config" / "kharvox-argent").exists())
        self.assertFalse((sysm.home / ".local" / "share" / "kharvox-argent" / "ARGENT").exists())

    def test_fetch_subcommand_prints_json(self):
        import json

        out = self.tmp / "fetched"
        res = subprocess.run(
            [sys.executable, str(TOOL), "fetch", "--dest", str(out), "--zip", str(self.zip)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["launcher"].endswith("ArgentLauncher.exe"))


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.argent = tmp / "My ARGENT"  # a space on purpose
        self.argent.mkdir()
        self.launcher = self.argent / "ArgentLauncher.exe"
        self.launcher.write_bytes(fake_pe())
        cfg = tmp / "cfg" / "kharvox-argent"
        cfg.mkdir(parents=True)
        import shlex

        (cfg / "config").write_text(
            f"ARGENT_DIR={shlex.quote(str(self.argent))}\nARGENT_LAUNCHER={shlex.quote(str(self.launcher))}\n"
        )
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp / "home"),
            "XDG_CONFIG_HOME": str(tmp / "cfg"),
            "XDG_STATE_HOME": str(tmp / "state"),
            "ARGENT_DRY_RUN": "1",
            "SteamGameId": "782330",
        }

    def tearDown(self):
        self._tmp.cleanup()

    def run_wrapper(self, *args, **env):
        return subprocess.run(["bash", str(WRAPPER), *args], env={**self.env, **env}, capture_output=True, text=True)

    def test_replaces_only_the_game_exe(self):
        game = "/games/steamapps/common/DOOMEternal/DOOMEternalx64vk.exe"
        res = self.run_wrapper(
            "/steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=782330", "--",
            "/steam/SteamLinuxRuntime_sniper/_v2-entry-point", "--verb=waitforexitandrun", "--",
            "/steam/compatibilitytools.d/Proton - Experimental/proton", "waitforexitandrun", game,
        )  # fmt: skip
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("My\\ ARGENT/ArgentLauncher.exe", res.stdout)
        self.assertNotIn("DOOMEternalx64vk", res.stdout)
        self.assertIn("Proton\\ -\\ Experimental/proton", res.stdout)
        self.assertTrue(res.stdout.startswith("/steam/ubuntu12_32/reaper SteamLaunch AppId=782330 -- "))

    def test_real_steam_command_with_idtech_launcher(self):
        # Verbatim shape of the command Steam passed on a real CachyOS install (paths from a user log).
        steam = "/home/matt/.local/share/Steam"
        res = self.run_wrapper(
            f"{steam}/ubuntu12_32/steam-launch-wrapper", "--",
            f"{steam}/ubuntu12_32/reaper", "SteamLaunch", "AppId=782330", "--",
            f"{steam}/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point", "--verb=waitforexitandrun", "--",
            f"{steam}/steamapps/common/Proton - Experimental/proton", "waitforexitandrun",
            f"{steam}/steamapps/common/DOOMEternal/launcher/idTechLauncher.exe",
        )  # fmt: skip
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("My\\ ARGENT/ArgentLauncher.exe", res.stdout)
        self.assertNotIn("idTechLauncher", res.stdout)
        self.assertIn("Proton\\ -\\ Experimental/proton waitforexitandrun", res.stdout)

    def test_unknown_exe_inside_game_folder_is_replaced_but_outside_is_not(self):
        cfg = Path(self.env["XDG_CONFIG_HOME"]) / "kharvox-argent" / "config"
        cfg.write_text(cfg.read_text() + "GAME_DIR=/games/steamapps/common/DOOMEternal\n")
        inside = self.run_wrapper("proton", "waitforexitandrun", "/games/steamapps/common/DOOMEternal/bin/Other.EXE")
        self.assertEqual(inside.returncode, 0, inside.stderr)
        self.assertIn("ArgentLauncher.exe", inside.stdout)
        outside = self.run_wrapper("proton", "waitforexitandrun", "/games/steamapps/common/OtherGame/Other.exe")
        self.assertEqual(outside.returncode, 1)
        # a folder that merely shares the prefix must not match
        sibling = self.run_wrapper("proton", "run", "/games/steamapps/common/DOOMEternal2/x.exe")
        self.assertEqual(sibling.returncode, 1)

    def test_trailing_user_args_dropped_unless_forwarded(self):
        cmd = ["proton", "waitforexitandrun", "/g/DOOMEternalx64vk.exe", "-foo"]
        self.assertNotIn("-foo", self.run_wrapper(*cmd).stdout)
        self.assertIn("-foo", self.run_wrapper(*cmd, ARGENT_FORWARD_ARGS="1").stdout)

    def test_fails_loudly_when_no_game_exe(self):
        res = self.run_wrapper("proton", "waitforexitandrun", "/g/SomethingElse.exe")
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not find the game executable", res.stderr)

    def test_usage_without_arguments(self):
        self.assertEqual(self.run_wrapper().returncode, 2)
        self.assertEqual(self.run_wrapper("--help").returncode, 0)

    def test_not_installed(self):
        res = self.run_wrapper("x", "/g/DOOMEternalx64vk.exe", XDG_CONFIG_HOME="/nonexistent")
        self.assertEqual(res.returncode, 1)
        self.assertIn("not installed", res.stderr)

    def test_real_exec_runs_the_command_from_the_launcher_folder(self):
        probe = Path(self._tmp.name) / "fake-proton"
        probe.write_text('#!/bin/sh\npwd\nfor a in "$@"; do echo "arg:$a"; done\n')
        probe.chmod(0o755)
        res = subprocess.run(
            ["bash", str(WRAPPER), str(probe), "waitforexitandrun", "/g/DOOMEternalx64vk.exe"],
            env={k: v for k, v in self.env.items() if k != "ARGENT_DRY_RUN"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0], str(self.argent))
        self.assertIn(f"arg:{self.launcher}", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
