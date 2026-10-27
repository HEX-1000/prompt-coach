"""First-run guidance: the empty prompt box and the Ollama / model notices.

python -B -m unittest discover -s tests      (needs: pip install textual)
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import coach_core as core
    import coach_tui
except ImportError:  # textual is not installed
    coach_tui = None


def notices(app):
    return [(n.severity, n.message) for n in app._notifications]


@unittest.skipIf(coach_tui is None, "textual is not installed")
class FirstRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "contexts").mkdir()
        self._load_config = core.load_config
        self._state_file = coach_tui.STATE_FILE
        self._models = core.ollama_models
        self._installed = core.ollama_installed
        self._notes = list(coach_tui.STARTUP_NOTES)
        coach_tui.STARTUP_NOTES.clear()
        self.cfg = {
            **core.DEFAULT_CONFIG,
            "contexts_dir": str(self.tmp / "contexts"),
            "models": ["qwen3.5:9b", "qwen3.5:4b"],
        }
        core.load_config = lambda path=None: dict(self.cfg)
        core.ollama_installed = lambda: True  # whatever this computer has, Ollama counts as installed
        coach_tui.STATE_FILE = self.tmp / "state.json"

    def tearDown(self):
        core.load_config = self._load_config
        core.ollama_models = self._models
        core.ollama_installed = self._installed
        coach_tui.STATE_FILE = self._state_file
        coach_tui.STARTUP_NOTES[:] = self._notes
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ollama_has(self, *names):
        core.ollama_models = lambda cfg: list(names)

    def ollama_down(self):
        def fail(cfg):
            raise core.CoachError("connection refused")
        core.ollama_models = fail

    async def test_empty_prompt_box_says_what_to_do(self):
        self.ollama_has("qwen3.5:9b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            box = app.query_one("#prompt")
            self.assertEqual(box.text, "")
            self.assertIn("ctrl+s", box.placeholder)
            self.assertIn("tab", box.placeholder)

    async def test_ollama_down_gives_an_error_and_a_red_dot(self):
        self.ollama_down()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            self.assertIs(app.ollama_ok, False)
            errors = [m for sev, m in notices(app) if sev == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("not reachable", errors[0])
            self.assertIn("ctrl+s", errors[0])

    async def test_missing_model_names_the_pull_command(self):
        self.ollama_has("something-else:1b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            self.assertIs(app.ollama_ok, True)
            warnings = [m for sev, m in notices(app) if sev == "warning"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("ollama pull qwen3.5:9b", warnings[0])
            self.assertNotIn("f3", warnings[0])  # no other configured model to switch to

    async def test_missing_default_suggests_the_installed_model(self):
        self.ollama_has("qwen3.5:4b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            warnings = [m for sev, m in notices(app) if sev == "warning"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("ollama pull qwen3.5:9b", warnings[0])
            self.assertIn("f3", warnings[0])
            self.assertIn("qwen3.5:4b", warnings[0])

    async def test_no_nag_about_other_models_when_the_selected_one_is_installed(self):
        self.ollama_has("qwen3.5:9b")  # the 4b model is configured but not installed
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            self.assertIs(app.ollama_ok, True)
            self.assertEqual(notices(app), [])

    async def test_dot_turns_green_after_ollama_is_started_late(self):
        self.ollama_down()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            self.assertIs(app.ollama_ok, False)
            count = len(notices(app))
            self.ollama_has("qwen3.5:9b")
            app._recheck_ollama()
            await pilot.pause(0.3)
            self.assertIs(app.ollama_ok, True)
            self.assertEqual(len(notices(app)), count)  # the recheck is silent

    async def wait_for(self, pilot, condition, what, timeout=10.0):
        """Poll until condition() holds. A fixed pause is a guess, and a slow machine needs longer."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.05)

    async def test_ollama_not_installed_points_to_the_download(self):
        core.ollama_installed = lambda: False
        self.ollama_down()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.wait_for(pilot, lambda: app.ollama_ok is not None, "the Ollama check")
            self.assertIs(app.ollama_ok, False)
            errors = [m for sev, m in notices(app) if sev == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("is not installed", errors[0])
            self.assertIn("https://ollama.com/download", errors[0])
            self.assertIn("No thanks, I'll use Ollama locally", errors[0])
            self.assertNotIn("not reachable", errors[0])

    async def test_installed_but_not_running_is_not_called_missing(self):
        core.ollama_installed = lambda: True
        self.ollama_down()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.wait_for(pilot, lambda: app.ollama_ok is not None, "the Ollama check")
            errors = [m for sev, m in notices(app) if sev == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("not reachable", errors[0])
            self.assertNotIn("ollama.com", errors[0])
            self.assertNotIn("not installed", errors[0])

    async def test_a_remote_ollama_is_never_called_not_installed(self):
        core.ollama_installed = lambda: False  # true of this computer, and beside the point
        self.cfg["ollama_url"] = "http://192.168.1.20:11434"
        self.ollama_down()
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.wait_for(pilot, lambda: app.ollama_ok is not None, "the Ollama check")
            errors = [m for sev, m in notices(app) if sev == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("not reachable at http://192.168.1.20:11434", errors[0])
            self.assertNotIn("not installed", errors[0])

    async def test_an_answering_ollama_is_never_called_not_installed(self):
        core.ollama_installed = lambda: False  # for example Ollama installed in a folder nobody looks in
        self.ollama_has("qwen3.5:9b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.wait_for(pilot, lambda: app.ollama_ok is not None, "the Ollama check")
            self.assertIs(app.ollama_ok, True)
            self.assertEqual(notices(app), [])

    async def test_f1_opens_the_key_sheet_while_typing_but_question_mark_is_text(self):
        self.ollama_has("qwen3.5:9b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertEqual(app.focused.id, "prompt")
            await pilot.press("question_mark")
            self.assertEqual(app.query_one("#prompt").text, "?")  # typed, no help sheet
            self.assertNotIsInstance(app.screen, coach_tui.HelpScreen)
            await pilot.press("f1")
            self.assertIsInstance(app.screen, coach_tui.HelpScreen)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, coach_tui.HelpScreen)

    async def test_question_mark_opens_help_outside_a_text_box(self):
        self.ollama_has("qwen3.5:9b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("tab")  # focus moves to the project list
            await pilot.press("question_mark")
            self.assertIsInstance(app.screen, coach_tui.HelpScreen)

    async def test_ctrl_q_quits(self):
        self.ollama_has("qwen3.5:9b")
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+q")
            await pilot.pause()
            self.assertTrue(app._exit)

    def test_footer_hints_name_f1_and_quit_and_fit_80_columns(self):
        for step, pairs in coach_tui.FOOTER_HINTS.items():
            keys = [k for k, _ in pairs]
            self.assertIn("f1", keys, step)
            self.assertIn("ctrl+q", keys, step)
            self.assertNotIn("?", keys, step)
            self.assertLessEqual(len(coach_tui.hint_line(pairs).plain), 80 - 4, step)


@unittest.skipIf(coach_tui is None, "textual is not installed")
class OllamaNoticeTests(unittest.TestCase):
    """The three things that can be wrong with Ollama, without starting the interface."""

    CFG = {"ollama_url": "http://localhost:11434", "models": ["qwen3.5:9b", "qwen3.5:4b"]}

    def notice(self, names, installed=True, model="qwen3.5:9b", **cfg):
        return coach_tui.ollama_notice({**self.CFG, **cfg}, model, names, installed)

    def test_everything_in_place_gives_no_notice(self):
        self.assertIsNone(self.notice(["qwen3.5:9b"]))
        self.assertIsNone(self.notice(["qwen3.5:9b", "other"], installed=False))

    def test_not_installed(self):
        text, severity = self.notice(None, installed=False)
        self.assertEqual(severity, "error")
        self.assertIn("Ollama is not installed", text)
        self.assertIn("https://ollama.com/download", text)
        self.assertIn("No thanks, I'll use Ollama locally", text)

    def test_installed_but_not_running(self):
        text, severity = self.notice(None, installed=True)
        self.assertEqual(severity, "error")
        self.assertIn("not reachable at http://localhost:11434", text)
        self.assertNotIn("ollama.com", text)

    def test_model_missing(self):
        text, severity = self.notice(["something-else:1b"])
        self.assertEqual(severity, "warning")
        self.assertIn("ollama pull qwen3.5:9b", text)
        self.assertNotIn("f3", text)
        text, _ = self.notice(["qwen3.5:4b"])
        self.assertIn("f3", text)
        self.assertIn("qwen3.5:4b", text)

    def test_the_three_notices_differ(self):
        texts = {self.notice(None, installed=False)[0], self.notice(None)[0], self.notice([])[0]}
        self.assertEqual(len(texts), 3)

    def test_only_a_local_address_can_be_not_installed(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"):
            self.assertIn("not installed", self.notice(None, installed=False, ollama_url=url)[0], url)
        for url in ("http://192.168.1.20:11434", "http://ollama.example.com:11434"):
            self.assertIn("not reachable", self.notice(None, installed=False, ollama_url=url)[0], url)


if __name__ == "__main__":
    unittest.main()
