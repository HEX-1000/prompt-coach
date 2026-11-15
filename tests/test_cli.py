"""The plain terminal version (prompt_coach.py) with a prepared model: translation output.

No test needs Ollama: core.chat_json returns prepared replies.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coach_core as core  # noqa: E402
import prompt_coach  # noqa: E402

STATS = core.CallStats(prompt_tokens=700, output_tokens=200, seconds=9.5, gen_seconds=7.0, num_ctx=8192)
TEXT = "Bu raporu kısalt, lütfen çok kısa olsun."
TRANSLATED = "Shorten this report, please keep it very short."
REPLY = {"understanding": "Raporu kısaltmamı istiyorsun.", "questions": [], "depth": "standard"}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = {**core.DEFAULT_CONFIG, "history_dir": str(self.tmp / "history"),
                    "contexts_dir": str(self.tmp / "contexts")}
        self.copied = []

    def run_cli(self, argv, translate):
        def chat(cfg, model, messages, schema, temperature):
            if "understanding" in schema["properties"]:
                return dict(REPLY), STATS
            parts = json.loads(messages[1]["content"])
            return {k: translate(v) for k, v in parts.items()}, STATS

        def fake_copy(text):
            self.copied.append(text)
            return True

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(core, "load_config", lambda path=None: self.cfg))
            stack.enter_context(mock.patch.object(core, "chat_json", chat))
            stack.enter_context(mock.patch.object(core, "copy_to_clipboard", fake_copy))
            stack.enter_context(mock.patch.object(core, "ensure_utf8_console", lambda: None))
            stack.enter_context(mock.patch("builtins.input", lambda prompt="": ""))
            stack.enter_context(mock.patch.object(sys, "argv", ["prompt_coach.py"] + argv))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = prompt_coach.main()
        return code, out.getvalue()

    def test_lang_translates_prints_and_copies_the_translation(self):
        code, out = self.run_cli([TEXT, "--lang", "English"], lambda v: TRANSLATED)
        self.assertEqual(code, 0)
        self.assertIn("translated to English: 1 of 1 parts passed the checks", out)
        self.assertTrue(out.rstrip().endswith(TRANSLATED))
        self.assertEqual(self.copied, [TRANSLATED])

    def test_a_part_kept_in_the_source_language_is_reported(self):
        code, out = self.run_cli([TEXT, "--lang", "English"], lambda v: "Shorten.")
        self.assertEqual(code, 0)
        self.assertIn("part 1 kept in the original language", out)
        self.assertTrue(out.rstrip().endswith(TEXT))
        self.assertEqual(self.copied, [TEXT])

    def test_same_language_does_not_translate(self):
        code, out = self.run_cli([TEXT], lambda v: self.fail("no translation call expected"))
        self.assertEqual(code, 0)
        self.assertNotIn("Translating", out)
        self.assertEqual(self.copied, [TEXT])

    def test_the_text_language_as_output_does_not_translate(self):
        code, out = self.run_cli([TEXT, "--lang", "Turkish"], lambda v: self.fail("no translation call expected"))
        self.assertEqual(code, 0)
        self.assertNotIn("Translating", out)

    def test_a_translation_error_falls_back_to_the_original(self):
        def boom(part):
            raise core.CoachError("Ollama is not reachable")

        code, out = self.run_cli([TEXT, "--lang", "English"], boom)
        self.assertEqual(code, 0)
        self.assertIn("Showing the original prompt instead", out)
        self.assertEqual(self.copied, [TEXT])

    # ---- project context block

    BRIEF = "Demo exports weekly reports."

    def project(self):
        folder = self.tmp / "contexts"
        folder.mkdir()
        (folder / "demo.md").write_text(
            f"---\nname: Demo\n---\n# Demo\n\n{self.BRIEF}\n\n## Glossary\n- E1: first export\n", encoding="utf-8")

    def test_the_project_brief_is_attached_and_reported(self):
        self.project()
        code, out = self.run_cli([TEXT, "--project", "demo"], lambda v: self.fail("no translation call expected"))
        self.assertEqual(code, 0)
        self.assertEqual(self.copied, [f"{TEXT}\n\nProje bağlamı (Demo):\n{self.BRIEF}"])
        self.assertIn(f"project context attached: demo.md, {len(self.BRIEF)} characters (--no-brief leaves it out)", out)

    def test_no_brief_leaves_the_block_out(self):
        self.project()
        code, out = self.run_cli([TEXT, "--project", "demo", "--no-brief"],
                                 lambda v: self.fail("no translation call expected"))
        self.assertEqual(code, 0)
        self.assertEqual(self.copied, [TEXT])
        self.assertNotIn("project context attached", out)
        self.assertNotIn("Proje bağlamı", out)

    def test_the_translated_prompt_keeps_the_brief_as_written(self):
        self.project()
        code, out = self.run_cli([TEXT, "--project", "demo", "--lang", "English"], lambda v: TRANSLATED)
        self.assertEqual(code, 0)
        self.assertEqual(self.copied, [f"{TRANSLATED}\n\nProject context (Demo):\n{self.BRIEF}"])


if __name__ == "__main__":
    unittest.main()
