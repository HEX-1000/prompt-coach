"""Tests for coach_text.py (pure functions). python -B -m unittest discover -s tests"""
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import coach_text as ct  # noqa: E402


@dataclass
class Q:
    question: str
    answer: str = ""
    source: str = "model"
    term: str = ""
    missing: str = ""


GLOSSARY_FILE = """# Project

Intro text.

## Glossary

<!-- keep this list short -->
- E7: the seventh retrieval experiment
- prompter text: the script read from the teleprompter
  during recording.
- A02, A03 ...: episode project names in DaVinci Resolve.
- 0.25.1 / D2: a release and its decision
- Records/vscode: the VS Code record folder

## Other

- NOT: an entry
"""


class GuessLanguageTests(unittest.TestCase):
    def test_turkish(self):
        self.assertEqual(ct.guess_language("Bu metni kısaltmak istiyorum ve bir özet için lütfen"), "tr")

    def test_turkish_letters_count(self):
        self.assertEqual(ct.guess_language("çözüm şöyle"), "tr")  # 4 special letters = score 2

    def test_english(self):
        self.assertEqual(ct.guess_language("Please shorten the text and send it to the team"), "en")

    def test_too_short(self):
        self.assertEqual(ct.guess_language("ok"), "")
        self.assertEqual(ct.guess_language(""), "")
        self.assertEqual(ct.guess_language("the"), "")

    def test_mixed_without_a_winner(self):
        self.assertEqual(ct.guess_language("the and ve bir"), "")

    def test_capital_dotted_i(self):
        self.assertEqual(ct.guess_language("İstiyorum ve bir"), "tr")


class UnknownCodesTests(unittest.TestCase):
    def test_finds_a_code_once(self):
        self.assertEqual(ct.unknown_codes("Fix D5 and D5 again", ""), ["D5"])

    def test_skips_common_codes(self):
        self.assertEqual(ct.unknown_codes("Use H264 and MP4", ""), [])

    def test_known_through_the_context_text(self):
        self.assertEqual(ct.unknown_codes("Fix D5 and E7", "d5: the fifth decision"), ["E7"])

    def test_known_through_a_series(self):
        entries = ct.parse_glossary("## Glossary\n- A02, A03 ...: episodes\n")
        self.assertEqual(ct.unknown_codes("A07 and B01", "", entries), ["B01"])

    def test_series_does_not_cover_other_letters_or_longer_prefixes(self):
        entries = ct.parse_glossary("## Glossary\n- A02, A03 ...: episodes\n")
        self.assertEqual(ct.unknown_codes("AB07", "", entries), ["AB07"])


class ParseGlossaryTests(unittest.TestCase):
    def setUp(self):
        self.entries = ct.parse_glossary(GLOSSARY_FILE)
        self.by_key = {e.key: e for e in self.entries}

    def test_a_plain_entry(self):
        e = self.by_key["E7"]
        self.assertEqual((e.definition, e.aliases, e.series), ("the seventh retrieval experiment", ("E7",), ""))

    def test_the_section_ends_at_the_next_heading(self):
        self.assertNotIn("NOT", self.by_key)
        self.assertEqual(len(self.entries), 5)

    def test_a_continuation_line(self):
        self.assertEqual(self.by_key["prompter text"].definition,
                         "the script read from the teleprompter during recording.")

    def test_aliases_split_at_comma_and_spaced_slash(self):
        self.assertEqual(self.by_key["0.25.1 / D2"].aliases, ("0.25.1", "D2"))
        self.assertEqual(self.by_key["A02, A03 ..."].aliases, ("A02", "A03"))

    def test_a_slash_without_spaces_stays_one_alias(self):
        self.assertEqual(self.by_key["Records/vscode"].aliases, ("Records/vscode",))

    def test_a_series_key(self):
        self.assertEqual(self.by_key["A02, A03 ..."].series, "A")
        self.assertEqual(ct.parse_glossary("## Terms\n- UE5, UE6 \u2026: engine\n")[0].series, "UE")

    def test_a_comment_is_skipped(self):
        body = "## Glossary\n<!-- - HIDDEN: no\n- ALSO: no -->\n- X1: yes\n"
        self.assertEqual([e.key for e in ct.parse_glossary(body)], ["X1"])

    def test_other_heading_names_and_levels(self):
        for title in ("Sözlük", "TERİMLER", "terms", "GLOSSARY"):
            self.assertEqual(len(ct.parse_glossary(f"### {title}\n- A1: x\n#### Sub\n- B1: y\n")), 2, title)

    def test_a_same_level_heading_ends_the_section(self):
        self.assertEqual(len(ct.parse_glossary("### Terms\n- A1: x\n### More\n- B1: y\n")), 1)

    def test_no_heading_means_no_entries(self):
        self.assertEqual(ct.parse_glossary("- A1: x\n- B1: y\n"), [])


class TermsInTests(unittest.TestCase):
    def setUp(self):
        self.entries = ct.parse_glossary(GLOSSARY_FILE)

    def keys(self, text):
        return [e.key for e in ct.terms_in(text, self.entries)]

    def test_codes_match_with_exact_case(self):
        self.assertEqual(self.keys("e7 is here"), [])
        self.assertEqual(self.keys("E7 is here"), ["E7"])

    def test_phrases_match_in_any_case(self):
        self.assertEqual(self.keys("Shorten the PROMPTER Text"), ["prompter text"])

    def test_a_code_does_not_match_inside_a_longer_code(self):
        self.assertEqual(self.keys("E71 and E7a and xE7"), [])
        self.assertEqual(self.keys("E7, then E7."), ["E7"])

    def test_a_suffix_after_an_apostrophe(self):
        self.assertEqual(self.keys("E7'nin ve prompter text'i"), ["prompter text", "E7"][::-1])

    def test_a_series_matches_prefix_and_digits(self):
        self.assertEqual(self.keys("see A07 and A12.1"), ["A02, A03 ..."])
        self.assertEqual(self.keys("see AB07 and A"), [])

    def test_a_dot_before_blocks_a_match(self):
        self.assertEqual(self.keys("version 1.D2 only"), [])
        self.assertEqual(self.keys("version 0.25.1 only"), ["0.25.1 / D2"])

    def test_order_follows_first_position(self):
        self.assertEqual(self.keys("D2 then A07 then E7"), ["0.25.1 / D2", "A02, A03 ...", "E7"])


class AddGlossaryLineTests(unittest.TestCase):
    def test_after_the_last_entry(self):
        out = ct.add_glossary_line(GLOSSARY_FILE, "F9", "a new thing")
        self.assertIn("- Records/vscode: the VS Code record folder\n- F9: a new thing\n\n## Other", out)
        self.assertEqual([e.key for e in ct.parse_glossary(out)][-1], "F9")

    def test_after_the_continuation_lines_of_the_last_entry(self):
        text = "## Glossary\n- A1: first\n  more of first\n\n## X\n"
        out = ct.add_glossary_line(text, "B1", "second")
        self.assertEqual(out, "## Glossary\n- A1: first\n  more of first\n- B1: second\n\n## X\n")

    def test_into_an_empty_section(self):
        out = ct.add_glossary_line("# T\n\n## Glossary\n\n## Next\n", "F9", "x")
        self.assertEqual(out, "# T\n\n## Glossary\n- F9: x\n\n## Next\n")

    def test_a_heading_at_the_end_without_a_newline(self):
        self.assertEqual(ct.add_glossary_line("## Glossary", "F9", "x"), "## Glossary\n- F9: x")

    def test_no_heading(self):
        self.assertEqual(ct.add_glossary_line("# T\ntext\n", "F9", "x"), "# T\ntext\n\n## Glossary\n- F9: x\n")
        self.assertEqual(ct.add_glossary_line("# T\ntext", "F9", "x"), "# T\ntext\n\n## Glossary\n- F9: x\n")
        self.assertEqual(ct.add_glossary_line("", "F9", "x"), "## Glossary\n- F9: x\n")

    def test_crlf_is_kept(self):
        text = "# T\r\n\r\n## Glossary\r\n- A1: one\r\n"
        out = ct.add_glossary_line(text, "B1", "two")
        self.assertEqual(out, "# T\r\n\r\n## Glossary\r\n- A1: one\r\n- B1: two\r\n")
        out = ct.add_glossary_line("# T\r\n", "B1", "two")
        self.assertEqual(out, "# T\r\n\r\n## Glossary\r\n- B1: two\r\n")

    def test_an_existing_term_leaves_the_text_unchanged(self):
        for term in ("E7", "A07", "prompter TEXT", "D2"):
            self.assertEqual(ct.add_glossary_line(GLOSSARY_FILE, term, "x"), GLOSSARY_FILE, term)

    def test_term_and_definition_go_on_one_line(self):
        out = ct.add_glossary_line("## Glossary\n", "F9", "two\nlines  here")
        self.assertEqual(out, "## Glossary\n- F9: two lines here\n")


DOC_ENTRIES = ct.parse_glossary(
    "## Glossary\n"
    "- prompter text: the script read from the teleprompter during recording.\n"
    "- A02, A03 ...: episode project names in DaVinci Resolve.\n"
)

DOC_EXAMPLE = """Shorten the prompter text for A07 and tell me which license fits the repo. E7 stays as it is.

Note: The repo is already public.

Questions I was asked about this request, and my answers:
- Q: How long should the shortened text be?
  A: about 90 seconds of speech

Questions I was asked and leave to you (say what you chose):
- Which license do you want for the repo?

Project terms:
- E7: the seventh retrieval experiment
- prompter text: the script read from the teleprompter during recording.
- A02, A03 ...: episode project names in DaVinci Resolve."""

DOC_TEXT = "Shorten the prompter text for A07 and tell me which license fits the repo. E7 stays as it is."


def doc_questions():
    return [
        Q("How long should the shortened text be?", "about 90 seconds of speech"),
        Q("Which license do you want for the repo?", "!"),
        Q("What is E7?", "the seventh retrieval experiment", source="term-check", term="E7"),
    ]


class AssembleTests(unittest.TestCase):
    def test_the_example_of_the_design_character_for_character(self):
        segs = ct.assemble(DOC_TEXT, doc_questions(), "The repo is already public.", DOC_ENTRIES, "en")
        self.assertEqual(ct.render(segs), DOC_EXAMPLE)

    def test_the_text_comes_first_and_unchanged(self):
        text = "  Keep   this  spacing\nand lines.  \n"
        out = ct.render(ct.assemble(text, doc_questions(), "n", DOC_ENTRIES, "en"))
        self.assertTrue(out.startswith("Keep   this  spacing\nand lines."))
        self.assertEqual(ct.render(ct.assemble(text, [], "", [], "en")), "Keep   this  spacing\nand lines.")

    def test_empty_blocks_are_left_out(self):
        self.assertEqual(ct.render(ct.assemble("Hello", [], "", DOC_ENTRIES, "en")), "Hello")
        only_note = ct.render(ct.assemble("Hello", [Q("x?", "")], "a note", [], "en"))
        self.assertEqual(only_note, "Hello\n\nNote: a note")

    def test_a_term_check_answered_with_bang_is_a_skip(self):
        qs = [Q("What is E7?", "!", source="term-check", term="E7")]
        self.assertEqual(ct.render(ct.assemble("Hello E7", qs, "", [], "en")), "Hello E7")

    def test_a_skipped_question_is_not_in_the_prompt(self):
        out = ct.render(ct.assemble("Hello", [Q("Why?", "   ")], "", [], "en"))
        self.assertEqual(out, "Hello")

    def test_turkish_labels(self):
        out = ct.render(ct.assemble("Merhaba", doc_questions(), "Bir not", [], "tr"))
        self.assertIn("\n\nNot: Bir not", out)
        self.assertIn("Bu istekle ilgili bana sorulan sorular ve cevaplarım:\n- S: How long", out)
        self.assertIn("\n  C: about 90", out)
        self.assertIn("Bana sorulan ve kararını sana bıraktığım sorular (neyi seçtiğini söyle):", out)
        self.assertIn("\n\nProje terimleri:\n- E7:", out)

    def test_an_unknown_language_falls_back_to_english(self):
        a = ct.render(ct.assemble("Hello", doc_questions(), "n", DOC_ENTRIES, "de"))
        b = ct.render(ct.assemble("Hello", doc_questions(), "n", DOC_ENTRIES, "en"))
        self.assertEqual(a, b)
        self.assertEqual(ct.render(ct.assemble("Hello", doc_questions(), "n", DOC_ENTRIES, "")), b)

    def test_a_multi_line_answer_goes_on_one_line(self):
        qs = [Q("Which\nfile?", "the  first\n\nand the second")]
        out = ct.render(ct.assemble("Hello", qs, "two\nlines", [], "en"))
        self.assertIn("Note: two lines", out)
        self.assertIn("- Q: Which file?\n  A: the first and the second", out)

    def test_glossary_lines_come_from_text_answers_and_note_together(self):
        entries = ct.parse_glossary("## Glossary\n- F9: the ninth\n- prompter text: the script\n- G1: unused\n")
        qs = [Q("Which?", "use F9 there")]
        out = ct.render(ct.assemble("Hello", qs, "the prompter text too", entries, "en"))
        self.assertTrue(out.endswith("Project terms:\n- prompter text: the script\n- F9: the ninth"), out)

    def test_segments_have_the_same_layout_in_every_language(self):
        en = ct.assemble(DOC_TEXT, doc_questions(), "note", DOC_ENTRIES, "en")
        tr = ct.assemble(DOC_TEXT, doc_questions(), "note", DOC_ENTRIES, "tr")
        self.assertEqual([k for k, _ in en], [k for k, _ in tr])
        self.assertEqual([t for k, t in en if k == "user"], [t for k, t in tr if k == "user"])
        self.assertIn("user", {k for k, _ in en})
        self.assertIn("fixed", {k for k, _ in en})


class SplitPartsTests(unittest.TestCase):
    def joined(self, parts):
        return "".join(p + s for p, s in parts)

    def test_joining_gives_the_input_back(self):
        samples = [
            "",
            "   ",
            "One sentence.",
            "  Leading space. Then more.  \n\n\nSecond block here.\n",
            "A long text. " * 60 + "\n\n" + "Another one! " * 40 + "Tail",
            "Wait... what? Yes; or no: maybe\u2026 who knows. " * 20,
            "Line one\r\n\r\nLine two\r\n\r\n  \r\nLine three",
            "x" * 1000,
        ]
        for text in samples:
            for limit in (20, 80, 350):
                self.assertEqual(self.joined(ct.split_parts(text, limit)), text, (text[:30], limit))

    def test_a_short_text_is_one_part(self):
        self.assertEqual(ct.split_parts("Hello there. How are you?", 350), [("Hello there. How are you?", "")])
        self.assertEqual(ct.split_parts("", 350), [])

    def test_empty_lines_stay_in_the_separators(self):
        parts = ct.split_parts("First.\n\nSecond.\n  \n\nThird.\n", 350)
        self.assertEqual(parts, [("First.", "\n\n"), ("Second.", "\n  \n\n"), ("Third.", "\n")])

    def test_no_split_at_an_ellipsis(self):
        parts = ct.split_parts("Hmm... yes. Hmm... no. Hmm... ok.", 15)
        self.assertEqual([p for p, _ in parts], ["Hmm... yes.", "Hmm... no.", "Hmm... ok."])

    def test_sentences_are_packed_up_to_the_limit(self):
        text = "Aaa aaa. Bbb bbb. Ccc ccc. Ddd ddd."
        parts = ct.split_parts(text, 20)
        self.assertEqual([p for p, _ in parts], ["Aaa aaa. Bbb bbb.", "Ccc ccc. Ddd ddd."])
        self.assertTrue(all(len(p) <= 20 for p, _ in parts))

    def test_a_long_sentence_is_cut_at_a_semicolon(self):
        text = "First clause here; second clause here; third clause here."
        parts = ct.split_parts(text, 25)
        self.assertEqual([p for p, _ in parts], ["First clause here;", "second clause here;", "third clause here."])

    def test_a_long_sentence_is_cut_at_a_colon_or_an_ellipsis(self):
        parts = ct.split_parts("Do this first: then that one\u2026 and finally the rest", 20)
        self.assertEqual([p for p, _ in parts], ["Do this first:", "then that one\u2026", "and finally the rest"])

    def test_the_limit_is_respected_where_sentences_allow(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(60))
        for p, _ in ct.split_parts(text, 100):
            self.assertLessEqual(len(p), 100)

    def test_a_piece_without_a_break_stays_whole(self):
        parts = ct.split_parts("x" * 500, 100)
        self.assertEqual(parts, [("x" * 500, "")])


class CheckPartTests(unittest.TestCase):
    SRC = "Bu metni kısaltmak istiyorum ve bir özet için lütfen"
    OUT = "I want to shorten this text and a summary, please"

    def test_a_clean_part(self):
        self.assertEqual(ct.check_part(self.SRC, self.OUT, "tr"), [])

    def test_empty(self):
        self.assertEqual(ct.check_part(self.SRC, "  \n", "tr"), ["empty"])

    def test_urls(self):
        src = "Bakın https://example.com/a. Sonra devam"
        self.assertEqual(ct.check_part(src, "See https://example.com/a. Then go on", "en"), [])
        self.assertEqual(ct.check_part(src, "See the page. Then go on", "en"), ["urls"])
        self.assertEqual(ct.check_part(src, "See https://example.com/b then go on", "en"), ["urls"])

    def test_numbers_missing_and_added(self):
        src = "Toplam 120 dosya ve 2026 yılı"
        self.assertEqual(ct.check_part(src, "Total 120 files and the year", "tr"), ["numbers missing"])
        self.assertEqual(ct.check_part(src, "Total 120 files, 2026 year, 77 more", "tr"), ["numbers added"])

    def test_a_one_digit_number_that_became_a_word_is_fine(self):
        self.assertEqual(ct.check_part("Ek 2. madde burada", "The second item is here", "tr"), [])

    def test_thousands_and_decimal_marks_are_equal(self):
        self.assertEqual(ct.check_part("Sayı 13,049 oldu", "The number 13.049 became", "tr"), [])

    def test_codes(self):
        self.assertEqual(ct.check_part("E7 ve A02 var", "E7 and B02 exist", "tr"), ["codes"])
        self.assertEqual(ct.check_part("E7 ve A02 var", "E7 and A02 exist", "tr"), [])

    def test_length(self):
        src = "Bu metin yeterince uzun, kırk karakterden fazla"
        self.assertEqual(ct.check_part(src, "Short text", "tr"), ["length"])
        self.assertEqual(ct.check_part(src, "text " * 30, "tr"), ["length"])
        # under 40 source characters the ratio is not checked
        self.assertEqual(ct.check_part("Kısa metin", "A", "tr"), [])

    def test_language(self):
        src = "Please shorten the text and send it to the team"
        self.assertEqual(ct.check_part(src, "Please shorten the text and send it to them", "en"), ["language"])
        self.assertEqual(ct.check_part(src, "Metni kısalt ve ekibe gönder lütfen", "en"), [])

    def test_language_is_not_checked_for_a_short_output_or_an_unknown_source(self):
        self.assertEqual(ct.check_part("Bu bir test", "This is the", "tr"), [])
        self.assertEqual(ct.check_part("Bu bir test", "Bu bir test ve bir", ""), [])


if __name__ == "__main__":
    unittest.main()
