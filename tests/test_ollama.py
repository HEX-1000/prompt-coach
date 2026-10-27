"""Finding Ollama on this computer, and what install.ps1 tells someone who has none.

python -B -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import coach_core as core  # noqa: E402


class OllamaInstalledTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name)

    def check(self, on_path: bool, localappdata) -> bool:
        env = {"LOCALAPPDATA": str(localappdata)} if localappdata else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not localappdata:
                os.environ.pop("LOCALAPPDATA", None)
            with mock.patch("coach_core.shutil.which", return_value="ollama" if on_path else None):
                return core.ollama_installed()

    def make_program(self):
        exe = self.local / "Programs" / "Ollama" / "ollama.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")

    def test_on_path(self):
        self.assertTrue(self.check(on_path=True, localappdata=None))

    def test_in_the_usual_windows_folder(self):
        self.make_program()
        self.assertTrue(self.check(on_path=False, localappdata=self.local))

    def test_nowhere(self):
        self.assertFalse(self.check(on_path=False, localappdata=self.local))

    def test_no_local_app_data_variable(self):
        self.assertFalse(self.check(on_path=False, localappdata=None))

    def test_the_folder_alone_is_not_the_program(self):
        (self.local / "Programs" / "Ollama").mkdir(parents=True)
        self.assertFalse(self.check(on_path=False, localappdata=self.local))


class LocalUrlTests(unittest.TestCase):
    def test_addresses_on_this_computer(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434", "HTTP://LOCALHOST:1"):
            self.assertTrue(core.is_local_url(url), url)

    def test_other_addresses(self):
        for url in ("http://192.168.1.20:11434", "http://ollama.example.com", "http://localhost.example.com", ""):
            self.assertFalse(core.is_local_url(url), url)


class InstallerWordingTests(unittest.TestCase):
    """install.ps1 cannot be run here, so these check that it says what the app says."""

    def setUp(self):
        self.text = (ROOT / "install.ps1").read_text(encoding="ascii")

    def test_the_not_installed_note_has_the_link_and_the_sign_in_tip(self):
        self.assertIn(core.OLLAMA_DOWNLOAD_URL, self.text)
        self.assertIn(core.OLLAMA_SKIP_SIGN_IN, self.text)
        self.assertIn("Then run this script again to check the models.", self.text)

    def test_the_note_goes_into_the_before_you_start_list(self):
        line = next(ln for ln in self.text.splitlines() if "Ollama is not installed" in ln)
        self.assertIn("$Notes.Add(", line)

    def test_it_looks_where_the_app_looks(self):
        self.assertIn("Get-Command ollama", self.text)
        self.assertIn(r"Programs\Ollama\ollama.exe", self.text)


if __name__ == "__main__":
    unittest.main()
