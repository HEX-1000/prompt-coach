"""Tests for coach_core: config, targets, context files, the session and the history record.

No test needs Ollama: core.chat_json is replaced by a function that returns a prepared reply.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coach_core as core  # noqa: E402

EM = chr(0x2014)
STATS = core.CallStats(prompt_tokens=700, output_tokens=200, seconds=9.5, gen_seconds=7.0, num_ctx=8192)


def reply(understanding="You want Claude to shorten the text.", questions=(), depth="standard"):
    return {"understanding": understanding, "questions": list(questions), "depth": depth}


def q(question, kind="fact", missing="a fact"):
    return {"missing": missing, "question": question, "kind": kind}


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = {**core.DEFAULT_CONFIG, "history_dir": str(self.tmp / "history"),
                    "contexts_dir": str(self.tmp / "contexts")}

    def write(self, name, text, binary=False):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def session(self, text, data, context=None, cfg=None, **kwargs):
        s = core.CoachSession(cfg or self.cfg, text, context, "test-model", **kwargs)
        with mock.patch.object(core, "chat_json", return_value=(data, STATS)) as m:
            s.understand()
        self.chat = m
        return s


class UnknownCodesStillImportable(unittest.TestCase):
    def test_names_moved_to_coach_text_stay_in_core(self):
        import coach_text
        self.assertIs(core.unknown_codes, coach_text.unknown_codes)
        self.assertIs(core.CODE_RE, coach_text.CODE_RE)
        self.assertEqual(core.unknown_codes("Check E7 and MP4.", ""), ["E7"])


class ContextFileTests(TempDirCase):
    def test_front_matter_with_name_and_list_includes(self):
        path = self.write("c.md", "---\nname: My Project\ninclude:\n  - a.md\n  - b.md\n---\nbody here\n")
        ctx = core.parse_context_file(path)
        self.assertEqual((ctx.key, ctx.name), ("c", "My Project"))
        self.assertEqual([p.name for p in ctx.includes], ["a.md", "b.md"])
        self.assertEqual(ctx.body.strip(), "body here")

    def test_include_on_one_line(self):
        path = self.write("c.md", "---\ninclude: other.md\n---\nbody\n")
        ctx = core.parse_context_file(path)
        self.assertEqual([p.name for p in ctx.includes], ["other.md"])
        self.assertEqual(ctx.name, "c")

    def test_no_front_matter(self):
        path = self.write("plain.md", "just text\n")
        ctx = core.parse_context_file(path)
        self.assertEqual((ctx.name, ctx.includes, ctx.body), ("plain", [], "just text\n"))


class ConfigTests(TempDirCase):
    def load(self, data):
        return core.load_config(self.write("config.json", json.dumps(data)))

    def test_new_defaults(self):
        cfg = core.load_config(self.tmp / "missing.json")
        self.assertEqual((cfg["num_ctx"], cfg["max_questions"], cfg["target"]), (8192, 3, "claude"))
        self.assertEqual(cfg["brief_max_chars"], 2000)
        self.assertNotIn("rules_file", cfg)
        self.assertNotIn("temperature_compose", cfg)

    def test_legacy_values_count_as_not_set(self):
        cfg = self.load({"num_ctx": 16384, "max_questions": 5, "models": ["mine"]})
        self.assertEqual((cfg["num_ctx"], cfg["max_questions"]), (8192, 3))
        self.assertEqual(cfg["models"], ["mine"])

    def test_config_version_2_keeps_legacy_values(self):
        cfg = self.load({"config_version": 2, "num_ctx": 16384, "max_questions": 5})
        self.assertEqual((cfg["num_ctx"], cfg["max_questions"]), (16384, 5))

    def test_other_values_are_kept(self):
        cfg = self.load({"num_ctx": 4096, "max_questions": 2})
        self.assertEqual((cfg["num_ctx"], cfg["max_questions"]), (4096, 2))

    def test_shipped_config_json_gets_the_new_defaults(self):
        cfg = core.load_config()
        self.assertEqual((cfg["num_ctx"], cfg["max_questions"]), (8192, 3))

    def test_unknown_keys_are_ignored(self):
        cfg = self.load({"rules_file": "x.md", "whatever": 1})
        self.assertEqual(cfg["whatever"], 1)


class TargetTests(unittest.TestCase):
    def test_default_is_the_claude_file(self):
        t = core.load_target({"target": "claude"})
        self.assertEqual(t["name"], "Claude")
        self.assertEqual(t["depths"]["deep"], {"model": "Opus", "effort": "high"})

    def test_object_in_the_config(self):
        spec = {"name": "Other", "depths": {d: {"model": d.upper()} for d in core.DEPTHS}}
        self.assertEqual(core.load_target({"target": spec})["name"], "Other")

    def test_missing_file_falls_back(self):
        self.assertIs(core.load_target({"target": "no-such-target"}), core.BUILTIN_TARGET)

    def test_path_like_names_fall_back(self):
        for name in ("../config", "a/b", "..", ""):
            self.assertIs(core.load_target({"target": name}), core.BUILTIN_TARGET, name)

    def test_broken_object_falls_back(self):
        for spec in ({"name": "X"}, {"name": "X", "depths": {"light": {"model": "a"}}}, 5):
            self.assertIs(core.load_target({"target": spec}), core.BUILTIN_TARGET)

    def test_builtin_has_no_effort(self):
        for d in core.DEPTHS:
            self.assertNotIn("effort", core.BUILTIN_TARGET["depths"][d])


class ContextBudgetTests(TempDirCase):
    def context(self, size):
        inc = self.write("big.md", "x" * size)
        path = self.write("c.md", "---\ninclude: big.md\n---\n## Glossary\n- E1: first\n")
        return core.parse_context_file(path), inc

    def test_small_context_is_not_cut(self):
        ctx, _ = self.context(500)
        s = core.CoachSession(self.cfg, "Short text.", ctx, "m")
        self.assertEqual(s.warnings, [])
        self.assertIn("x" * 500, s.context_text)

    def test_long_include_is_cut_with_a_warning(self):
        ctx, _ = self.context(30000)
        cfg = {**self.cfg, "include_max_chars": 30000}
        s = core.CoachSession(cfg, "Short text.", ctx, "m")
        self.assertEqual(len(s.warnings), 1)
        self.assertIn("to fit num_ctx 8192", s.warnings[0])
        self.assertIn("raise num_ctx in config.json", s.warnings[0])
        budget = int((8192 - core.OUTPUT_RESERVE) * core.CHARS_PER_TOKEN)
        total = len(s._system(s.context_text)) + len(s._user_message())
        self.assertLessEqual(total, budget)
        self.assertGreater(total, budget - 50)
        self.assertTrue(s.context_text.startswith("## Glossary"))

    def test_cut_never_hides_the_glossary(self):
        ctx, _ = self.context(30000)
        s = core.CoachSession({**self.cfg, "include_max_chars": 30000}, "Short text.", ctx, "m")
        self.assertIn("- E1: first", s.context_text)

    def test_text_too_long_raises(self):
        with self.assertRaises(core.CoachError) as cm:
            core.CoachSession(self.cfg, "word " * 6000, None, "m")
        self.assertIn("too long", str(cm.exception))
        self.assertIn("8192", str(cm.exception))

    def test_raised_num_ctx_makes_room(self):
        s = core.CoachSession({**self.cfg, "num_ctx": 16384}, "word " * 6000, None, "m")
        self.assertEqual(s.warnings, [])


class UnderstandTests(TempDirCase):
    def test_choice_questions_are_dropped(self):
        s = self.session("Pick a license for me.", reply(questions=[
            q("Which license do you want?", kind="choice"), q("Is the repo public?")]))
        self.assertEqual([x.question for x in s.round.questions], ["Is the repo public?"])

    def test_empty_questions_are_dropped(self):
        s = self.session("Text.", reply(questions=[q("  "), q("Real one?")]))
        self.assertEqual([x.question for x in s.round.questions], ["Real one?"])

    def test_limit_is_max_questions(self):
        s = self.session("Text.", reply(questions=[q(f"Question {i}?") for i in range(6)]))
        self.assertEqual(len(s.round.questions), 3)
        self.assertEqual(s.round.questions[0].question, "Question 0?")

    def test_dropping_choices_happens_before_the_limit(self):
        qs = [q("c1?", "choice"), q("c2?", "choice"), q("f1?"), q("f2?"), q("f3?"), q("f4?")]
        s = self.session("Text.", reply(questions=qs))
        self.assertEqual([x.question for x in s.round.questions], ["f1?", "f2?", "f3?"])

    def test_missing_line_is_kept_but_not_the_kind(self):
        s = self.session("Text.", reply(questions=[q("Which file?", missing="the file name")]))
        one = s.round.questions[0]
        self.assertEqual((one.missing, one.source, one.answer), ("the file name", "model", ""))
        self.assertFalse(hasattr(one, "kind"))

    def test_term_checks_come_first_and_are_capped(self):
        text = "Compare E1 E2 E3 E4 E5 E6 E7 with my notes."
        s = self.session(text, reply(questions=[q("Which notes?")]))
        qs = s.round.questions
        self.assertEqual([x.term for x in qs[:5]], ["E1", "E2", "E3", "E4", "E5"])
        self.assertTrue(all(x.source == "term-check" for x in qs[:5]))
        self.assertEqual(qs[5].question, "Which notes?")
        self.assertEqual(s.round.more_terms, ["E6", "E7"])

    def test_code_the_model_already_asked_about_is_not_asked_twice(self):
        s = self.session("What is E1 in my notes?", reply(questions=[q("What does E1 stand for?")]))
        self.assertEqual([x.source for x in s.round.questions], ["model"])

    def test_term_check_follows_the_text_language(self):
        s = self.session("Bu E1 deneyini bana açıkla, lütfen çok kısa olsun.", reply())
        self.assertEqual(s.text_lang, "tr")
        self.assertIn("neyi ifade ediyor", s.round.questions[0].question)
        s = self.session("Please explain this E1 experiment to me, and keep it short.", reply())
        self.assertIn("refer to here", s.round.questions[0].question)

    def test_series_and_glossary_cover_codes(self):
        path = self.write("c.md", "## Glossary\n- A02, A03 ...: episode projects\n- E1: first run\n")
        ctx = core.parse_context_file(path)
        s = self.session("Look at A07 and E1.", reply(), context=ctx)
        self.assertEqual(s.round.questions, [])

    def test_em_dash_replaced_unless_the_text_has_one(self):
        data = reply(understanding=f"You want a summary {EM} short.", questions=[q(f"Which file {EM} which?")])
        s = self.session("Summarise.", data)
        self.assertEqual(s.round.understanding, "You want a summary, short.")
        self.assertEqual(s.round.questions[0].question, "Which file, which?")
        s = self.session(f"Summarise {EM} please.", data)
        self.assertIn(EM, s.round.understanding)

    def test_bad_depth_becomes_standard(self):
        self.assertEqual(self.session("Text.", reply(depth="extreme")).round.depth, "standard")
        self.assertEqual(self.session("Text.", reply(depth="deep")).round.depth, "deep")

    def test_malformed_questions_do_not_crash(self):
        s = self.session("Text.", {"understanding": "x", "questions": ["oops", None, q("Fine?")], "depth": "light"})
        self.assertEqual([x.question for x in s.round.questions], ["Fine?"])

    def test_one_model_call_with_the_options_of_the_config(self):
        self.session("Text.", reply())
        self.assertEqual(self.chat.call_count, 1)
        cfg, model, messages, schema, temperature = self.chat.call_args.args
        self.assertEqual(model, "test-model")
        self.assertEqual(temperature, 0.3)
        self.assertEqual(list(schema["properties"]), ["understanding", "questions", "depth"])
        self.assertEqual(schema["properties"]["questions"]["items"]["required"], ["missing", "question", "kind"])
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertTrue(messages[1]["content"].startswith("THE USER'S TEXT:\n<<<\nText."))

    def test_system_prompt_names_the_target_and_the_limit(self):
        self.session("Text.", reply())
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("about to send to Claude", system)
        self.assertIn("At most 3 questions", system)
        self.assertIn("PROJECT CONTEXT: none selected", system)
        self.assertNotIn("WRITING RULES", system)

    def test_system_prompt_language_and_address_note(self):
        self.session("Bu metni kısalt, lütfen çok kısa olsun.", reply())
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("in Turkish.", system)
        self.assertIn('"sen" verb forms', system)
        self.session("Please make this text shorter and keep the meaning.", reply())
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("in English.", system)
        self.assertNotIn('"sen"', system)
        self.session("???", reply())
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("in the language of the user's text.", system)

    def test_project_context_is_in_the_system_prompt(self):
        ctx = core.parse_context_file(self.write("c.md", "---\nname: Proj\n---\n## Glossary\n- E1: first\n"))
        self.session("Text.", reply(), context=ctx)
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("PROJECT CONTEXT: Proj\n<context>", system)
        self.assertIn("- E1: first", system)


class BuildTests(TempDirCase):
    def test_build_makes_no_model_call(self):
        s = self.session("Shorten this text.", reply(questions=[q("How short?")]))
        s.round.questions[0].answer = "one page"
        with mock.patch.object(core, "chat_json", side_effect=AssertionError("no call expected")):
            res = s.build()
        self.assertTrue(res.final_prompt.startswith("Shorten this text."))
        self.assertIn("one page", res.final_prompt)

    def test_build_before_understand_raises(self):
        s = core.CoachSession(self.cfg, "Text.", None, "m")
        with self.assertRaises(core.CoachError):
            s.build()

    def test_states_of_questions_and_the_note(self):
        s = self.session("Pick a license and shorten the README.", reply(questions=[
            q("How long?"), q("Which license do you prefer?"), q("Which README?")]))
        a, b, c = s.round.questions
        a.answer, b.answer, c.answer = "two pages", "!", ""
        s.set_note("  The repo is public.  ")
        res = s.build()
        text = res.final_prompt
        self.assertTrue(text.startswith("Pick a license and shorten the README.\n\nNote: The repo is public."))
        self.assertIn("- Q: How long?\n  A: two pages", text)
        self.assertIn("(say what you chose):\n- Which license do you prefer?", text)
        self.assertNotIn("Which README?", text)

    def test_depth_maps_to_model_and_effort(self):
        s = self.session("Text.", reply(depth="deep"))
        res = s.build()
        self.assertEqual((res.depth, res.model, res.effort), ("deep", "Opus", "high"))
        s = self.session("Text.", reply(depth="light"))
        self.assertEqual((s.build().model, s.result.effort), ("Haiku", "low"))

    def test_builtin_target_gives_no_effort(self):
        s = self.session("Text.", reply(depth="standard"), cfg={**self.cfg, "target": "nope"})
        res = s.build()
        self.assertEqual((res.model, res.effort), ("a mid-size model", ""))
        self.assertIn("the assistant", self.chat.call_args.args[2][0]["content"])

    def test_glossary_lines_are_attached_and_listed(self):
        path = self.write("c.md", "## Glossary\n- E7: the seventh run\n- prompter text: the script\n- B1: unused\n")
        ctx = core.parse_context_file(path)
        s = self.session("Shorten the prompter text. E7 stays.", reply(), context=ctx)
        res = s.build()
        self.assertEqual(res.terms, ["prompter text", "E7"])
        self.assertTrue(res.final_prompt.endswith("Project terms:\n- prompter text: the script\n- E7: the seventh run"))

    def test_turkish_labels_follow_the_text(self):
        s = self.session("Bu metni kısalt, lütfen çok kısa olsun.", reply(questions=[q("Ne kadar?")]))
        s.round.questions[-1].answer = "bir sayfa"
        self.assertIn("Bu istekle ilgili bana sorulan sorular ve cevaplarım:", s.build().final_prompt)

    def test_text_is_unchanged_even_with_em_dash(self):
        s = self.session(f"Keep this {EM} exactly.", reply())
        self.assertEqual(s.build().final_prompt, f"Keep this {EM} exactly.")


class BriefTests(TempDirCase):
    BRIEF = "Proj exports weekly reports."
    BODY = f"# Proj\n\n{BRIEF}\n\n## Glossary\n- E1: first export\n"

    def context(self, front="name: Proj\n", body=BODY):
        return core.parse_context_file(self.write("contexts/proj.md", f"---\n{front}---\n{body}"))

    def with_file(self, text="Now at stage two.\n", body="## Glossary\n- E1: first export\n", extra=""):
        brief = self.write("project/BRIEF.md", text, binary=isinstance(text, bytes))
        return self.context(f"name: Proj\nbrief: {brief}\n{extra}", body), brief

    # ---- the context file

    def test_front_matter_brief_key(self):
        ctx = self.context("name: Proj\nbrief: docs/BRIEF.md\ninclude:\n  - a.md\n")
        self.assertEqual(ctx.brief_path, self.tmp / "contexts" / "docs" / "BRIEF.md")  # like an include path
        self.assertEqual([p.name for p in ctx.includes], ["a.md"])
        self.assertIsNone(self.context().brief_path)
        ctx, brief = self.with_file()  # an absolute path, on Windows with a drive colon in it
        self.assertEqual(ctx.brief_path, brief)

    def test_without_a_brief_key_the_body_above_the_glossary_is_the_brief(self):
        ctx = self.context()
        self.assertEqual(ctx.brief(2000), (self.BRIEF, ctx.path, []))

    def test_the_brief_file_is_read_without_comments_and_byte_order_mark(self):
        ctx, brief = self.with_file("\ufeff<!-- kept current by the project -->\n# Brief\n\nNow at stage two.\n",
                                    body=self.BODY)
        self.assertEqual(ctx.brief(2000), ("# Brief\n\nNow at stage two.", brief, []))

    def test_a_missing_brief_file_warns_and_the_body_is_used(self):
        missing = self.tmp / "nope.md"
        ctx = self.context(f"name: Proj\nbrief: {missing}\n")
        self.assertEqual(ctx.brief(2000), (self.BRIEF, ctx.path, [f"brief not found: {missing}"]))

    def test_an_unreadable_brief_file_warns_and_the_body_is_used(self):
        ctx, brief = self.with_file(b"\xff\xfeN\x00o\x00w\x00", body=self.BODY)  # UTF-16, not UTF-8
        text, source, warnings = ctx.brief(2000)
        self.assertEqual((text, source), (self.BRIEF, ctx.path))
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith(f"cannot read {brief}: "), warnings[0])

    def test_an_empty_brief_file_means_no_brief(self):
        ctx, brief = self.with_file("<!-- nothing yet -->\n", body=self.BODY)
        self.assertEqual(ctx.brief(2000), ("", brief, []))

    def test_a_long_brief_is_cut_at_an_empty_line_with_a_warning(self):
        ctx = self.context(body="First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n\n## Glossary\n")
        text, _, warnings = ctx.brief(40)
        self.assertEqual(text, "First paragraph.\n\nSecond paragraph.")
        self.assertEqual(warnings, ["brief cut to 35 characters"])
        text, _, warnings = ctx.brief(10)
        self.assertEqual((text, warnings), ("First para", ["brief cut to 10 characters"]))

    def test_render_puts_the_brief_file_after_the_body_and_before_the_includes(self):
        self.write("contexts/inc.md", "Included text.\n")
        ctx, _ = self.with_file(extra="include: inc.md\n")
        text, warnings = ctx.render(12000)
        self.assertEqual(warnings, [])
        self.assertEqual(text, "## Glossary\n- E1: first export\n\n"
                               "--- project brief: BRIEF.md ---\nNow at stage two.\n\n"
                               "--- included file: inc.md ---\nIncluded text.")

    def test_render_reads_a_brief_file_that_is_also_an_include_once(self):
        brief = self.write("project/BRIEF.md", "Now at stage two.\n")
        ctx = self.context(f"name: Proj\nbrief: {brief}\ninclude: {brief}\n")
        text, warnings = ctx.render(12000)
        self.assertEqual((text.count("Now at stage two."), warnings), (1, []))
        self.assertNotIn("included file", text)

    def test_render_warns_about_a_missing_brief_file(self):
        missing = self.tmp / "nope.md"
        text, warnings = self.context(f"name: Proj\nbrief: {missing}\n").render(12000)
        self.assertEqual(warnings, [f"brief not found: {missing}"])
        self.assertNotIn("project brief", text)

    def test_render_without_a_brief_key_is_unchanged(self):
        text, warnings = self.context().render(12000)
        self.assertEqual((text, warnings), (self.BODY.strip(), []))

    # ---- the session

    def test_the_brief_is_attached_by_default(self):
        s = self.session("Shorten the report.", reply(), context=self.context())
        res = s.build()
        self.assertEqual((s.attach_brief, s.brief_text, s.brief_source), (True, self.BRIEF, "proj.md"))
        self.assertRegex(s.brief_date, r"^\d{4}-\d\d-\d\d$")
        self.assertEqual(res.final_prompt, f"Shorten the report.\n\nProject context (Proj):\n{self.BRIEF}")
        self.assertEqual((res.brief_chars, res.brief_source), (len(self.BRIEF), "proj.md"))

    def test_the_block_comes_after_the_answers_and_before_the_terms(self):
        s = self.session("Compare E1 with the report.", reply(questions=[q("Which report?")]), context=self.context())
        s.round.questions[0].answer = "the weekly one"
        self.assertEqual(s.build().final_prompt,
                         "Compare E1 with the report.\n\n"
                         "Questions I was asked about this request, and my answers:\n"
                         "- Q: Which report?\n  A: the weekly one\n\n"
                         f"Project context (Proj):\n{self.BRIEF}\n\n"
                         "Project terms:\n- E1: first export")

    def test_the_label_follows_the_language_of_the_text(self):
        s = self.session("Bu raporu kısalt, lütfen çok kısa olsun.", reply(), context=self.context())
        self.assertTrue(s.build().final_prompt.endswith(f"\n\nProje bağlamı (Proj):\n{self.BRIEF}"))

    def test_attach_brief_false_leaves_it_out_and_it_can_be_switched_on(self):
        s = self.session("Shorten the report.", reply(), context=self.context(), attach_brief=False)
        res = s.build()
        self.assertEqual(res.final_prompt, "Shorten the report.")
        self.assertEqual((res.brief_chars, res.brief_source), (0, ""))
        self.assertEqual(s.brief_text, self.BRIEF)  # read anyway, so a key can switch it on
        s.attach_brief = True
        with mock.patch.object(core, "chat_json", side_effect=AssertionError("no call expected")):
            res = s.build()
        self.assertIn("Project context (Proj):", res.final_prompt)
        self.assertEqual(res.brief_chars, len(self.BRIEF))

    def test_the_brief_file_is_used_when_there_is_one(self):
        ctx, _ = self.with_file(body=self.BODY)
        s = self.session("Shorten the report.", reply(), context=ctx)
        res = s.build()
        self.assertEqual(res.final_prompt, "Shorten the report.\n\nProject context (Proj):\nNow at stage two.")
        self.assertEqual((res.brief_chars, res.brief_source), (len("Now at stage two."), "BRIEF.md"))

    def test_no_block_without_a_project_or_without_brief_text(self):
        s = self.session("Text.", reply())
        self.assertEqual((s.brief_text, s.brief_source, s.brief_date), ("", "", ""))
        self.assertEqual(s.build().final_prompt, "Text.")
        s = self.session("Text.", reply(), context=self.context(body="## Glossary\n- E1: first export\n"))
        res = s.build()
        self.assertEqual((s.brief_text, res.final_prompt, res.brief_chars, res.brief_source), ("", "Text.", 0, ""))

    def test_brief_warnings_join_the_session_warnings_once(self):
        missing = self.tmp / "nope.md"
        s = core.CoachSession(self.cfg, "Text.", self.context(f"name: Proj\nbrief: {missing}\n"), "m")
        self.assertEqual(s.warnings, [f"brief not found: {missing}"])  # render() and brief() both report it
        self.assertEqual(s.brief_text, self.BRIEF)

    def test_the_limit_comes_from_the_config(self):
        s = core.CoachSession({**self.cfg, "brief_max_chars": 10}, "Text.", self.context(), "m")
        self.assertEqual((s.brief_text, s.warnings), ("Proj expor", ["brief cut to 10 characters"]))

    def test_the_model_reads_the_brief_file_and_its_codes_count_as_known(self):
        ctx, _ = self.with_file("E9 is the ninth export.\n")
        s = self.session("Look at E9.", reply(), context=ctx)
        self.assertEqual(s.round.questions, [])  # no term check
        system = self.chat.call_args.args[2][0]["content"]
        self.assertIn("--- project brief: BRIEF.md ---\nE9 is the ninth export.", system)

    def test_the_record_holds_the_length_and_the_source(self):
        s = self.session("Shorten the report.", reply(), context=self.context())
        s.build()
        rec = json.loads(s.save().read_text(encoding="utf-8"))
        self.assertEqual(rec["format"], 2)
        self.assertEqual((rec["result"]["brief_chars"], rec["result"]["brief_source"]), (len(self.BRIEF), "proj.md"))
        s.attach_brief = False
        s.build()
        rec = json.loads(s.save().read_text(encoding="utf-8"))
        self.assertEqual((rec["result"]["brief_chars"], rec["result"]["brief_source"]), (0, ""))


class GlossaryTests(TempDirCase):
    def context(self, raw=None):
        raw = raw if raw is not None else "---\nname: P\n---\n## Glossary\n- E1: first\n"
        path = self.write("c.md", raw, binary=isinstance(raw, bytes))
        return core.parse_context_file(path), path

    def test_new_terms_lists_answered_term_checks_only(self):
        ctx, _ = self.context()
        s = self.session("Compare E1 with E2, E3 and E4.", reply(), context=ctx)
        e2, e3, e4 = s.round.questions
        e2.answer, e3.answer, e4.answer = "the second run", "!", ""
        self.assertEqual(s.new_terms(), [("E2", "the second run")])

    def test_save_terms_writes_the_glossary_and_updates_the_session(self):
        ctx, path = self.context()
        s = self.session("Compare E1 with E2.", reply(), context=ctx)
        s.round.questions[0].answer = "the  second\nrun"
        self.assertEqual(s.save_terms(), ["E2"])
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "---\nname: P\n---\n## Glossary\n- E1: first\n- E2: the second run\n")
        self.assertEqual(s.new_terms(), [])
        self.assertEqual(s.save_terms(), [])
        self.assertIn("E2", [e.key for e in s.entries])
        self.assertFalse(list(self.tmp.glob("*.tmp")))

    def test_save_terms_keeps_bom_and_crlf(self):
        raw = b"\xef\xbb\xbf## Glossary\r\n- E1: first\r\n"
        ctx, path = self.context(raw)
        s = self.session("Compare E1 with E2.", reply(), context=ctx)
        s.round.questions[0].answer = "second"
        s.save_terms()
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbf## Glossary\r\n- E1: first\r\n- E2: second\r\n")

    def test_save_terms_without_a_project_raises(self):
        s = self.session("Compare E1 with E2.", reply())
        s.round.questions[0].answer = "second"
        with self.assertRaises(core.CoachError):
            s.save_terms()

    def test_unwritable_file_raises_and_leaves_no_temp_file(self):
        ctx, path = self.context()
        with mock.patch.object(core.os, "replace", side_effect=OSError("denied")):
            with self.assertRaises(core.CoachError):
                ctx.add_terms([("E2", "second")])
        self.assertFalse(list(self.tmp.glob("*.tmp")))
        self.assertNotIn("E2", path.read_text(encoding="utf-8"))

    def test_add_terms_with_a_known_term_writes_nothing(self):
        ctx, path = self.context()
        before = path.read_bytes()
        self.assertEqual(ctx.add_terms([("E1", "again")]), [])
        self.assertEqual(path.read_bytes(), before)


class HistoryTests(TempDirCase):
    def saved(self, session):
        path = session.save()
        return path, json.loads(path.read_text(encoding="utf-8"))

    def test_format_2_record(self):
        s = self.session("Shorten this.", reply(questions=[q("How short?", missing="the length")], depth="deep"))
        s.round.questions[0].answer = "one page"
        s.set_note("Be brief.")
        s.build()
        path, rec = self.saved(s)
        self.assertEqual(rec["format"], 2)
        self.assertEqual(rec["app_version"], (core.APP_DIR / "VERSION").read_text().strip())
        for key in ("time", "model_used", "context", "target", "output_language", "text_language",
                    "original", "rounds", "result", "translation", "copied"):
            self.assertIn(key, rec)
        self.assertEqual((rec["model_used"], rec["target"], rec["original"]), ("test-model", "Claude", "Shorten this."))
        self.assertEqual(len(rec["rounds"]), 1)
        rnd = rec["rounds"][0]
        self.assertEqual((rnd["depth"], rnd["note"]), ("deep", "Be brief."))
        self.assertEqual(rnd["questions"], [{"question": "How short?", "missing": "the length",
                                             "answer": "one page", "source": "model", "term": ""}])
        self.assertEqual(rnd["stats"], {"prompt_tokens": 700, "output_tokens": 200, "seconds": 9.5,
                                        "gen_seconds": 7.0, "num_ctx": 8192})
        self.assertEqual(set(rec["result"]), {"final_prompt", "depth", "model", "effort", "terms", "notes",
                                              "brief_chars", "brief_source"})
        self.assertEqual((rec["result"]["brief_chars"], rec["result"]["brief_source"]), (0, ""))
        self.assertIsNone(rec["translation"])
        self.assertIsNone(rec["copied"])
        self.assertTrue(rec["result"]["final_prompt"].startswith(rec["original"]))

    def test_save_before_build_has_no_result(self):
        _, rec = self.saved(self.session("Text.", reply()))
        self.assertIsNone(rec["result"])

    def test_later_saves_rewrite_the_same_file(self):
        s = self.session("Text.", reply())
        s.build()
        first, _ = self.saved(s)
        s.mark_copied("the copied text")
        second, rec = self.saved(s)
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.tmp / "history").glob("*.json"))), 1)
        self.assertEqual(rec["copied"]["text"], "the copied text")
        self.assertTrue(rec["copied"]["time"])

    def test_mark_copied_does_not_write_by_itself(self):
        s = self.session("Text.", reply())
        s.mark_copied("x")
        self.assertFalse((self.tmp / "history").exists())

    def test_record_time_is_stable_across_saves(self):
        s = self.session("Text.", reply())
        _, one = self.saved(s)
        _, two = self.saved(s)
        self.assertEqual(one["time"], two["time"])

    def test_unwritable_history_folder_returns_none(self):
        blocker = self.write("blocker", "a file, not a folder")
        s = self.session("Text.", reply(), cfg={**self.cfg, "history_dir": str(blocker / "sub")})
        self.assertIsNone(s.save())

    def test_text_language_is_recorded(self):
        s = self.session("Bu metni kısalt, lütfen çok kısa olsun.", reply())
        _, rec = self.saved(s)
        self.assertEqual(rec["text_language"], "tr")


class TranslateTests(TempDirCase):
    TEXT = "Bu raporu kısalt ve sonucu 13.049 satırlık tabloya yaz, lütfen çok kısa olsun."

    def fake(self, calls, fn):
        """chat_json stand-in: fn(parts) returns the reply dict for a translate call."""
        def chat(cfg, model, messages, schema, temperature):
            parts = json.loads(messages[1]["content"])
            self.assertEqual(temperature, 0.0)
            self.assertEqual(set(schema["required"]), set(parts))
            calls.append(parts)
            return fn(parts), STATS
        return chat

    def built(self, text=None, lang="English", note="", answers=()):
        s = self.session(text or self.TEXT, reply(questions=[q(f"Which file {i}?") for i, _ in enumerate(answers)]))
        for qq, a in zip(s.round.questions, answers):
            qq.answer = a
        s.out_lang = lang
        s.set_note(note)
        s.build()
        return s

    def test_needs_translation(self):
        s = self.built(lang="same")
        self.assertFalse(s.needs_translation())
        s.out_lang = "Turkish"
        self.assertFalse(s.needs_translation())   # the text is Turkish already
        s.out_lang = "English"
        self.assertTrue(s.needs_translation())

    def test_same_language_never_calls_the_model(self):
        s = self.built(lang="same")
        with self.assertRaises(core.CoachError):
            s.translate()

    def test_all_parts_pass(self):
        s = self.built(note="Kısa tut.")
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, lambda p: {
                k: "Shorten this report and write the result into the 13.049 line table." if k == "1"
                else "Keep it short." for k in p})):
            tr = s.translate()
        self.assertEqual(len(calls), 1)
        self.assertEqual(tr.language, "English")
        self.assertEqual(tr.parts, 2)
        self.assertEqual(tr.kept, [])
        self.assertTrue(tr.text.startswith("Shorten this report"))
        self.assertIn("\n\nNote: Keep it short.", tr.text)   # English labels around the parts
        self.assertEqual(len(tr.stats), 1)

    def test_progress_is_reported_per_batch(self):
        s = self.built()
        seen = []
        ok = "Shorten this report and write the result into the 13.049 line table."
        with mock.patch.object(core, "chat_json", self.fake([], lambda p: {k: ok for k in p})):
            s.translate(progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(1, 1)])

    def test_a_part_that_passes_on_the_second_try(self):
        s = self.built()
        calls = []
        good = "Shorten this report and write the result into the 13.049 line table."

        def fn(parts):
            return {k: ("Shorten this report." if len(calls) == 1 else good) for k in parts}

        with mock.patch.object(core, "chat_json", self.fake(calls, fn)):
            tr = s.translate()
        self.assertEqual(len(calls), 2)         # the batch, then the one part alone
        self.assertEqual(tr.kept, [])
        self.assertEqual(tr.text, good)

    def test_a_part_that_fails_twice_stays_in_the_source_language(self):
        s = self.built()
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, lambda p: {k: "Shorten this." for k in p})):
            tr = s.translate()
        self.assertEqual(len(calls), 2)
        self.assertEqual(tr.text, self.TEXT)
        self.assertEqual(len(tr.kept), 1)
        self.assertEqual(tr.kept[0]["index"], 1)
        self.assertIn("numbers missing", tr.kept[0]["problems"])

    def test_batches_stay_under_4000_characters(self):
        text = "\n\n".join(f"Bu {i}. paragraf, lütfen çok kısa olsun ve metni değiştirme. " * 10 for i in range(20))
        s = self.built(text=text)
        calls = []

        def fn(parts):
            return {k: v.replace("Bu ", "This ").replace("lütfen çok kısa olsun ve metni değiştirme",
                                                          "keep it short and do not change the text")
                    for k, v in parts.items()}

        with mock.patch.object(core, "chat_json", self.fake(calls, fn)):
            tr = s.translate()
        self.assertGreater(len(calls), 1)
        for batch in calls:
            self.assertLessEqual(sum(len(v) for v in batch.values()), core.BATCH_LIMIT)
        self.assertEqual(sorted(int(k) for b in calls for k in b), list(range(1, tr.parts + 1)))

    def test_parts_already_in_the_output_language_are_not_sent(self):
        text = "Please keep this sentence in English as it is.\n\nBu raporu kısalt, lütfen çok kısa olsun."
        s = self.built(text=text)
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, lambda p: {k: "Shorten this report, keep it very short." for k in p})):
            tr = s.translate()
        self.assertEqual(calls, [{"1": "Bu raporu kısalt, lütfen çok kısa olsun."}])
        self.assertTrue(tr.text.startswith("Please keep this sentence in English as it is.\n\nShorten"))

    def test_an_em_dash_the_source_did_not_have_is_replaced(self):
        s = self.built()
        out = "Shorten this report " + EM + " and write the result into the 13.049 line table."
        with mock.patch.object(core, "chat_json", self.fake([], lambda p: {k: out for k in p})):
            tr = s.translate()
        self.assertNotIn(EM, tr.text)

    def test_build_drops_an_old_translation(self):
        s = self.built()
        ok = "Shorten this report and write the result into the 13.049 line table."
        with mock.patch.object(core, "chat_json", self.fake([], lambda p: {k: ok for k in p})):
            s.translate()
        self.assertIsNotNone(s.translation)
        s.build()
        self.assertIsNone(s.translation)

    def test_the_record_holds_the_translation(self):
        s = self.built()
        ok = "Shorten this report and write the result into the 13.049 line table."
        with mock.patch.object(core, "chat_json", self.fake([], lambda p: {k: ok for k in p})):
            s.translate()
        path = s.save()
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["translation"]["language"], "English")
        self.assertEqual(rec["translation"]["text"], ok)
        self.assertEqual(rec["translation"]["parts"], 1)
        self.assertEqual(rec["translation"]["kept"], [])
        self.assertEqual(rec["translation"]["stats"][0]["output_tokens"], 200)
        self.assertEqual(rec["output_language"], "English")

    def test_a_model_error_propagates(self):
        s = self.built()

        def boom(*a, **k):
            raise core.CoachError("Ollama is not reachable")

        with mock.patch.object(core, "chat_json", boom):
            with self.assertRaises(core.CoachError):
                s.translate()
        self.assertIsNone(s.translation)

    # ---- the cache

    OK = "Shorten this report and write the result into the 13.049 line table."

    def good(self, parts):
        return {k: (self.OK if "13.049" in v else "Keep it short.") for k, v in parts.items()}

    def test_a_second_translate_makes_no_call(self):
        s = self.built(note="Kısa tut.")
        calls = []
        self.assertFalse(s.translation_cached())
        with mock.patch.object(core, "chat_json", self.fake(calls, self.good)):
            first = s.translate()
            self.assertTrue(s.translation_cached())
            second = s.translate()
        self.assertEqual(len(calls), 1)
        self.assertIsNot(second, first)
        self.assertEqual((second.text, second.parts, second.kept), (first.text, 2, []))
        self.assertEqual(len(second.stats), 1)  # the one call this session made

    def test_fresh_sends_every_part_again(self):
        s = self.built(note="Kısa tut.")
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, self.good)):
            s.translate()
            tr = s.translate(fresh=True)
        self.assertEqual(calls, [{"1": self.TEXT, "2": "Kısa tut."}] * 2)
        self.assertEqual(len(tr.stats), 2)

    def test_after_a_new_build_only_the_new_part_is_sent(self):
        s = self.built()
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, self.good)):
            s.translate()
            s.set_note("Kısa tut.")
            s.build()
            self.assertFalse(s.translation_cached())
            tr = s.translate()
        self.assertEqual(calls, [{"1": self.TEXT}, {"2": "Kısa tut."}])
        self.assertEqual(tr.text, self.OK + "\n\nNote: Keep it short.")
        self.assertEqual((tr.parts, len(tr.stats)), (2, 2))

    def test_a_part_kept_in_the_source_language_is_not_sent_again(self):
        s = self.built()
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, lambda p: {k: "Shorten this." for k in p})):
            s.translate()
            self.assertTrue(s.translation_cached())
            tr = s.translate()
            self.assertEqual(len(calls), 2)  # the batch and the second try, both from the first run
            self.assertEqual((tr.text, tr.kept[0]["index"]), (self.TEXT, 1))
            self.assertIn("numbers missing", tr.kept[0]["problems"])
            s.translate(fresh=True)
        self.assertEqual(len(calls), 4)

    def test_the_cache_is_per_output_language(self):
        s = self.built(text="Please shorten this report and keep the 13.049 line table as it is.", lang="Turkish")
        calls = []
        turkish = "Lütfen bu raporu kısalt ve 13.049 satırlık tabloyu olduğu gibi bırak."
        with mock.patch.object(core, "chat_json", self.fake(calls, lambda p: {k: turkish for k in p})):
            s.translate()
            s.out_lang = "same"
            self.assertFalse(s.translation_cached())
            s.out_lang = "Turkish"
            self.assertTrue(s.translation_cached())
        self.assertEqual(len(calls), 1)

    def test_switching_the_brief_costs_no_call_and_the_brief_is_not_translated(self):
        brief = "Proj exports weekly reports."
        path = self.write("c.md", f"---\nname: Proj\n---\n# Proj\n\n{brief}\n\n## Glossary\n- E1: first export\n")
        s = self.session(self.TEXT, reply(), context=core.parse_context_file(path))
        s.out_lang = "English"
        s.build()
        calls = []
        with mock.patch.object(core, "chat_json", self.fake(calls, self.good)):
            attached = s.translate()
            s.attach_brief = False
            s.build()
            self.assertTrue(s.translation_cached())
            removed = s.translate()
        self.assertEqual(calls, [{"1": self.TEXT}])
        self.assertEqual(attached.text, f"{self.OK}\n\nProject context (Proj):\n{brief}")  # English label
        self.assertEqual(removed.text, self.OK)
        self.assertTrue(s.result.final_prompt.endswith("olsun."))


if __name__ == "__main__":
    unittest.main()
