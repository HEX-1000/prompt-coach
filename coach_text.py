"""Pure text functions for Prompt Coach. Standard library only, no file or network access.

Language guess, codes, glossary, project brief, prompt assembly, part splitting and translation checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- codes

# Codes such as D5, A02, E1, V0.25.1, UE5. Small models expand these from general
# knowledge ("D5" -> Nikon D5000); the term check makes sure they are asked about instead.
CODE_RE = re.compile(r"(?<![\w.])([A-Z]{1,3}\d{1,3}(?:\.\d+)*)(?![\w])")
_CODE_FULL_RE = re.compile(r"[A-Z]{1,3}\d{1,3}(?:\.\d+)*")
COMMON_CODES = {
    "H264", "H265", "MP3", "MP4", "UTF8", "UTF16", "X64", "X86", "IPV4", "IPV6", "SHA1",
    "SHA256", "MD5", "S3", "EC2", "PS4", "PS5", "USB2", "USB3", "GPT4", "GPT5", "WIN10",
    "WIN11", "DDR4", "DDR5", "M1", "M2", "M3", "M4", "A4", "A3", "K8S", "4K", "8K", "B2B",
    "P2P", "L1", "L2", "L3", "HTTP2", "HTTP3", "TLS1", "C99", "C11", "C17", "C23", "ES6",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "H3", "H4", "H5", "H6", "MP2", "AV1", "VP9", "IE11",
}


def one_line(s: str) -> str:
    return " ".join(s.split())


# --------------------------------------------------------------------------- language

_TR_WORDS = frozenset(
    "ve bir bu şu için ile gibi çok daha ama fakat veya değil olarak kadar sonra önce "
    "hem bana bunu bunun şunu istiyorum lütfen nasıl neden ise olan olsun ben sen biz siz "
    "mı mü yok".split()
)
_EN_WORDS = frozenset(
    "the and of to is that for with this are from or by not you have will would should "
    "what which how please my your we was but if so they do all me".split()
)
_TR_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")
_WORD_RE = re.compile(r"[^\W\d_]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.replace("İ", "i").lower())


def guess_language(text: str) -> str:
    """'tr', 'en' or '' (unsure). '' is safe wherever it is used."""
    words = _words(text)
    tr = sum(w in _TR_WORDS for w in words) + 0.5 * sum(ch in _TR_LETTERS for ch in text)
    en = sum(w in _EN_WORDS for w in words)
    if tr >= 2 and tr > en:
        return "tr"
    if en >= 2 and en > tr:
        return "en"
    return ""


# --------------------------------------------------------------------------- glossary


@dataclass(frozen=True)
class GlossaryEntry:
    key: str
    definition: str
    aliases: tuple[str, ...]
    series: str = ""


_HEADING_RE = re.compile(r"^\ufeff?(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_GLOSSARY_TITLES = {"glossary", "terms", "sözlük", "terimler"}
_ENTRY_RE = re.compile(r"^[ \t]*[-*+][ \t]+(\S.*?):[ \t](.*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _heading(line: str) -> tuple[int, str] | None:
    m = _HEADING_RE.match(line.rstrip("\r\n"))
    return (len(m.group(1)), m.group(2)) if m else None


def _is_glossary_title(title: str) -> bool:
    return title.replace("İ", "i").lower().strip() in _GLOSSARY_TITLES


def _make_entry(key: str, definition: str) -> GlossaryEntry:
    series = ""
    core = key.strip()
    is_series = core.endswith("...") or core.endswith("…")
    if is_series:
        core = core[:-3] if core.endswith("...") else core[:-1]
    aliases: list[str] = []
    for chunk in core.split(","):
        for alias in chunk.split(" / "):
            alias = alias.strip()
            if alias:
                aliases.append(alias)
    if is_series:
        for alias in reversed(aliases):
            if _CODE_FULL_RE.fullmatch(alias):
                series = re.match(r"[A-Z]+", alias)[0]
                break
    return GlossaryEntry(key=key.strip(), definition=one_line(definition), aliases=tuple(aliases), series=series)


def _section_lines(lines: list[str]) -> tuple[int, int] | None:
    """(heading index, end index) of the glossary section in a list of lines, end exclusive."""
    for i, line in enumerate(lines):
        h = _heading(line)
        if h and _is_glossary_title(h[1]):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                h2 = _heading(lines[j])
                if h2 and h2[0] <= h[0]:
                    end = j
                    break
            return i, end
    return None


def _entries_with_spans(lines: list[str], start: int, end: int) -> list[tuple[GlossaryEntry, int, int]]:
    """Entries in lines[start:end] with the line range each one covers (continuation lines included)."""
    out: list[tuple[GlossaryEntry, int, int]] = []
    i = start
    while i < end:
        m = _ENTRY_RE.match(lines[i].rstrip("\r\n"))
        if not m:
            i += 1
            continue
        parts = [m.group(2)]
        j = i + 1
        while j < end:
            nxt = lines[j].rstrip("\r\n")
            if not nxt.strip() or _ENTRY_RE.match(nxt) or not nxt[0].isspace():
                break
            parts.append(nxt.strip())
            j += 1
        out.append((_make_entry(m.group(1), " ".join(parts)), i, j))
        i = j
    return out


def _strip_comments(body: str) -> str:
    # keep the line count, so line positions stay what they were
    return _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), body)


def parse_glossary(body: str) -> list[GlossaryEntry]:
    lines = _strip_comments(body).splitlines(keepends=True)
    span = _section_lines(lines)
    if span is None:
        return []
    return [e for e, _, _ in _entries_with_spans(lines, span[0] + 1, span[1])]


def _alias_regex(alias: str) -> re.Pattern:
    flags = 0 if _CODE_FULL_RE.fullmatch(alias) else re.IGNORECASE
    return re.compile(r"(?<![\w.])" + re.escape(alias) + r"(?!\w)", flags)


def _series_regex(prefix: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])" + re.escape(prefix) + r"\d+(?:\.\d+)*(?!\w)")


def _first_position(text: str, entry: GlossaryEntry) -> int:
    best = -1
    patterns = [_alias_regex(a) for a in entry.aliases]
    if entry.series:
        patterns.append(_series_regex(entry.series))
    for p in patterns:
        m = p.search(text)
        if m and (best < 0 or m.start() < best):
            best = m.start()
    return best


def terms_in(text: str, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """The entries that occur in text, ordered by first position."""
    found = []
    for order, entry in enumerate(entries):
        pos = _first_position(text, entry)
        if pos >= 0:
            found.append((pos, order, entry))
    found.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in found]


def _covers(entry: GlossaryEntry, term: str) -> bool:
    for alias in entry.aliases:
        if alias == term or (not _CODE_FULL_RE.fullmatch(alias) and alias.lower() == term.lower()):
            return True
    return bool(entry.series) and _series_regex(entry.series).fullmatch(term) is not None


def unknown_codes(text: str, known_text: str, entries: list[GlossaryEntry] | tuple = ()) -> list[str]:
    """Codes in text that the context does not explain: not in known_text, not covered by a series."""
    known = known_text.lower()
    seen, out = set(), []
    for m in CODE_RE.finditer(text):
        code = m.group(1)
        if code in seen or code.upper() in COMMON_CODES:
            continue
        seen.add(code)
        if code.lower() in known or any(_covers(e, code) for e in entries):
            continue
        out.append(code)
    return out


def add_glossary_line(file_text: str, term: str, definition: str) -> str:
    """Return file_text with `- TERM: definition` added to its glossary section."""
    term, definition = one_line(term), one_line(definition)
    nl = "\r\n" if "\r\n" in file_text else "\n"
    new = f"- {term}: {definition}"
    lines = file_text.splitlines(keepends=True)
    span = _section_lines(_strip_comments(file_text).splitlines(keepends=True))
    if span is None:
        base = file_text
        if base and not base.endswith(("\n", "\r")):
            base += nl
        if base:
            base += nl
        return f"{base}## Glossary{nl}{new}{nl}"
    entries = _entries_with_spans(_strip_comments(file_text).splitlines(keepends=True), span[0] + 1, span[1])
    if any(_covers(e, term) for e, _, _ in entries):
        return file_text
    at = entries[-1][2] if entries else span[0] + 1  # line index to insert before
    if at > 0 and not lines[at - 1].endswith(("\n", "\r")):
        lines[at - 1] += nl
        return "".join(lines[:at]) + new + "".join(lines[at:])
    return "".join(lines[:at]) + new + nl + "".join(lines[at:])


# --------------------------------------------------------------------------- project brief

# A comment that fills its line or lines; the lines go with it. The body cannot run past "-->".
_COMMENT_LINES_RE = re.compile(r"^[ \t]*<!--(?:(?!-->).)*-->[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n(?:[ \t]*\n){2,}")
_EMPTY_LINE_RE = re.compile(r"\n[ \t]*\n")


def _without_comments(text: str) -> str:
    """text without HTML comments. Unlike _strip_comments, the lines a comment filled are removed."""
    return _COMMENT_RE.sub("", _COMMENT_LINES_RE.sub("", text.replace("\r\n", "\n")))


def clean_brief(text: str) -> str:
    """A brief as it goes into the prompt: no HTML comments, one empty line at most in a row, stripped."""
    return _BLANK_RUN_RE.sub("\n\n", _without_comments(text)).strip()


def brief_from_body(body: str) -> str:
    """The brief held in the body of a context file.

    The body without HTML comments, without the glossary section (its heading to its end) and
    without a level-1 heading on the first line that is not empty."""
    lines = _without_comments(body).splitlines()
    span = _section_lines(lines)
    if span:
        del lines[span[0]:span[1]]
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        h = _heading(lines[first])
        if h and h[0] == 1:
            del lines[first]
    return clean_brief("\n".join(lines))


def cut_brief(text: str, max_chars: int) -> tuple[str, bool]:
    """(text, was_cut). A text longer than max_chars is cut at the last empty line that starts at
    or before the limit, and at the limit when there is none."""
    if len(text) <= max_chars:
        return text, False
    at = max_chars
    for m in _EMPTY_LINE_RE.finditer(text):
        if m.start() > max_chars:
            break
        at = m.start()
    return text[:at].rstrip(), True


# --------------------------------------------------------------------------- final prompt

_LABELS = {
    "en": {
        "note": "Note:",
        "answers": "Questions I was asked about this request, and my answers:",
        "q": "Q:", "a": "A:",
        "left": "Questions I was asked and leave to you (say what you chose):",
        "context": "Project context",
        "terms": "Project terms:",
    },
    "tr": {
        "note": "Not:",
        "answers": "Bu istekle ilgili bana sorulan sorular ve cevaplarım:",
        "q": "S:", "a": "C:",
        "left": "Bana sorulan ve kararını sana bıraktığım sorular (neyi seçtiğini söyle):",
        "context": "Proje bağlamı",
        "terms": "Proje terimleri:",
    },
}
# How the label of the project context block starts in each language (tools/replay.py takes the block out).
CONTEXT_LABELS = tuple(lab["context"] for lab in _LABELS.values())


def _state(q) -> str:
    """'skipped', 'left' or 'answered', from the answer alone."""
    a = (q.answer or "").strip()
    if not a:
        return "skipped"
    return "left" if a == "!" else "answered"


def attached_terms(text: str, note: str, questions, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    """The glossary entries that occur in the text, the note and the answers together."""
    searched = " ".join([text, note] + [q.answer for q in questions if _state(q) == "answered"])
    return terms_in(searched, entries)


def assemble(text: str, questions, note: str, entries: list[GlossaryEntry], lang: str,
             brief: str = "", project: str = "") -> list[tuple[str, str]]:
    """Segments (kind, text), kind 'user' or 'fixed'. The segment layout does not depend on lang.

    questions: objects with .question, .answer, .source and .term.
    brief: the project brief, attached as it stands under a label that names the project.
    Order: text, note, answers, questions left to the assistant, project context, project terms.
    """
    lab = _LABELS.get(lang, _LABELS["en"])
    text = text.strip()
    note = one_line(note or "")
    brief = brief.strip()
    segs: list[tuple[str, str]] = [("user", text)]

    if note:
        segs += [("fixed", f"\n\n{lab['note']} "), ("user", note)]

    is_check = lambda q: q.source == "term-check"  # noqa: E731
    answered = [q for q in questions if not is_check(q) and _state(q) == "answered"]
    left = [q for q in questions if not is_check(q) and _state(q) == "left"]
    checks = [q for q in questions if is_check(q) and _state(q) == "answered"]

    if answered:
        segs.append(("fixed", f"\n\n{lab['answers']}"))
        for q in answered:
            segs += [("fixed", f"\n- {lab['q']} "), ("user", one_line(q.question)),
                     ("fixed", f"\n  {lab['a']} "), ("user", one_line(q.answer))]
    if left:
        segs.append(("fixed", f"\n\n{lab['left']}"))
        for q in left:
            segs += [("fixed", "\n- "), ("user", one_line(q.question))]

    if brief:
        name = one_line(project)
        label = f"{lab['context']} ({name}):" if name else f"{lab['context']}:"
        segs.append(("fixed", f"\n\n{label}\n{brief}"))

    found = attached_terms(text, note, questions, entries)
    if checks or found:
        segs.append(("fixed", f"\n\n{lab['terms']}"))
        for q in checks:
            segs += [("fixed", f"\n- {q.term}: "), ("user", one_line(q.answer))]
        for e in found:
            segs.append(("fixed", f"\n- {e.key}: {e.definition}"))
    return segs


def render(segments: list[tuple[str, str]]) -> str:
    return "".join(t for _, t in segments)


# --------------------------------------------------------------------------- translation parts

_BLANK_RE = re.compile(r"[ \t\r]*\n[ \t\r]*\n\s*")
_SENTENCE_END_RE = re.compile(r"(?<![.…])[.!?](?=\s)")
_CLAUSE_END_RE = re.compile(r"(?:\.\.\.|…|[;:])(?=\s)")


def _units(block: str, end_re: re.Pattern) -> list[tuple[str, str]]:
    """Cut block after every match of end_re; the whitespace after the cut is the separator."""
    out, pos = [], 0
    for m in end_re.finditer(block):
        if m.end() <= pos:
            continue
        ws = m.end()
        while ws < len(block) and block[ws].isspace():
            ws += 1
        out.append((block[pos:m.end()], block[m.end():ws]))
        pos = ws
    if pos < len(block):
        out.append((block[pos:], ""))
    return out


def _pack(units: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    cur, sep = "", ""
    for text, s in units:
        if cur and len(cur) + len(sep) + len(text) > limit:
            parts.append((cur, sep))
            cur, sep = text, s
        else:
            cur = cur + sep + text if cur else text
            sep = s
    if cur:
        parts.append((cur, sep))
    return parts


def split_parts(text: str, limit: int) -> list[tuple[str, str]]:
    """Cut text into (part, separator) pairs; joining all of them gives text back exactly.

    First at empty lines. A block longer than limit is cut at sentence ends (never at an
    ellipsis), a sentence that is still longer at ';', ':' or an ellipsis; pieces are packed up
    to limit. A piece without any such break stays whole, even if it is longer than limit.
    """
    if not text:
        return []
    body = text.lstrip()
    lead = text[:len(text) - len(body)]
    stripped = body.rstrip()
    tail = body[len(stripped):]
    if not stripped:
        return [(text, "")]
    blocks: list[tuple[str, str]] = []
    pos = 0
    for m in _BLANK_RE.finditer(stripped):
        blocks.append((stripped[pos:m.start()], m.group(0)))
        pos = m.end()
    blocks.append((stripped[pos:], tail))

    result: list[tuple[str, str]] = []
    for block, sep in blocks:
        if len(block) <= limit:
            pieces = [(block, sep)]
        else:
            units: list[tuple[str, str]] = []
            for sentence, s in _units(block, _SENTENCE_END_RE):
                if len(sentence) > limit:
                    sub = _units(sentence, _CLAUSE_END_RE)
                    if sub:
                        sub[-1] = (sub[-1][0], sub[-1][1] + s)
                        units.extend(sub)
                        continue
                units.append((sentence, s))
            pieces = _pack(units, limit)
            pieces[-1] = (pieces[-1][0], pieces[-1][1] + sep)
        result.extend(pieces)
    result[0] = (lead + result[0][0], result[0][1])
    return result


# --------------------------------------------------------------------------- translation checks

_URL_RE = re.compile(r"https?://\S+")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def url_set(text: str) -> set[str]:
    return {u.rstrip(".,;:!?") for u in _URL_RE.findall(text)}


def number_set(text: str) -> set[str]:
    """Numbers of 2 or more digits, with . and , removed (13,049 equals 13.049)."""
    nums = {re.sub(r"[.,]", "", n) for n in _NUMBER_RE.findall(text)}
    return {n for n in nums if len(n) >= 2}


def code_set(text: str) -> set[str]:
    return set(CODE_RE.findall(text))


def check_part(source: str, output: str, source_lang: str) -> list[str]:
    """Problems found in a translated part; an empty list means the part is fine."""
    if not output.strip():
        return ["empty"]
    problems: list[str] = []
    if url_set(source) != url_set(output):
        problems.append("urls")
    ns, no = number_set(source), number_set(output)
    if ns - no:
        problems.append("numbers missing")
    if no - ns:
        problems.append("numbers added")
    if code_set(source) != code_set(output):
        problems.append("codes")
    src_len = len(source.strip())
    if src_len >= 40:
        ratio = len(output.strip()) / src_len
        if ratio < 0.5 or ratio > 2.0:
            problems.append("length")
    if source_lang and len(_words(output)) >= 4 and guess_language(output) == source_lang:
        problems.append("language")
    return problems
