"""Tests for coach_update.py. Standard library only: python -B -m unittest discover -s tests"""
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import coach_update as up  # noqa: E402

GOOD_PY = "VALUE = 'new'\n"

CHANGELOG_TEXT = """# Changelog

Newest first.

## 1.3.0

- Third thing.
- Fourth thing.

## 1.2.0
- Second thing.

## 1.1.0
- First thing.
"""


def make_zip(path: Path, files: dict[str, str | bytes], top: str = "prompt-coach-main") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for rel, content in files.items():
            name = f"{top}/{rel}" if top else rel
            zf.writestr(name, content)


class Remote:
    """A fake GitHub: a VERSION file and a zip, both served as file:// URLs."""

    def __init__(self, root: Path, version: str, files: dict[str, str | bytes], top: str = "prompt-coach-main"):
        self.version_file = root / "remote_VERSION"
        self.zip_file = root / "main.zip"
        self.version_file.write_text(version + "\n", encoding="utf-8")
        make_zip(self.zip_file, {"VERSION": version + "\n", **files}, top)

    @property
    def urls(self) -> dict:
        return {"version_url": self.version_file.as_uri(), "zip_url": self.zip_file.as_uri()}


class ZipUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "app"
        self.app.mkdir()
        (self.app / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (self.app / "coach_core.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (self.app / "coach_tui.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (self.app / "config.json").write_text('{"models": ["mine"]}', encoding="utf-8")
        (self.app / "requirements.txt").write_text("textual>=8.2,<9\n", encoding="utf-8")
        (self.app / ".state.json").write_text('{"model": "mine"}', encoding="utf-8")
        (self.app / "contexts").mkdir()
        (self.app / "contexts" / "mine.md").write_text("my glossary", encoding="utf-8")
        (self.app / "contexts" / "_template.md").write_text("old template", encoding="utf-8")
        (self.app / "rules").mkdir()
        (self.app / "rules" / "communication_rules.md").write_text("my rules", encoding="utf-8")
        (self.app / "history").mkdir()
        (self.app / "history" / "a.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_remote(self, version="1.1.0", **extra):
        files = {
            "coach_core.py": GOOD_PY,
            "coach_tui.py": GOOD_PY,
            "config.json": '{"models": ["upstream"]}',
            "requirements.txt": "textual>=8.2,<9\n",
            "README.md": "new readme\n",
            "contexts/_template.md": "new template",
            "contexts/stranger.md": "must not appear",
            "rules/README.md": "new rules readme",
            "rules/communication_rules.md": "must not overwrite",
            "history/a.json": "must not overwrite",
            ".state.json": "must not overwrite",
        }
        files.update(extra)
        return Remote(self.tmp, version, files)

    def test_updates_code_and_keeps_user_files(self):
        remote = self.new_remote()
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual((res.status, res.old, res.new), ("updated", "1.0.0", "1.1.0"), res.message)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.1.0")
        self.assertEqual((self.app / "coach_core.py").read_text(), GOOD_PY)
        self.assertEqual((self.app / "README.md").read_text(), "new readme\n")
        self.assertEqual((self.app / "contexts" / "_template.md").read_text(), "new template")
        self.assertEqual((self.app / "rules" / "README.md").read_text(), "new rules readme")
        # user data is untouched
        self.assertEqual((self.app / "config.json").read_text(), '{"models": ["mine"]}')
        self.assertEqual((self.app / ".state.json").read_text(), '{"model": "mine"}')
        self.assertEqual((self.app / "contexts" / "mine.md").read_text(), "my glossary")
        self.assertEqual((self.app / "rules" / "communication_rules.md").read_text(), "my rules")
        self.assertEqual((self.app / "history" / "a.json").read_text(), "{}")
        self.assertFalse((self.app / "contexts" / "stranger.md").exists())
        # the replaced file was backed up
        self.assertEqual((self.app / up.BACKUP_DIR / "coach_core.py").read_text(), "VALUE = 'old'\n")
        self.assertEqual(list(self.app.rglob("*.new")), [])

    def test_missing_config_is_created(self):
        (self.app / "config.json").unlink()
        res = up.check_and_update(self.app, **self.new_remote().urls)
        self.assertEqual(res.status, "updated", res.message)
        self.assertEqual((self.app / "config.json").read_text(), '{"models": ["upstream"]}')

    def test_same_version_changes_nothing(self):
        remote = self.new_remote(version="1.0.0")
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual(res.status, "current")
        self.assertEqual((self.app / "coach_core.py").read_text(), "VALUE = 'old'\n")

    def test_local_newer_than_remote_changes_nothing(self):
        (self.app / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        res = up.check_and_update(self.app, **self.new_remote().urls)
        self.assertEqual(res.status, "current")
        self.assertEqual((self.app / "VERSION").read_text().strip(), "2.0.0")

    def test_missing_local_version_counts_as_old(self):
        (self.app / "VERSION").unlink()
        res = up.check_and_update(self.app, **self.new_remote().urls)
        self.assertEqual(res.status, "updated", res.message)

    def test_unreachable_is_offline(self):
        res = up.check_and_update(
            self.app, version_url=(self.tmp / "nope").as_uri(), zip_url=(self.tmp / "nope.zip").as_uri()
        )
        self.assertEqual(res.status, "offline")
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.0.0")

    def test_garbage_version_file_fails_quietly(self):
        (self.tmp / "remote_VERSION").write_text("<html>404</html>", encoding="utf-8")
        res = up.check_and_update(self.app, version_url=(self.tmp / "remote_VERSION").as_uri(), zip_url="x")
        self.assertEqual(res.status, "failed")

    def test_syntax_error_in_download_writes_nothing(self):
        remote = self.new_remote(**{"coach_core.py": "def broken(:\n"})
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual(res.status, "failed")
        self.assertIn("does not compile", res.message)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.0.0")
        self.assertEqual((self.app / "coach_tui.py").read_text(), "VALUE = 'old'\n")
        self.assertFalse((self.app / "README.md").exists())

    def test_zip_slip_is_rejected(self):
        remote = self.new_remote()
        with zipfile.ZipFile(remote.zip_file, "a") as zf:
            zf.writestr("prompt-coach-main/../escaped.py", "x = 1\n")
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual(res.status, "failed")
        self.assertFalse((self.tmp / "escaped.py").exists())
        self.assertEqual((self.app / "VERSION").read_text().strip(), "1.0.0")

    def test_zip_without_required_file_is_rejected(self):
        remote = Remote(self.tmp, "1.1.0", {"README.md": "only a readme"})
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual(res.status, "failed")
        self.assertEqual((self.app / "coach_tui.py").read_text(), "VALUE = 'old'\n")

    def test_not_a_zip_is_rejected(self):
        remote = self.new_remote()
        remote.zip_file.write_bytes(b"this is not a zip")
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual(res.status, "failed")

    def test_line_ending_only_difference_is_not_rewritten(self):
        (self.app / "README.md").write_bytes(b"new readme\r\n")
        remote = self.new_remote()
        up.check_and_update(self.app, **remote.urls)
        self.assertEqual((self.app / "README.md").read_bytes(), b"new readme\r\n")
        self.assertFalse((self.app / up.BACKUP_DIR / "README.md").exists())

    def test_is_protected(self):
        for rel in ("config.json", ".state.json", "contexts/mine.md", "rules/communication_rules.md",
                    "history/x.json", ".venv/Scripts/python.exe", ".git/config", "pkg/__pycache__/a.pyc"):
            self.assertTrue(up.is_protected(rel), rel)
        for rel in ("coach_tui.py", "README.md", "contexts/_template.md", "rules/README.md",
                    "assets/icon.ico", "requirements.txt", "VERSION", "CHANGELOG.md"):
            self.assertFalse(up.is_protected(rel), rel)

    def test_the_changelog_is_shipped_and_whats_new_reads_it(self):
        remote = self.new_remote(**{"CHANGELOG.md": CHANGELOG_TEXT})
        res = up.check_and_update(self.app, **remote.urls)
        self.assertEqual((res.status, res.old, res.new), ("updated", "1.0.0", "1.1.0"), res.message)
        self.assertEqual((self.app / "CHANGELOG.md").read_text(encoding="utf-8"), CHANGELOG_TEXT)
        self.assertEqual(up.whats_new(self.app, res.old, res.new), ["First thing."])

    def test_an_update_without_a_changelog_still_works_and_says_nothing_new(self):
        res = up.check_and_update(self.app, **self.new_remote().urls)
        self.assertEqual(res.status, "updated", res.message)
        self.assertFalse((self.app / "CHANGELOG.md").exists())
        self.assertEqual(up.whats_new(self.app, res.old, res.new), [])

    def test_version_parsing(self):
        self.assertEqual(up.parse_version("1.10.0"), (1, 10, 0))
        self.assertIsNone(up.parse_version("v1.0"))
        self.assertIsNone(up.parse_version(""))
        self.assertTrue(up.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(up.is_newer("1.0.0", "1.0.0"))
        self.assertTrue(up.is_newer("1.0.1", "0"))


class ChangelogTests(unittest.TestCase):
    def test_changes_between_two_versions(self):
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "1.2.0", "1.3.0"), ["Third thing.", "Fourth thing."])
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "1.1.0", "1.3.0"),
                         ["1.3.0: Third thing.", "1.3.0: Fourth thing.", "1.2.0: Second thing."])
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "1.3.0", "1.3.0"), [])  # nothing newer than you have
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "1.4.0", "1.5.0"), [])
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "1.1.0", "1.2.0"), ["Second thing."])  # not past new
        self.assertEqual(up.changes_between(CHANGELOG_TEXT, "0", "1.1.0"), ["First thing."])

    def test_versions_compare_as_numbers(self):
        text = "## 1.10.0\n- ten\n\n## 1.9.0\n- nine\n\n## v1.2\n- two\n"
        self.assertEqual(up.changes_between(text, "1.9.0", "1.10.0"), ["ten"])
        self.assertEqual(up.changes_between(text, "1.1.0", "1.9.0"), ["1.9.0: nine", "1.2: two"])

    def test_a_long_changelog_is_cut_with_a_pointer_to_the_file(self):
        text = "## 1.9.0\n" + "".join(f"- line {n}\n" for n in range(8))
        self.assertEqual(up.changes_between(text, "1.8.0", "1.9.0", limit=3),
                         ["line 0", "line 1", "line 2", "and 5 more in CHANGELOG.md"])
        lines = up.changes_between(text, "1.8.0", "1.9.0")  # the default limit is 5
        self.assertEqual((len(lines), lines[-1]), (6, "and 3 more in CHANGELOG.md"))

    def test_exactly_the_limit_is_not_cut(self):
        text = "## 1.9.0\n" + "".join(f"- line {n}\n" for n in range(5))
        self.assertEqual(up.changes_between(text, "1.8.0", "1.9.0"), [f"line {n}" for n in range(5)])

    def test_text_that_is_not_a_changelog_gives_nothing(self):
        for text in ("", "no headings\n- a bullet\n", "## not a version\n- x\n", "- before any heading\n## 1.1.0\n"):
            self.assertEqual(up.changes_between(text, "1.0.0", "2.0.0"), [], repr(text))

    def test_whats_new_never_fails(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertEqual(up.whats_new(None, "1.0.0", "1.1.0"), [])
        self.assertEqual(up.whats_new(tmp, "1.0.0", "1.1.0"), [])  # no CHANGELOG.md there
        (tmp / "CHANGELOG.md").write_bytes(b"\xff\xfe## 1.1.0\n- not utf-8\n")
        self.assertEqual(up.whats_new(tmp, "1.0.0", "1.1.0"), [])
        (tmp / "CHANGELOG.md").unlink()
        (tmp / "CHANGELOG.md").mkdir()  # a folder where the file should be
        self.assertEqual(up.whats_new(tmp, "1.0.0", "1.1.0"), [])
        (tmp / "CHANGELOG.md").rmdir()
        (tmp / "CHANGELOG.md").write_text(CHANGELOG_TEXT, encoding="utf-8")
        self.assertEqual(up.whats_new(tmp, "1.2.0", "1.3.0"), ["Third thing.", "Fourth thing."])

    def test_the_real_changelog_has_an_entry_for_the_current_version(self):
        root = Path(__file__).resolve().parent.parent
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {version}", text.splitlines(), "add an entry for this version to CHANGELOG.md")
        self.assertTrue(up.changes_between(text, "0", version, limit=1000), "the entry has no bullet lines")


def git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
           "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return out.stdout


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "VERSION").write_text("1.0.0\n")
        (seed / "coach_core.py").write_text("VALUE = 1\n")
        (seed / "config.json").write_text('{"models": ["upstream"]}')
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "one")
        self.origin = self.tmp / "hex-1000-prompt-coach.git"
        git(self.tmp, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.app = self.tmp / "app"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.app))
        (seed / "VERSION").write_text("1.1.0\n")
        (seed / "coach_core.py").write_text("VALUE = 2\n")
        (seed / "CHANGELOG.md").write_text(CHANGELOG_TEXT)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "two")
        git(seed, "push", "-q", str(self.origin), "main")
        self.seed = seed
        self.kw = {
            "version_url": (seed / "VERSION").as_uri(),
            "zip_url": (self.tmp / "unused.zip").as_uri(),
            "origin_match": "hex-1000-prompt-coach",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pulls_when_clean(self):
        res = up.check_and_update(self.app, **self.kw)
        self.assertEqual((res.status, res.new), ("updated", "1.1.0"), res.message)
        self.assertEqual((self.app / "coach_core.py").read_text(), "VALUE = 2\n")

    def test_the_pull_brings_the_changelog(self):
        self.assertFalse((self.app / "CHANGELOG.md").exists())
        res = up.check_and_update(self.app, **self.kw)
        self.assertEqual(res.status, "updated", res.message)
        self.assertEqual(up.whats_new(self.app, res.old, res.new), ["First thing."])

    def test_config_edit_does_not_block_the_pull(self):
        (self.app / "config.json").write_text('{"models": ["mine"]}')
        res = up.check_and_update(self.app, **self.kw)
        self.assertEqual(res.status, "updated", res.message)
        self.assertEqual((self.app / "config.json").read_text(), '{"models": ["mine"]}')

    def test_local_code_edit_blocks_the_pull(self):
        (self.app / "coach_core.py").write_text("VALUE = 'mine'\n")
        res = up.check_and_update(self.app, **self.kw)
        self.assertEqual(res.status, "skipped")
        self.assertEqual((self.app / "coach_core.py").read_text(), "VALUE = 'mine'\n")

    def test_other_origin_is_left_alone(self):
        res = up.check_and_update(self.app, **{**self.kw, "origin_match": "someone-else/fork"})
        self.assertEqual(res.status, "skipped")
        self.assertEqual((self.app / "coach_core.py").read_text(), "VALUE = 1\n")


if __name__ == "__main__":
    unittest.main()
