"""Sidebar behaviour of the TUI, run headless with Textual's test pilot.

python -B -m unittest discover -s tests      (needs: pip install textual)
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import coach_core as core
    import coach_tui
except ImportError:  # textual is not installed
    coach_tui = None


@unittest.skipIf(coach_tui is None, "textual is not installed")
class SidebarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.contexts = self.tmp / "contexts"
        self.contexts.mkdir()
        for key, name in (("b-two", "Second Project"), ("a-one", "First Project")):
            (self.contexts / f"{key}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
        (self.contexts / "_template.md").write_text("---\nname: hidden\n---\n", encoding="utf-8")
        self._load_config = core.load_config
        self._state_file = coach_tui.STATE_FILE
        core.load_config = lambda path=None: {**core.DEFAULT_CONFIG, "contexts_dir": str(self.contexts)}
        coach_tui.STATE_FILE = self.tmp / "state.json"

    def tearDown(self):
        core.load_config = self._load_config
        coach_tui.STATE_FILE = self._state_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ids(self, app):
        lst = app.query_one("#projects")
        return [lst.get_option_at_index(i).id for i in range(lst.option_count)]

    async def test_layout_and_default(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertEqual(self.ids(app), [coach_tui.GENERAL_KEY, coach_tui.HEADER_KEY, "a-one", "b-two"])
            lst = app.query_one("#projects")
            self.assertEqual(str(lst.get_option_at_index(0).prompt), "General Context")
            self.assertTrue(lst.get_option_at_index(1).disabled)
            self.assertEqual(lst.highlighted, 0)
            self.assertIsNone(app.context)
            self.assertEqual(app.focused.id, "prompt")  # ready to type in the default context

    async def test_arrow_keys_skip_the_heading_and_pick_a_project(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("tab")
            await pilot.press("down")
            self.assertEqual(app.context.name, "First Project")
            await pilot.press("down")
            self.assertEqual(app.context.name, "Second Project")
            await pilot.press("up", "up")
            self.assertIsNone(app.context)  # back on General Context, never on the heading

    async def test_last_project_is_not_restored(self):
        (self.tmp / "state.json").write_text('{"project": "a-one"}', encoding="utf-8")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertIsNone(app.context)

    async def test_no_projects_means_no_heading(self):
        for f in self.contexts.glob("*.md"):
            f.unlink()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertEqual(self.ids(app), [coach_tui.GENERAL_KEY])


if __name__ == "__main__":
    unittest.main()
