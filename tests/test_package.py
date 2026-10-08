"""Verify the ZIP against AstrBot's legacy enclosing-directory installer."""
import importlib.util
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("canvas_packager", ROOT / "scripts/package.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class InstallationArchiveTests(unittest.TestCase):
    def test_legacy_astrbot_can_flatten_the_archive_without_runtime_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package = packager.build(root / "astrbot_plugin_persona_canvas.zip")
            installed = root / "installed"
            installed.mkdir()
            with zipfile.ZipFile(package) as archive:
                update_dir = installed / archive.namelist()[0]
                self.assertTrue(archive.infolist()[0].is_dir())
                archive.extractall(installed)
            # This is the enclosing-directory flattening performed by AstrBot
            # v4.17 PluginUpdator.unzip_file, confined to this temporary folder.
            self.assertTrue(update_dir.resolve().is_relative_to(root))
            for source in update_dir.iterdir():
                destination = installed / source.name
                self.assertTrue(source.resolve().is_relative_to(root))
                self.assertTrue(destination.resolve().is_relative_to(root))
                shutil.move(str(source), str(destination))
            update_dir.rmdir()
            self.assertEqual((installed / "main.py").read_bytes(), (ROOT / "main.py").read_bytes())
            self.assertTrue((installed / "pages/canvas/index.html").is_file())
            self.assertTrue((installed / "providers/base.py").is_file())
            self.assertEqual((installed / "metadata.yaml").read_bytes(), (ROOT / "metadata.yaml").read_bytes())
            self.assertTrue((installed / "companion.py").is_file())
            self.assertEqual((installed / "logo.png").read_bytes(), (ROOT / "logo.png").read_bytes())
            self.assertFalse((installed / "data").exists())
            self.assertFalse((installed / ".git").exists())


if __name__ == "__main__":
    unittest.main()
