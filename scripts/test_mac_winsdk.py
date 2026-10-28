"""SDK architecture boundaries; local fake files only, no installation/download."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import prepare_mac_winsdk as sdk


class SdkArchitectureTests(unittest.TestCase):
    def fixture(self, home, arches):
        root = home / "clearcote-ime-spike" / "sdk"
        vc = root / "VC/Tools/MSVC" / sdk.VC
        kit = root / "Windows Kits/10"
        for sub in ("um", "shared", "winrt", "cppwinrt", "ucrt"):
            (kit / "Include" / sdk.SDK / sub).mkdir(parents=True)
        (vc / "include").mkdir(parents=True)
        (vc / "include/vcruntime.h").write_bytes(b"fixture")
        for name in ("windows.h", "WiNdOwS.h"):
            (kit / "Include" / sdk.SDK / "um" / name).write_bytes(b"fixture")
        for arch in arches:
            for directory, name in ((vc / "lib" / arch, "libcmt.lib"),
                    (kit / "Lib" / sdk.SDK / "um" / arch, "kernel32.lib"),
                    (kit / "Lib" / sdk.SDK / "ucrt" / arch, "ucrt.lib")):
                directory.mkdir(parents=True)
                (directory / name).write_bytes(b"fixture")
        return root

    def test_missing_requested_architecture_stops_before_metadata_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = self.fixture(home, ("x64",))
            with patch.object(sdk.platform, "system", return_value="Darwin"), patch.object(sdk.Path, "home", return_value=home), patch.object(sdk.Path, "symlink_to"):

                with self.assertRaisesRegex(RuntimeError, "payload directory missing.*x86"):
                    sdk.layout(root, ("x64", "x86"))
            self.assertFalse((root / "Windows Kits/10/bin/SetEnv.x64.json").exists())

    def test_architecture_metadata_uses_only_its_real_library_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = self.fixture(home, ("x64", "x86"))
            with patch.object(sdk.platform, "system", return_value="Darwin"), patch.object(sdk.Path, "home", return_value=home), patch.object(sdk.Path, "symlink_to"):

                result = sdk.layout(root, ("x64", "x86"))
            self.assertEqual(result["architectures"], ["x64", "x86"])
            for arch in ("x64", "x86"):
                env = json.loads((root / f"Windows Kits/10/bin/SetEnv.{arch}.json").read_text())["env"]
                self.assertTrue(all(parts[-1] == arch for parts in env["LIB"]))
                self.assertTrue(all(root.joinpath(*parts).is_dir() for parts in env["LIB"]))


if __name__ == "__main__":
    unittest.main()
