"""The write, clarify and result screens of the TUI, run headless with Textual's test pilot.

No test needs Ollama: core.chat_json returns a prepared reply, and the clipboard function is replaced.

python -B -m unittest discover -s tests      (needs: pip install textual)
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import coach_core as core
    import coach_tui
except ImportError:  # textual is not installed
    coach_tui = None

STATS = None if coach_tui is None else core.CallStats(
    prompt_tokens=700, output_tokens=200, seconds=9.5, gen_seconds=7.0, num_ctx=8192)
TEXT = "Summarise the weekly report for E7 in five lines."
REPLY = {"understanding": "You want a five line summary of the weekly report.",
         "questions": [], "depth": "standard"}
CONTEXT = "---\nname: Demo\n---\n# Demo\n\n## Glossary\n- E1: first export\n"


@unittest.skipIf(coach_tui is None, "textual is not installed")
class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "contexts").mkdir()
        self.project = self.tmp / "contexts" / "demo.md"
        self.project.write_text(CONTEXT, encoding="utf-8")
        cfg = {**core.DEFAULT_CONFIG, "contexts_dir": str(self.tmp / "contexts"),
               "history_dir": str(self.tmp / "history")}
        self.copied = []

        def fake_copy(text):
            self.copied.append(text)
            return True

        patches = [
            mock.patch.object(core, "load_config", lambda path=None: cfg),
            mock.patch.object(core, "ollama_models", lambda c: list(cfg["models"])),
            mock.patch.object(core, "chat_json", lambda *a, **k: (dict(REPLY), STATS)),
            mock.patch.object(core, "copy_to_clipboard", fake_copy),
            mock.patch.object(coach_tui, "STATE_FILE", self.tmp / "state.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def settle(self, app, pilot, step):
        """Wait for the model worker, then for the screen change."""
        await app.workers.wait_for_complete()
        for _ in range(20):
            await pilot.pause()
            if app.step == step:
                return
        self.fail(f"the app stayed on {app.step!r}, expected {step!r}")

    async def to_clarify(self, app, pilot, text=TEXT, project=True):
        if project:
            app._set_context("demo")
        app.query_one("#prompt").load_text(text)
        await pilot.press("ctrl+s")
        await self.settle(app, pilot, "clarify")

    def records(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in (self.tmp / "history").glob("*.json")]

    async def test_clarify_shows_the_restatement_and_the_fields(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            self.assertIn("five line summary", str(app.query_one("#understanding").render()))
            self.assertEqual(len(app.answer_inputs), 1)  # the term check for E7
            self.assertEqual(app.answer_inputs[0].placeholder, coach_tui.ANSWER_PLACEHOLDER)
            self.assertEqual(app.query_one("#note").placeholder, coach_tui.NOTE_PLACEHOLDER)
            self.assertEqual(len(app.query("#repaired")), 0)
            self.assertEqual(len(app.query("#correction")), 0)

    async def test_ctrl_l_writes_a_bang_and_moves_on(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            self.assertIs(app.focused, app.answer_inputs[0])
            await pilot.press("ctrl+l")
            self.assertEqual(app.answer_inputs[0].value, "!")
            self.assertEqual(app.focused.id, "note")
            await pilot.press("ctrl+l")  # not an answer field: nothing is written
            self.assertEqual(app.query_one("#note").value, "")

    async def test_enter_moves_on_and_builds_from_the_note_field(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            await pilot.press("enter")
            self.assertEqual(app.focused.id, "note")
            self.assertEqual(app.step, "clarify")
            await pilot.press("enter")
            self.assertEqual(app.step, "result")

    async def test_ctrl_s_builds_and_copies_once(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            app.answer_inputs[0].value = "the export job"
            app.query_one("#note").value = "Keep it neutral."
            await pilot.press("ctrl+s")
            self.assertEqual(app.step, "result")
            final = app.query_one("#final").text
            self.assertTrue(final.startswith(TEXT))
            self.assertIn("the export job", final)
            self.assertIn("Keep it neutral.", final)
            self.assertEqual(self.copied, [final])
            (record,) = self.records()
            self.assertEqual(record["format"], 2)
            self.assertEqual(record["copied"]["text"], final)
            self.assertEqual(record["rounds"][0]["note"], "Keep it neutral.")

    async def test_y_copies_the_edited_text_and_rewrites_the_record(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            await pilot.press("ctrl+s")
            app.query_one("#final").load_text("edited prompt")
            await pilot.press("y")
            self.assertEqual(len(self.copied), 2)
            self.assertEqual(self.copied[-1], "edited prompt")
            (record,) = self.records()  # still one file
            self.assertEqual(record["copied"]["text"], "edited prompt")

    async def test_g_writes_the_new_term_to_the_context_file(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            app.answer_inputs[0].value = "the export job"
            await pilot.press("ctrl+s")
            meta = str(app.query_one("#result-meta").render())
            self.assertIn("new terms: E7. g saves them to the glossary of Demo", meta)
            await pilot.press("g")
            self.assertIn("E7: the export job", self.project.read_text(encoding="utf-8"))
            self.assertNotIn("new terms", str(app.query_one("#result-meta").render()))

    async def test_no_new_terms_line_without_a_project(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot, project=False)
            app.answer_inputs[0].value = "the export job"
            await pilot.press("ctrl+s")
            self.assertNotIn("new terms", str(app.query_one("#result-meta").render()))

    async def test_esc_goes_back_one_screen(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot)
            await pilot.press("ctrl+s")
            await pilot.press("escape")
            self.assertEqual(app.step, "clarify")
            await pilot.press("escape")
            self.assertEqual(app.step, "write")

    # ---- translation

    TR_TEXT = "Bu raporu kısalt, lütfen çok kısa olsun."
    TRANSLATED = "Shorten this report, please keep it very short."

    def fake_chat(self, gate=None, fail_first=False):
        """Stage 1 gets REPLY. A translation call waits for gate, and the first one can fail."""
        state = {"failed": False}

        def chat(cfg, model, messages, schema, temperature):
            if "understanding" in schema["properties"]:
                return dict(REPLY), STATS
            if gate is not None:
                gate.wait(10)
            if fail_first and not state["failed"]:
                state["failed"] = True
                raise core.CoachError("Ollama is not reachable")
            parts = json.loads(messages[1]["content"])
            return {k: self.TRANSLATED for k in parts}, STATS

        return mock.patch.object(core, "chat_json", chat)

    async def to_translating(self, app, pilot):
        app.out_lang = "English"
        await self.to_clarify(app, pilot, text=self.TR_TEXT)
        await pilot.press("ctrl+s")
        for _ in range(40):
            await pilot.pause()
            if app.busy_label.startswith("translating"):
                return
        self.fail("the translation did not start")

    async def test_the_box_is_read_only_during_the_call_then_the_translation_replaces_the_text(self):
        import threading

        gate = threading.Event()
        with self.fake_chat(gate):
            app = coach_tui.CoachApp()
            async with app.run_test(size=(120, 36)) as pilot:
                await self.to_translating(app, pilot)
                final = app.query_one("#final")
                self.assertEqual(app.step, "result")
                self.assertTrue(final.read_only)
                self.assertEqual(final.text, self.TR_TEXT)
                self.assertEqual(self.copied, [])          # nothing is copied while it runs
                self.assertFalse(app.query_one("#main").loading)
                gate.set()
                await self.settle(app, pilot, "result")
                self.assertFalse(final.read_only)
                self.assertEqual(final.text, self.TRANSLATED)
                self.assertEqual(self.copied, [self.TRANSLATED])   # copied once, the translation
                meta = str(app.query_one("#result-meta").render())
                self.assertIn("translated to English: 1 of 1 parts passed the checks", meta)
                rec = self.records()[0]
                self.assertEqual(rec["translation"]["text"], self.TRANSLATED)
                self.assertEqual(rec["copied"]["text"], self.TRANSLATED)

    async def test_y_copies_the_original_while_the_translation_runs(self):
        import threading

        gate = threading.Event()
        with self.fake_chat(gate):
            app = coach_tui.CoachApp()
            async with app.run_test(size=(120, 36)) as pilot:
                await self.to_translating(app, pilot)
                await pilot.press("y")
                self.assertEqual(self.copied, [self.TR_TEXT])
                gate.set()
                await self.settle(app, pilot, "result")

    async def test_o_switches_between_the_views_and_keeps_edits(self):
        with self.fake_chat():
            app = coach_tui.CoachApp()
            async with app.run_test(size=(120, 36)) as pilot:
                await self.to_translating(app, pilot)
                await self.settle(app, pilot, "result")
                final = app.query_one("#final")
                final.load_text("my edit of the translation")
                await pilot.press("o")
                self.assertEqual(final.text, self.TR_TEXT)
                final.load_text("my edit of the original")
                await pilot.press("o")
                self.assertEqual(final.text, "my edit of the translation")
                await pilot.press("o")
                self.assertEqual(final.text, "my edit of the original")
                self.assertEqual(len(self.copied), 1)       # switching never copies

    async def test_a_failed_translation_leaves_the_original_and_t_tries_again(self):
        with self.fake_chat(fail_first=True):
            app = coach_tui.CoachApp()
            async with app.run_test(size=(120, 36)) as pilot:
                await self.to_translating(app, pilot)
                await self.settle(app, pilot, "result")
                final = app.query_one("#final")
                self.assertFalse(final.read_only)
                self.assertEqual(final.text, self.TR_TEXT)
                self.assertIn("translation failed", str(app.query_one("#result-meta").render()))
                self.assertEqual(self.copied, [])
                await pilot.press("t")
                await self.settle(app, pilot, "result")
                self.assertEqual(final.text, self.TRANSLATED)
                self.assertEqual(self.copied, [self.TRANSLATED])

    async def test_no_translation_without_a_different_output_language(self):
        app = coach_tui.CoachApp()
        async with app.run_test(size=(120, 36)) as pilot:
            await self.to_clarify(app, pilot, text=self.TR_TEXT)
            await pilot.press("ctrl+s")
            final = app.query_one("#final")
            self.assertFalse(final.read_only)
            self.assertEqual(self.copied, [self.TR_TEXT])
            await pilot.press("o")                       # no translation: nothing to switch to
            self.assertEqual(final.text, self.TR_TEXT)

    async def test_t_with_the_same_language_drops_the_translation(self):
        with self.fake_chat():
            app = coach_tui.CoachApp()
            async with app.run_test(size=(120, 36)) as pilot:
                await self.to_translating(app, pilot)
                await self.settle(app, pilot, "result")
                app.out_lang = app.session.out_lang = "same"
                await pilot.press("t")
                self.assertEqual(app.query_one("#final").text, self.TR_TEXT)
                self.assertIsNone(app.session.translation)
                self.assertIsNone(self.records()[0]["translation"])


@unittest.skipIf(coach_tui is None, "textual is not installed")
class FooterTests(unittest.TestCase):
    def test_clarify_footer_has_the_new_keys_and_no_ctrl_b(self):
        keys = [k for k, _ in coach_tui.FOOTER_HINTS["clarify"]]
        self.assertEqual(keys, ["ctrl+s", "ctrl+l", "esc", "f1", "ctrl+q"])
        self.assertNotIn("ctrl+b", coach_tui.HELP_TEXT)

    def test_footers_fit_80_columns(self):
        for step, pairs in coach_tui.FOOTER_HINTS.items():
            self.assertLessEqual(len(coach_tui.hint_line(pairs).plain), 80 - 4, step)


if __name__ == "__main__":
    unittest.main()
