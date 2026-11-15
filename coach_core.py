"""Core logic for the local prompt coach.

One model call, then code builds the prompt:
  understand: restate what the user means, ask up to a few questions, label the depth.
  build: the user's text unchanged, then the note, the answers, the questions left to
  the assistant, the project brief and the glossary lines of the terms that occur.
  No model call.

Context sources: a per-project context file (contexts/*.md) with a short brief, a glossary,
an optional brief path (a brief kept in the project's own folder) and optional include paths
(for example a project's CLAUDE.md).

Standard library only. The coach itself talks only to Ollama on localhost. The one other network
use is the launch-time update check in coach_update.py, which can be turned off.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from coach_text import (  # noqa: F401  (CODE_RE, COMMON_CODES, unknown_codes stay importable from here)
    CODE_RE, COMMON_CODES, GlossaryEntry, add_glossary_line, assemble, attached_terms,
    brief_from_body, check_part, clean_brief, cut_brief, guess_language, one_line, parse_glossary,
    quote_found, render, split_parts, unknown_codes,
)

APP_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    # First entry is the default. F3 in the TUI (or --model in the CLI) switches.
    "models": ["qwen3.5:9b", "qwen3.5:4b"],
    "num_ctx": 8192,
    "think": False,
    "temperature_understand": 0.3,
    "timeout_s": 600,
    "contexts_dir": "contexts",
    "history_dir": "history",
    "include_max_chars": 12000,
    # The project brief that is attached to the final prompt is cut to this length.
    "brief_max_chars": 2000,
    "max_questions": 3,
    # A file name under targets/ (without .json), or an object of the same shape.
    "target": "claude",
    # true: look for a newer tagged version after launch and say so (F6 installs it).
    # "install": install it on launch without asking. false: never check. See coach_update.py.
    "auto_update": True,
}

# The 1.x config.json pins these two values. Without "config_version" they count as not set.
LEGACY_CONFIG_VALUES = {"num_ctx": 16384, "max_questions": 5}

OUTPUT_LANGUAGES = ["same", "English", "Turkish"]

# Context budget: characters per token is below the lowest value measured (3.2).
CHARS_PER_TOKEN = 3.0
OUTPUT_RESERVE = 1000  # tokens kept free for the model's answer
MAX_TERM_CHECKS = 5

# Translation
LANG_CODES = {"English": "en", "Turkish": "tr"}
PART_LIMIT = 350        # characters per part
BATCH_LIMIT = 4000      # source characters per model call


class CoachError(Exception):
    """A problem the user should see as one plain line."""


# --------------------------------------------------------------------------- config


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = path or APP_DIR / "config.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "config_version" not in raw:
                for key, old in LEGACY_CONFIG_VALUES.items():
                    if raw.get(key) == old:
                        del raw[key]
            cfg.update(raw)
    return cfg


BUILTIN_TARGET = {
    "name": "the assistant",
    "depths": {
        "light": {"model": "a small, fast model"},
        "standard": {"model": "a mid-size model"},
        "deep": {"model": "the strongest model you have"},
    },
}
DEPTHS = ("light", "standard", "deep")


def _valid_target(obj) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str) or not obj["name"].strip():
        return False
    depths = obj.get("depths")
    if not isinstance(depths, dict):
        return False
    return all(isinstance(depths.get(d), dict) and isinstance(depths[d].get("model"), str)
               and depths[d]["model"].strip() and isinstance(depths[d].get("effort", ""), str)
               for d in DEPTHS)


def load_target(cfg: dict) -> dict:
    """The target named in cfg["target"]; the built-in one when it is missing or broken."""
    spec = cfg.get("target", "claude")
    if isinstance(spec, str):
        name = spec.strip()
        if not name or any(c in name for c in "/\\:") or name.startswith("."):
            return BUILTIN_TARGET
        try:
            spec = json.loads((APP_DIR / "targets" / (name + ".json")).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return BUILTIN_TARGET
    return spec if _valid_target(spec) else BUILTIN_TARGET


def resolve(path_str: str, base: Path = APP_DIR) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path_str.strip().strip('"').strip("'"))))
    return p if p.is_absolute() else (base / p)


# --------------------------------------------------------------------------- context files


@dataclass
class ProjectContext:
    key: str
    name: str
    path: Path
    body: str
    includes: list[Path] = field(default_factory=list)
    brief_path: Path | None = None

    def _brief_file(self) -> tuple[str | None, list[str]]:
        """(the brief file's text without comments, warnings). The text is None without a readable file."""
        if self.brief_path is None:
            return None, []
        if not self.brief_path.exists():
            return None, [f"brief not found: {self.brief_path}"]
        try:
            return clean_brief(self.brief_path.read_text(encoding="utf-8-sig")), []
        except (OSError, UnicodeDecodeError) as exc:
            return None, [f"cannot read {self.brief_path}: {exc}"]

    def brief(self, max_chars: int) -> tuple[str, Path, list[str]]:
        """The project brief for the final prompt: (text, the file it comes from, warnings).

        The brief file when it can be read. Otherwise the body of this file without its title
        line and its glossary. The text is empty when there is nothing to attach."""
        text, warnings = self._brief_file()
        source = self.brief_path
        if text is None or source is None:
            text, source = brief_from_body(self.body), self.path
        text, was_cut = cut_brief(text, max_chars)
        if was_cut:
            warnings.append(f"brief cut to {len(text)} characters")
        return text, source, warnings

    def render(self, max_chars: int) -> tuple[str, list[str]]:
        """What the local model reads: the body, the brief file, then the included files.

        The brief file comes before the includes, so a context cut never removes it first.
        Returns (text, warnings)."""
        parts = [self.body.strip()]
        brief, warnings = self._brief_file()
        if brief and self.brief_path:
            if len(brief) > max_chars:
                warnings.append(f"{self.brief_path.name} cut to {max_chars} characters")
                brief = brief[:max_chars]
            parts.append(f"--- project brief: {self.brief_path.name} ---\n{brief.strip()}")
        for inc in self.includes:
            if inc == self.brief_path:  # already read as the brief
                continue
            if not inc.exists():
                warnings.append(f"include not found: {inc}")
                continue
            try:
                text = inc.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                warnings.append(f"cannot read {inc}: {exc}")
                continue
            if len(text) > max_chars:
                warnings.append(f"{inc.name} cut to {max_chars} characters")
                text = text[:max_chars]
            parts.append(f"--- included file: {inc.name} ---\n{text.strip()}")
        return "\n\n".join(p for p in parts if p), warnings

    def add_terms(self, pairs: list[tuple[str, str]]) -> list[str]:
        """Add `- TERM: definition` lines to the glossary of this file. Returns the terms written."""
        try:
            raw = self.path.read_bytes()
            bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise CoachError(f"Cannot read {self.path}: {exc}") from exc
        written = []
        for term, definition in pairs:
            new = add_glossary_line(text, term, definition)
            if new != text:
                text = new
                written.append(term)
        if not written:
            return []
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise CoachError(f"Cannot write {self.path}: {exc}") from exc
        return written


def parse_context_file(path: Path) -> ProjectContext:
    """Read a context file. Optional front matter between '---' lines:

        ---
        name: Token Optimization System
        brief: D:\\Projects\\Token Optimization System\\docs\\BRIEF.md
        include:
          - D:\\Projects\\Token Optimization System\\CLAUDE.md
        ---

    brief: one path to a short brief kept in the project's own folder. Without it the brief is
    the body of this file without its title line and its glossary.
    """
    raw = path.read_text(encoding="utf-8")
    name, includes, body = path.stem, [], raw
    brief_path = None
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            end = None
        if end:
            in_include = False
            for line in lines[1:end]:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.lower().startswith("name:"):
                    name = stripped.split(":", 1)[1].strip().strip('"')
                    in_include = False
                elif stripped.lower().startswith("brief:"):
                    rest = stripped.split(":", 1)[1].strip()
                    in_include = False
                    if rest:
                        brief_path = resolve(rest, path.parent)
                elif stripped.lower().startswith("include:"):
                    rest = stripped.split(":", 1)[1].strip()
                    in_include = not rest
                    if rest:
                        includes.append(resolve(rest, path.parent))
                elif in_include and stripped.startswith("-"):
                    includes.append(resolve(stripped[1:], path.parent))
            body = "\n".join(lines[end + 1:])
    return ProjectContext(key=path.stem, name=name, path=path, body=body, includes=includes,
                          brief_path=brief_path)


def list_contexts(cfg: dict) -> list[ProjectContext]:
    folder = resolve(cfg["contexts_dir"])
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            out.append(parse_context_file(p))
        except (OSError, UnicodeDecodeError):
            continue
    return out


# --------------------------------------------------------------------------- Ollama


def _post(cfg: dict, route: str, body: dict | None, timeout: float) -> dict:
    url = cfg["ollama_url"].rstrip("/") + route
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise CoachError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise CoachError(f"Cannot reach Ollama at {cfg['ollama_url']} ({exc}). Is it running?") from exc


def ollama_models(cfg: dict) -> list[str]:
    data = _post(cfg, "/api/tags", None, timeout=5)
    return [m.get("name", "") for m in data.get("models", [])]


# What install.ps1 and the TUI tell a user who has no Ollama yet. Keep the two in step.
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
OLLAMA_SKIP_SIGN_IN = "No thanks, I'll use Ollama locally"


def ollama_installed() -> bool:
    """True when the ollama program is on this computer: on PATH, or in its usual Windows folder."""
    if shutil.which("ollama"):
        return True
    local = os.environ.get("LOCALAPPDATA")
    return bool(local) and (Path(local) / "Programs" / "Ollama" / "ollama.exe").exists()


def is_local_url(url: str) -> bool:
    """True for an address on this computer. A remote Ollama is never 'not installed'."""
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


@dataclass
class CallStats:
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0       # wall time of the whole call, including model load
    gen_seconds: float = 0.0   # generation time reported by Ollama
    num_ctx: int = 0

    @property
    def tok_per_s(self) -> float:
        return self.output_tokens / self.gen_seconds if self.gen_seconds else 0.0

    @property
    def near_limit(self) -> bool:
        return bool(self.num_ctx) and self.prompt_tokens >= 0.9 * self.num_ctx

    def line(self) -> str:
        text = (f"{self.output_tokens} tok out, {self.seconds:.1f} s, {self.tok_per_s:.0f} tok/s, "
                f"context {self.prompt_tokens / 1000:.1f}K of {self.num_ctx / 1000:.0f}K")
        if self.near_limit:
            text += "  (near the context limit: input may be cut)"
        return text


def chat_json(cfg: dict, model: str, messages: list[dict], schema: dict,
              temperature: float) -> tuple[dict, CallStats]:
    body = {
        "model": model,
        "stream": False,
        "think": cfg["think"],
        "format": schema,
        "options": {"temperature": temperature, "num_ctx": cfg["num_ctx"]},
        "messages": messages,
    }
    started = time.monotonic()
    data = _post(cfg, "/api/chat", body, timeout=cfg["timeout_s"])
    stats = CallStats(
        prompt_tokens=int(data.get("prompt_eval_count") or 0),
        output_tokens=int(data.get("eval_count") or 0),
        seconds=time.monotonic() - started,
        gen_seconds=(data.get("eval_duration") or 0) / 1e9,
        num_ctx=cfg["num_ctx"],
    )
    content = (data.get("message") or {}).get("content", "")
    try:
        return json.loads(content), stats
    except json.JSONDecodeError as exc:
        raise CoachError(f"The model did not return valid JSON: {content[:200]!r}") from exc


# --------------------------------------------------------------------------- prompts

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "missing": {"type": "string"},
                    "quote": {"type": "string"},
                    "question": {"type": "string"},
                    "kind": {"type": "string", "enum": ["fact", "material", "choice"]},
                },
                "required": ["missing", "quote", "question", "kind"],
            },
        },
        "depth": {"type": "string", "enum": list(DEPTHS)},
    },
    "required": ["understanding", "questions", "depth"],
}

UNDERSTAND_SYSTEM = """You check a text that a user is about to send to {target}, an AI assistant. You do not answer the text and you do not rewrite it. You tell the user how the text reads, and you ask for facts that are missing. Return JSON only.

understanding: Restate in two to five plain sentences what the user wants {target} to do, what {target} will work from, and what result the user expects. Write to the user in the second person ("You want {target} to ..."); never call them "the user". {target} does the work, so never write "I", "me" or "we".{address_note} State only what the text and the project context say. Do not mention your questions here.

questions: Facts that {target} needs and that only the user knows. Good questions ask what a name, code or term means, which file, version, device, number, date or audience is meant, or what already exists. At most {max_q} questions, most useful first. An empty list is a good answer when the text is clear.
For each question write "missing" first: at most 15 words that name the fact that is missing. Then write "quote": look in the text and in the project context for one sentence that states this fact, and copy that sentence word for word, in the language it is written in; write an empty string when no sentence states it. Then write the question.
Rules for questions:
- Never ask the user to make a choice that the text asks {target} to make. Never ask for a preference, an opinion or a decision the user has not made. Example: the text says "tell me which license to use". Do not ask "Which license do you want?", because choosing the license is {target}'s job.
- Do not ask about anything the text or the project context already says.
- Do not doubt what the user states about their own devices, files, versions, dates or work. The user knows these better than you do.
- Do not ask about a term, code or name that the project context defines. If one is not defined, ask what it is; never expand it from general knowledge.
- "kind" is one of three words. "material": the question asks the user to send, paste, list or attach something, such as a file, a list or a link. "choice": the question asks the user to pick, prefer or decide something. "fact": neither of the two, and the user already knows the answer.

depth: how much reasoning the task needs from {target}.
- "light": a lookup, a format change, a short rewrite, one simple fact.
- "standard": routine work with a clear path: writing, editing, summarising, research with a clear goal, routine code, carrying out an agreed plan.
- "deep": a decision with trade-offs, a design, a diagnosis, a review of evidence, strategy, or advice the user will act on.

Write "understanding", every "missing" and every question in {lang}. Write plainly, in short sentences. Do not use the em dash character."""

TURKISH_ADDRESS_NOTE = (' In Turkish use the "sen" verb forms (istiyorsun, bekliyorsun) '
                        'and never write "kullanıcı".')

EM_DASH = chr(0x2014)
_EM_DASH_RE = re.compile(r"\s*" + EM_DASH + r"\s*")


def _no_em_dash(text: str) -> str:
    return _EM_DASH_RE.sub(", ", text)


def _shared_sections(context: ProjectContext | None, context_text: str) -> str:
    if context:
        return f"PROJECT CONTEXT: {context.name}\n<context>\n{context_text}\n</context>"
    return ("PROJECT CONTEXT: none selected. Treat every project-specific term, code "
            "or name as unknown and ask about it.")


# --------------------------------------------------------------------------- session


@dataclass
class Question:
    question: str
    missing: str = ""
    answer: str = ""
    source: str = "model"  # "model" or "term-check"
    term: str = ""         # the code, for a term check


@dataclass
class Round:
    understanding: str
    questions: list[Question]
    note: str = ""
    depth: str = "standard"
    stats: CallStats | None = None
    more_terms: list[str] = field(default_factory=list)  # unknown codes that were not asked
    # Model questions the filter removed: dicts with question, missing, kind, quote, reason.
    dropped: list[dict] = field(default_factory=list)


@dataclass
class Result:
    final_prompt: str
    depth: str
    model: str
    effort: str
    terms: list[str]   # keys of the glossary lines attached to the prompt
    notes: list[str]
    brief_chars: int = 0     # length of the project brief in the prompt; 0 when it is not attached
    brief_source: str = ""   # name of the file the brief comes from; "" when it is not attached


@dataclass
class Translation:
    language: str                  # "English" or "Turkish"
    text: str                      # the whole final prompt in that language
    parts: int                     # parts that needed a translation
    kept: list[dict] = field(default_factory=list)   # [{"index": n, "problems": [...]}]
    # One entry per translation call the session has made so far. A translation that comes
    # from the session's cache adds none.
    stats: list[CallStats] = field(default_factory=list)


TRANSLATE_SYSTEM = """You translate a text into {language}. The text is a request that a user wrote for an AI assistant. The user message is a JSON object: each key is a part number and each value is one part of the text. Return a JSON object with the same keys. Each value is the translation of that part and nothing else.

Rules:
- Translate every sentence, every clause and every list item of a part. If the part lists three things, the translation lists three things. Do not shorten, summarise, explain or answer.
- Keep each part separate. Never move words from one part into another.
- Keep the user's tone and the order of the ideas. An informal sentence stays informal.
- Copy unchanged: URLs, file names, paths, code, commands, numbers, version numbers, quotation marks, and the names of people, products and projects.
- Words that are already in {language} stay as they are. A part that is already in {language} is returned unchanged."""


def _app_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _file_date(path: Path) -> str:
    """The day a file was last changed, as 2026-11-15; '' when the file cannot be looked at."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


class CoachSession:
    """Holds one run. UI code calls the methods in order:

        s = CoachSession(cfg, text, context, model, out_lang, attach_brief=True)
        rnd = s.understand()          # the one model call
        ... fill rnd.questions[i].answer, then s.set_note("...")
        res = s.build()               # no model call
        if s.needs_translation(): s.translate(progress)   # model calls for parts not translated yet
        s.new_terms()                 # [(code, answer)] not yet in the glossary
        s.save_terms()                # writes them, returns the codes written
        s.mark_copied(text)           # remembers what was copied; call save() after it
        s.save()                      # writes the history record; later calls rewrite the same file

    The project brief: brief_text, brief_source (a file name) and brief_date are read once, here.
    attach_brief says whether build() and translate() put it into the prompt. To switch, set
    attach_brief and call build() again; translate() then takes every part from the cache.
    """

    def __init__(self, cfg: dict, text: str, context: ProjectContext | None,
                 model: str, out_lang: str = "same", attach_brief: bool = True):
        self.cfg = cfg
        self.text = text.strip()
        self.context = context
        self.model = model
        self.out_lang = out_lang
        self.attach_brief = attach_brief
        self.target = load_target(cfg)
        self.text_lang = guess_language(self.text)
        self.entries: list[GlossaryEntry] = parse_glossary(context.body) if context else []
        self.warnings: list[str] = []
        self._known_text = ""
        self.brief_text = self.brief_source = self.brief_date = ""
        if context:
            self._known_text, self.warnings = context.render(cfg["include_max_chars"])
            self.brief_text, source, brief_warnings = context.brief(cfg["brief_max_chars"])
            self.brief_source, self.brief_date = source.name, _file_date(source)
            self.warnings += [w for w in brief_warnings if w not in self.warnings]
        self.context_text = self._fit_context()
        self.round: Round | None = None
        self.result: Result | None = None
        self.translation: Translation | None = None
        self.copied: dict | None = None
        self._path: Path | None = None
        self._time = ""
        # Translation results of this session: (language code, source part) -> (output, problems).
        # A part that failed its checks twice is kept as (source part, problems).
        self._translated: dict[tuple[str, str], tuple[str, list[str]]] = {}
        self._translate_stats: list[CallStats] = []

    # ---- stage 1

    def _system(self, context_text: str) -> str:
        lang = {"tr": "Turkish", "en": "English"}.get(self.text_lang, "the language of the user's text")
        head = UNDERSTAND_SYSTEM.format(
            target=self.target["name"], max_q=self.cfg["max_questions"], lang=lang,
            address_note=TURKISH_ADDRESS_NOTE if self.text_lang == "tr" else "")
        return head + "\n\n\n" + _shared_sections(self.context, context_text)

    def _user_message(self) -> str:
        return "THE USER'S TEXT:\n<<<\n" + self.text + "\n>>>"

    def _fit_context(self) -> str:
        """Cut the context so that system prompt, context and text fit into num_ctx."""
        budget = int((self.cfg["num_ctx"] - OUTPUT_RESERVE) * CHARS_PER_TOKEN)
        room = budget - len(self._system("")) - len(self._user_message())
        if room < 0:
            raise CoachError(
                f"The text is too long for the model's context (about {budget - room} characters "
                f"against room for {budget}, num_ctx {self.cfg['num_ctx']}). Shorten it or raise "
                f"num_ctx in config.json.")
        text = self._known_text
        if len(text) > room:
            text = text[:room]
            self.warnings.append(
                f"context cut to {room} characters to fit num_ctx {self.cfg['num_ctx']}; "
                f"raise num_ctx in config.json to read all of it")
        return text

    def set_note(self, text: str) -> None:
        if self.round:
            self.round.note = text.strip()

    def understand(self) -> Round:
        messages = [
            {"role": "system", "content": self._system(self.context_text)},
            {"role": "user", "content": self._user_message()},
        ]
        data, stats = chat_json(self.cfg, self.model, messages, UNDERSTAND_SCHEMA,
                                self.cfg["temperature_understand"])
        keep_dash = EM_DASH in self.text
        fix = (lambda t: t) if keep_dash else _no_em_dash

        model_questions = []
        dropped = []
        for q in data.get("questions") or []:
            if not isinstance(q, dict):
                continue
            question = fix(str(q.get("question", "")).strip())
            if not question:
                continue
            missing = str(q.get("missing", "")).strip()
            kind = str(q.get("kind", "")).strip()
            quote = str(q.get("quote", "")).strip()
            reason = ""
            if kind != "fact":
                reason = kind or "unknown"
            elif quote_found(quote, [self.context_text, self.text]):
                reason = "stated"
            if reason:
                dropped.append({"question": question, "missing": missing, "kind": kind,
                                "quote": quote, "reason": reason})
                continue
            model_questions.append(Question(question, missing=missing))
        model_questions = model_questions[: self.cfg["max_questions"]]

        checks, more = self._term_checks(model_questions)
        depth = data.get("depth")
        self.round = Round(
            understanding=fix(str(data.get("understanding", "")).strip()),
            questions=checks + model_questions,
            depth=depth if depth in DEPTHS else "standard",
            stats=stats,
            more_terms=more,
            dropped=dropped,
        )
        return self.round

    def _term_checks(self, model_questions: list[Question]) -> tuple[list[Question], list[str]]:
        asked = " ".join(q.question for q in model_questions)
        codes = [c for c in unknown_codes(self.text, self._known_text, self.entries) if c not in asked]
        checks = [Question(self._code_question(c), missing="Not defined in the selected project context.",
                           source="term-check", term=c) for c in codes[:MAX_TERM_CHECKS]]
        return checks, codes[MAX_TERM_CHECKS:]

    def _code_question(self, code: str) -> str:
        if self.text_lang == "tr":
            return f"“{code}” neyi ifade ediyor?"
        return f"What does “{code}” refer to here?"

    # ---- build

    def _label_lang(self) -> str:
        return self.text_lang

    def _brief(self) -> str:
        """The brief that goes into the prompt now: '' when it is switched off or there is none."""
        return self.brief_text if self.attach_brief and self.context else ""

    def _segments(self, lang: str) -> list[tuple[str, str]]:
        rnd = self.round
        return assemble(self.text, rnd.questions, rnd.note, self.entries, lang,
                        brief=self._brief(), project=self.context.name if self.context else "")

    def build(self) -> Result:
        """The final prompt, built in code. No model call."""
        if not self.round:
            raise CoachError("The model has not been asked yet.")
        rnd = self.round
        self.translation = None  # a new build makes an old translation stale
        segments = self._segments(self._label_lang())
        depth = rnd.depth if rnd.depth in DEPTHS else "standard"
        pick = self.target["depths"][depth]
        brief = self._brief()
        self.result = Result(
            final_prompt=render(segments),
            depth=depth,
            model=pick["model"],
            effort=pick.get("effort", ""),
            terms=[e.key for e in attached_terms(self.text, rnd.note, rnd.questions, self.entries)],
            notes=[],
            brief_chars=len(brief),
            brief_source=self.brief_source if brief else "",
        )
        return self.result

    # ---- translation

    def needs_translation(self) -> bool:
        code = LANG_CODES.get(self.out_lang)
        return bool(code) and self.text_lang != code

    def _translate_call(self, parts: dict[str, str], stats: list[CallStats]) -> dict[str, str]:
        schema = {"type": "object",
                  "properties": {k: {"type": "string"} for k in parts},
                  "required": list(parts)}
        messages = [
            {"role": "system", "content": TRANSLATE_SYSTEM.format(language=self.out_lang)},
            {"role": "user", "content": json.dumps(parts, ensure_ascii=False)},
        ]
        data, st = chat_json(self.cfg, self.model, messages, schema, 0.0)
        stats.append(st)
        if not isinstance(data, dict):
            data = {}
        out = {}
        for k, source in parts.items():
            value = str(data.get(k) or "")
            if EM_DASH not in source:
                value = _no_em_dash(value)
            out[k] = value
        return out

    def _translation_plan(self, code: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """(layout, sources) of the prompt with the labels of language code.

        layout: ("fixed", text) or ("part", key), in order. sources: key -> a part that needs a
        translation. Fixed segments (labels, glossary lines, the project brief) are never sent."""
        layout: list[tuple[str, str]] = []
        sources: dict[str, str] = {}
        for kind, text in self._segments(code):
            if kind != "user":
                layout.append(("fixed", text))
                continue
            for part, sep in split_parts(text, PART_LIMIT):
                if part.strip() and guess_language(part) != code:
                    key = str(len(sources) + 1)
                    sources[key] = part
                    layout.append(("part", key))
                else:
                    layout.append(("fixed", part))
                if sep:
                    layout.append(("fixed", sep))
        return layout, sources

    def translation_cached(self) -> bool:
        """True when translate() would make no model call: every part has a result in the cache."""
        code = LANG_CODES.get(self.out_lang)
        if not code or not self.round:
            return False
        _, sources = self._translation_plan(code)
        return all((code, part) in self._translated for part in sources.values())

    def translate(self, progress=None, fresh: bool = False) -> Translation:
        """Translate the final prompt into the output language. May raise CoachError.

        A part this session has translated before comes from the cache, so only new parts cost a
        model call. With fresh=True every part is sent again. progress(done, total) is called
        after each batch."""
        code = LANG_CODES.get(self.out_lang)
        if not code:
            raise CoachError("The output language is 'same', so there is nothing to translate.")
        if not self.round:
            raise CoachError("The model has not been asked yet.")
        layout, sources = self._translation_plan(code)
        cache, stats = self._translated, self._translate_stats

        batches: list[dict[str, str]] = []
        current: dict[str, str] = {}
        size = 0
        for key, part in sources.items():
            if not fresh and (code, part) in cache:
                continue
            if current and size + len(part) > BATCH_LIMIT:
                batches.append(current)
                current, size = {}, 0
            current[key] = part
            size += len(part)
        if current:
            batches.append(current)

        redo: list[str] = []
        for n, batch in enumerate(batches, 1):
            got = self._translate_call(batch, stats)
            for key, source in batch.items():
                if check_part(source, got[key], self.text_lang):
                    redo.append(key)
                else:
                    cache[(code, source)] = (got[key], [])
            if progress:
                progress(n, len(batches))
        for key in redo:  # one more try, alone
            source = sources[key]
            out = self._translate_call({key: source}, stats)[key]
            problems = check_part(source, out, self.text_lang)
            cache[(code, source)] = (source, problems) if problems else (out, [])

        done: dict[str, str] = {}
        kept: list[dict] = []
        for key, source in sources.items():
            done[key], problems = cache[(code, source)]
            if problems:
                kept.append({"index": int(key), "problems": problems})

        text = "".join(done[v] if kind == "part" else v for kind, v in layout)
        self.translation = Translation(self.out_lang, text, len(sources), kept, list(stats))
        return self.translation

    # ---- glossary

    def new_terms(self) -> list[tuple[str, str]]:
        """Answered term checks whose code the glossary does not cover yet."""
        if not self.round:
            return []
        out = []
        for q in self.round.questions:
            answer = q.answer.strip()
            if q.source != "term-check" or not answer or answer == "!":
                continue
            if unknown_codes(q.term, "", self.entries):
                out.append((q.term, one_line(answer)))
        return out

    def save_terms(self) -> list[str]:
        if not self.context:
            raise CoachError("No project is selected, so there is no glossary to write to.")
        written = self.context.add_terms(self.new_terms())
        if written:
            self.context.body = parse_context_file(self.context.path).body
            self.entries = parse_glossary(self.context.body)
        return written

    # ---- history

    def mark_copied(self, text: str) -> None:
        self.copied = {"text": text, "time": datetime.now().isoformat(timespec="seconds")}

    def _record(self) -> dict:
        def stats(st):
            return None if st is None else {
                "prompt_tokens": st.prompt_tokens, "output_tokens": st.output_tokens,
                "seconds": round(st.seconds, 2), "gen_seconds": round(st.gen_seconds, 2),
                "num_ctx": st.num_ctx}

        rounds = []
        if self.round:
            r = self.round
            rounds.append({
                "understanding": r.understanding, "depth": r.depth, "note": r.note,
                "questions": [{"question": q.question, "missing": q.missing, "answer": q.answer,
                               "source": q.source, "term": q.term} for q in r.questions],
                "stats": stats(r.stats),
                "dropped": r.dropped,
            })
        return {
            "format": 2,
            "app_version": _app_version(),
            "time": self._time,
            "model_used": self.model,
            "context": self.context.key if self.context else None,
            "target": self.target["name"],
            "output_language": self.out_lang,
            "text_language": self.text_lang,
            "original": self.text,
            "rounds": rounds,
            "result": None if not self.result else dict(self.result.__dict__),
            "translation": None if not self.translation else {
                "language": self.translation.language, "text": self.translation.text,
                "parts": self.translation.parts, "kept": self.translation.kept,
                "stats": [stats(st) for st in self.translation.stats]},
            "copied": self.copied,
        }

    def save(self) -> Path | None:
        """Write the history record. The first call picks the file; later calls rewrite it."""
        folder = resolve(self.cfg["history_dir"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if self._path is None:
                now = datetime.now()
                self._time = now.isoformat(timespec="seconds")
                self._path = folder / (now.strftime("%Y%m%d-%H%M%S") + ".json")
            self._path.write_text(json.dumps(self._record(), ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            return self._path
        except OSError:
            return None


# --------------------------------------------------------------------------- clipboard


def _win_clipboard():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, user32, kernel32


def _open_clipboard(user32) -> bool:
    for _ in range(20):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def copy_to_clipboard(text: str) -> bool:
    """Copy text as UTF-16 (CF_UNICODETEXT), so Turkish characters survive."""
    if sys.platform == "win32":
        ctypes, user32, kernel32 = _win_clipboard()
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"
        if not _open_clipboard(user32):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            return True  # the system owns the memory now
        finally:
            user32.CloseClipboard()
    import shutil
    import subprocess
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode("utf-8"), check=False)
            return True
    return False


def read_clipboard() -> str:
    if sys.platform == "win32":
        ctypes, user32, kernel32 = _win_clipboard()
        if not _open_clipboard(user32):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr).replace("\r\n", "\n")
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    import shutil
    import subprocess
    for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, capture_output=True).stdout.decode("utf-8", "replace")
    return ""


def ensure_utf8_console() -> None:
    """Make the Windows console read and write UTF-8 (Turkish characters)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except (AttributeError, OSError):
            pass
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
