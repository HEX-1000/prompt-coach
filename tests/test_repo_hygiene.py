"""Repository rules: no em dash, Windows-safe scripts, a plain VERSION file, personal data ignored.

python -B -m unittest discover -s tests
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import coach_update  # noqa: E402

EM_DASH = chr(0x2014)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".ps1", ".cmd", ".svg", ""}
# rules/ holds the user's communication rules, which contain the character on purpose.
# contexts/ holds the user's own glossaries.
EXCLUDED_DIRS = {"rules", "contexts"}
# Only used when this folder is not a git checkout (a ZIP copy): skip what is personal or generated.
NOT_SHIPPED = {".git", ".venv", "history", ".update_backup", "__pycache__", *EXCLUDED_DIRS}

HAS_GIT = shutil.which("git") is not None


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def is_checkout() -> bool:
    return HAS_GIT and git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def tracked_files() -> list[Path]:
    """Files git tracks. In a folder without git, the files an update would ship."""
    if is_checkout():
        names = git("ls-files", "-z").stdout.split("\0")
        return [ROOT / n for n in names if n]
    return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & NOT_SHIPPED)]


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


class RepoHygieneTests(unittest.TestCase):
    def test_the_file_list_is_not_empty(self):
        # Guards the other tests: an empty list would make them pass without checking anything.
        names = [relative(p) for p in tracked_files()]
        self.assertIn("coach_tui.py", names)
        self.assertIn("install.ps1", names)

    def test_no_em_dash_character_in_text_files(self):
        offenders = []
        for path in tracked_files():
            rel = relative(path)
            if rel.split("/")[0] in EXCLUDED_DIRS or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if EM_DASH in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_powershell_scripts_are_plain_ascii(self):
        # Windows PowerShell 5.1 reads a file without a byte order mark in the local code page,
        # so any other character can turn into garbage or a parse error.
        scripts = sorted(ROOT.glob("*.ps1"))
        self.assertIn("install.ps1", [p.name for p in scripts])
        for path in scripts:  # setup_ollama.ps1 is not shipped, so it is checked only when it is there
            data = path.read_bytes()
            bad = [i for i, byte in enumerate(data) if byte > 127]
            self.assertEqual(bad, [], f"{path.name} has non-ASCII bytes at offsets {bad[:5]}")

    def test_version_is_one_line_of_numbers_separated_by_dots(self):
        data = (ROOT / "VERSION").read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "VERSION starts with a byte order mark")
        text = data.decode("ascii")
        self.assertEqual(len(text.splitlines()), 1, "VERSION must be a single line")
        self.assertRegex(text.strip(), r"^\d+(\.\d+){1,3}$")
        self.assertIsNotNone(coach_update.parse_version(text))  # what the updater itself accepts

    def test_gitignore_lists_the_personal_paths(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (".venv/", ".state.json", ".update_backup/", "history/",
                        "contexts/*", "!contexts/_template.md", "rules/*", "!rules/README.md"):
            self.assertIn(pattern, lines)

    @unittest.skipUnless(is_checkout(), "needs a git checkout")
    def test_git_ignores_personal_files_and_keeps_the_shipped_ones(self):
        def ignored(rel: str) -> bool:
            # --no-index judges the patterns alone, whether or not the file is tracked
            return git("check-ignore", "-q", "--no-index", rel).returncode == 0

        for rel in (".state.json", "history/20260101-000000.json", "contexts/my-project.md",
                    "rules/communication_rules.md", ".venv/Scripts/python.exe", ".update_backup/coach_tui.py"):
            self.assertTrue(ignored(rel), f"{rel} would be committed")
        for rel in ("contexts/_template.md", "rules/README.md", "VERSION", "CHANGELOG.md"):
            self.assertFalse(ignored(rel), f"{rel} is ignored but ships")

    def test_no_personal_files_are_tracked(self):
        if not is_checkout():
            self.skipTest("needs a git checkout")
        tracked = {relative(p) for p in tracked_files()}
        personal = {n for n in tracked if re.match(r"^(history/|\.venv/|\.update_backup/)", n)} | (
            tracked & {".state.json"})
        personal |= {n for n in tracked if n.startswith(("contexts/", "rules/"))
                     and n not in ("contexts/_template.md", "rules/README.md")}
        self.assertEqual(personal, set())


if __name__ == "__main__":
    unittest.main()
