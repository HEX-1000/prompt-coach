"""Core logic for the local prompt coach.

Two stages, both run on a local Ollama model:
  Stage 1 (understand): restate what the user means, repair the text, ask the
  questions that are needed. Repeats with the user's answers until the user
  confirms the understanding.
  Stage 2 (compose): write the final prompt and recommend a Claude model and effort.

Context sources:
  - a per-project context file (contexts/*.md) with a glossary and optional
    include paths (for example a project's CLAUDE.md or STATE.md),
  - the user's writing rules (rules/communication_rules.md), used only for the
    coach's own behaviour and style, never copied into the final prompt.

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

APP_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    # First entry is the default. F3 in the TUI (or --model in the CLI) switches.
    "models": ["qwen3.5:9b", "qwen3.5:4b"],
    "num_ctx": 16384,
    "think": False,
    "temperature_understand": 0.3,
    "temperature_compose": 0.2,
    "timeout_s": 600,
    "rules_file": "rules/communication_rules.md",
    "contexts_dir": "contexts",
    "history_dir": "history",
    "include_max_chars": 12000,
    "max_questions": 5,
    # Check GitHub for a newer version on launch and update in place. See coach_update.py.
    "auto_update": True,
}

OUTPUT_LANGUAGES = ["same", "English", "Turkish"]
MODELS = ["haiku", "sonnet", "opusplan", "opus"]
EFFORTS = ["low", "medium", "high", "max"]


class CoachError(Exception):
    """A problem the user should see as one plain line."""


# --------------------------------------------------------------------------- config


def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = path or APP_DIR / "config.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


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

    def render(self, max_chars: int) -> tuple[str, list[str]]:
        """Glossary body plus included files. Returns (text, warnings)."""
        parts = [self.body.strip()]
        warnings = []
        for inc in self.includes:
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


def parse_context_file(path: Path) -> ProjectContext:
    """Read a context file. Optional front matter between '---' lines:

        ---
        name: Token Optimization System
        include:
          - D:\\Projects\\Claude Token Optimizer\\CLAUDE.md
        ---
    """
    raw = path.read_text(encoding="utf-8")
    name, includes, body = path.stem, [], raw
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
                elif stripped.lower().startswith("include:"):
                    rest = stripped.split(":", 1)[1].strip()
                    in_include = not rest
                    if rest:
                        includes.append(resolve(rest, path.parent))
                elif in_include and stripped.startswith("-"):
                    includes.append(resolve(stripped[1:], path.parent))
            body = "\n".join(lines[end + 1:])
    return ProjectContext(key=path.stem, name=name, path=path, body=body, includes=includes)


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


def load_rules(cfg: dict) -> str:
    path = resolve(cfg["rules_file"])
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


# --------------------------------------------------------------------------- unknown-term check

# Codes such as D5, A02, E1, V0.25.1, UE5. Small models expand these from general
# knowledge ("D5" -> Nikon D5000); this check makes sure they are asked about instead.
CODE_RE = re.compile(r"(?<![\w.])([A-Z]{1,3}\d{1,3}(?:\.\d+)*)(?![\w])")
COMMON_CODES = {
    "H264", "H265", "MP3", "MP4", "UTF8", "UTF16", "X64", "X86", "IPV4", "IPV6", "SHA1",
    "SHA256", "MD5", "S3", "EC2", "PS4", "PS5", "USB2", "USB3", "GPT4", "GPT5", "WIN10",
    "WIN11", "DDR4", "DDR5", "M1", "M2", "M3", "M4", "A4", "A3", "K8S", "4K", "8K", "B2B",
    "P2P", "L1", "L2", "L3", "HTTP2", "HTTP3", "TLS1", "C99", "C11", "C17", "C23", "ES6",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "H3", "H4", "H5", "H6", "MP2", "AV1", "VP9", "IE11",
}


def unknown_codes(text: str, known_text: str) -> list[str]:
    known = known_text.lower()
    seen, out = set(), []
    for m in CODE_RE.finditer(text):
        code = m.group(1)
        if code in seen or code.upper() in COMMON_CODES:
            continue
        seen.add(code)
        if code.lower() not in known:
            out.append(code)
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
        "repaired_text": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "why": {"type": "string"}},
                "required": ["question", "why"],
            },
        },
    },
    "required": ["understanding", "repaired_text", "questions"],
}

COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "final_prompt": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
        "model": {"type": "string", "enum": MODELS},
        "effort": {"type": "string", "enum": EFFORTS},
        "reason": {"type": "string"},
    },
    "required": ["final_prompt", "missing_context", "model", "effort", "reason"],
}

UNDERSTAND_SYSTEM = """You are a prompt coach. The user wrote a text they plan to send to Claude, an AI assistant. In this stage you make sure you understand the text before anything is rewritten. Return JSON only.

understanding: Restate in plain words what the user wants Claude to do, what Claude will work from, and what result the user expects. Address the user as "you". Two to six sentences. State only what the text, the project context or the user's answers say. If a point is unclear, leave it out of the restatement and ask about it instead.

repaired_text: The user's text with spelling, grammar and punctuation fixed. Keep the user's language, wording, tone and order of ideas. Do not add, remove or reorder requests. If the text is already correct, return it unchanged.

questions: The questions Claude would need answered to do the task well. Ask instead of guessing. Ask when:
- a term, code, name or abbreviation is not defined in the project context. Never expand it from general knowledge. Example: if "D5" is not in the project context, ask what D5 is; do not assume it is a camera or a product.
- the goal, the input, the scope, a constraint or the expected output is unclear and the answer would change the result.
Do not ask about anything the text, the project context or earlier answers already settle. Do not ask about preferences that have an obvious default. At most {max_q} questions, most important first. Each question has a short "why": what would go wrong without the answer. Return an empty list when nothing important is unclear.

Write "understanding" and the questions in the same language as the user's text ({lang_hint}). Keep the repaired text in the user's language."""

COMPOSE_SYSTEM = """You are a prompt coach. The user confirmed what their text means. Now write the final prompt they will send to Claude, and recommend a Claude model and effort level. Return JSON only.

final_prompt:
- Build it from the user's repaired text and the facts in their answers. Keep the user's voice: their wording, tone, formality and order of ideas. Change wording only where it was unclear or wrong.
- Put each clarified fact where it belongs in the text. When the answers or the project context define a project term, explain it briefly the first time it appears, for example "D5 (the chunking experiment)".
- Do not add requests, steps, constraints or output formats that the user did not ask for or confirm.
- Do not include the writing rules, a role line such as "You are an expert", a greeting you invented, or a sign-off. The user's Claude projects already contain the writing rules.
- Use headings or lists only when the user's text already had that structure or the user listed steps.
- Language of the final prompt: {out_lang}.

missing_context: facts Claude will still need that nobody has supplied. Empty list if none. Never list something the user already answered.

model: the cheapest Claude model that can do the task well.
- "haiku": lookups, formatting, short rewrites, simple factual questions.
- "sonnet": routine coding, editing, summaries, most writing, step-by-step execution of an agreed plan.
- "opusplan": a coding task that needs a design decision first, then implementation (Claude Code only).
- "opus": architecture, experiment design, decisions with trade-offs, hard debugging, reviewing evidence.

effort:
- "low" only for mechanical work: fixing typos, reformatting, looking up one fact.
- "medium" for routine work with a clear plan.
- "high" when the task needs judgment: comparing options, designing, diagnosing a problem, reviewing evidence, interpreting results, or writing whose quality depends on reasoning.
- "max" only for rare, high-stakes reviews.
When torn between two levels, choose the higher one.

reason: one or two plain sentences on why this model and effort."""


def _lang_instruction(out_lang: str) -> str:
    if out_lang == "same":
        return "the same language as the user's text"
    return f"{out_lang}. If the user wrote in another language, translate faithfully and keep their tone"


def _shared_sections(rules: str, context: ProjectContext | None, context_text: str) -> str:
    parts = []
    if rules:
        parts.append(
            "WRITING RULES. Follow them in everything you write (restatement, questions, final "
            "prompt). Never copy or summarise them into your output.\n<rules>\n" + rules + "\n</rules>"
        )
    if context:
        parts.append(f"PROJECT CONTEXT: {context.name}\n<context>\n{context_text}\n</context>")
    else:
        parts.append("PROJECT CONTEXT: none selected. Treat every project-specific term, code "
                     "or name as unknown and ask about it.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- session


@dataclass
class Question:
    question: str
    why: str = ""
    answer: str = ""
    source: str = "model"  # "model" or "term-check"


@dataclass
class Round:
    understanding: str
    repaired_text: str
    questions: list[Question]
    correction: str = ""
    stats: CallStats | None = None


@dataclass
class Result:
    final_prompt: str
    missing_context: list[str]
    model: str
    effort: str
    reason: str
    notes: list[str]
    stats: CallStats | None = None


class CoachSession:
    """Holds one run of the two-stage flow. UI code calls the methods in order:

        s = CoachSession(cfg, text, context, model, out_lang)
        rnd = s.understand()            # stage 1, first round
        ... fill rnd.questions[i].answer and/or s.set_correction("...")
        rnd = s.understand()            # next round with the answers
        res = s.compose()               # stage 2, after the user confirms
    """

    def __init__(self, cfg: dict, text: str, context: ProjectContext | None,
                 model: str, out_lang: str = "same"):
        self.cfg = cfg
        self.text = text.strip()
        self.context = context
        self.model = model
        self.out_lang = out_lang
        self.rules = load_rules(cfg)
        self.context_text, self.warnings = ("", [])
        if context:
            self.context_text, self.warnings = context.render(cfg["include_max_chars"])
        if not self.rules:
            self.warnings.append(f"rules file not found: {resolve(cfg['rules_file'])}")
        self.rounds: list[Round] = []
        self.messages: list[dict] = []
        self.asked_codes: set[str] = set()
        self.result: Result | None = None

    # ---- stage 1

    def _lang_hint(self) -> str:
        turkish = sum(self.text.count(ch) for ch in "çğıöşüÇĞİÖŞÜ")
        return "it looks Turkish" if turkish >= 2 else "detect it from the text"

    def _system_understand(self) -> str:
        head = UNDERSTAND_SYSTEM.format(max_q=self.cfg["max_questions"], lang_hint=self._lang_hint())
        return head + "\n\n" + _shared_sections(self.rules, self.context, self.context_text)

    def set_correction(self, text: str) -> None:
        if self.rounds:
            self.rounds[-1].correction = text.strip()

    def understand(self) -> Round:
        if not self.messages:
            self.messages = [
                {"role": "system", "content": self._system_understand()},
                {"role": "user", "content": "THE USER'S TEXT:\n<<<\n" + self.text + "\n>>>"},
            ]
        else:
            last = self.rounds[-1]
            self.messages.append({"role": "assistant", "content": json.dumps({
                "understanding": last.understanding,
                "repaired_text": last.repaired_text,
                "questions": [{"question": q.question, "why": q.why} for q in last.questions],
            }, ensure_ascii=False)})
            self.messages.append({"role": "user", "content": self._answers_message(last)})

        data, stats = chat_json(self.cfg, self.model, self.messages, UNDERSTAND_SCHEMA,
                                self.cfg["temperature_understand"])
        questions = [Question(str(q.get("question", "")).strip(), str(q.get("why", "")).strip())
                     for q in data.get("questions") or [] if str(q.get("question", "")).strip()]
        questions = questions[: self.cfg["max_questions"]]
        questions += self._term_check_questions(questions)
        rnd = Round(
            understanding=str(data.get("understanding", "")).strip(),
            repaired_text=str(data.get("repaired_text", "")).strip() or self.text,
            questions=questions,
            stats=stats,
        )
        self.rounds.append(rnd)
        return rnd

    def _answers_message(self, last: Round) -> str:
        lines = ["THE USER'S ANSWERS:"]
        answered = [q for q in last.questions if q.answer.strip()]
        skipped = [q for q in last.questions if not q.answer.strip()]
        for i, q in enumerate(answered, 1):
            lines.append(f"{i}. Q: {q.question}\n   A: {q.answer.strip()}")
        if skipped:
            lines.append("Skipped by the user (do not ask these again): "
                         + "; ".join(q.question for q in skipped))
        if last.correction:
            lines.append("CORRECTION FROM THE USER: " + last.correction)
        lines.append("Update the understanding and the repaired text with these facts. "
                     "Ask only what is still unclear; return an empty list if nothing is.")
        return "\n".join(lines)

    def _term_check_questions(self, model_questions: list[Question]) -> list[Question]:
        known = self.context_text + "\n" + "\n".join(
            q.question + " " + q.answer for r in self.rounds for q in r.questions)
        asked_now = " ".join(q.question for q in model_questions)
        extra = []
        for code in unknown_codes(self.text, known):
            if code in self.asked_codes or code in asked_now:
                continue
            self.asked_codes.add(code)
            extra.append(Question(
                question=self._code_question(code),
                why="Not defined in the selected project context.",
                source="term-check",
            ))
        for q in model_questions:
            for code in CODE_RE.findall(q.question):
                self.asked_codes.add(code)
        return extra

    def _code_question(self, code: str) -> str:
        if self._lang_hint() == "it looks Turkish":
            return f"“{code}” neyi ifade ediyor?"
        return f"What does “{code}” refer to here?"

    @property
    def all_questions(self) -> list[Question]:
        return [q for r in self.rounds for q in r.questions]

    # ---- stage 2

    def compose(self) -> Result:
        if not self.rounds:
            raise CoachError("Stage 1 has not run yet.")
        last = self.rounds[-1]
        system = COMPOSE_SYSTEM.format(out_lang=_lang_instruction(self.out_lang)) + "\n\n" + \
            _shared_sections(self.rules, self.context, self.context_text)
        qa = [f"Q: {q.question}\nA: {q.answer.strip()}" for q in self.all_questions if q.answer.strip()]
        corrections = [r.correction for r in self.rounds if r.correction]
        user = "\n\n".join(filter(None, [
            "THE USER'S ORIGINAL TEXT:\n<<<\n" + self.text + "\n>>>",
            "REPAIRED TEXT:\n<<<\n" + last.repaired_text + "\n>>>",
            "CONFIRMED UNDERSTANDING:\n" + last.understanding,
            ("ANSWERS:\n" + "\n\n".join(qa)) if qa else "",
            ("CORRECTIONS:\n" + "\n".join(corrections)) if corrections else "",
        ]))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        data, stats = chat_json(self.cfg, self.model, messages, COMPOSE_SCHEMA,
                                self.cfg["temperature_compose"])
        res = Result(
            final_prompt=str(data.get("final_prompt", "")).strip(),
            missing_context=[str(x).strip() for x in data.get("missing_context") or [] if str(x).strip()],
            model=data.get("model") if data.get("model") in MODELS else "sonnet",
            effort=data.get("effort") if data.get("effort") in EFFORTS else "medium",
            reason=str(data.get("reason", "")).strip(),
            notes=[],
            stats=stats,
        )
        self._guardrails(res)
        self.result = res
        return res

    def _guardrails(self, res: Result) -> None:
        """Deterministic checks the small model tends to get wrong."""
        rank = EFFORTS.index
        if res.model in ("opus", "opusplan") and rank(res.effort) < rank("high"):
            res.notes.append(f"Effort raised from {res.effort} to high: {res.model} is for judgment work.")
            res.effort = "high"
        if len(self.all_questions) >= 2 and res.effort == "low":
            res.notes.append("Effort raised from low to medium: the text needed clarification.")
            res.effort = "medium"
        if "\u2014" in res.final_prompt and "\u2014" not in self.text:
            count = res.final_prompt.count("\u2014")
            res.final_prompt = re.sub(r"\s*\u2014\s*", ", ", res.final_prompt)
            res.notes.append(f"Replaced {count} em dash(es) with commas; check those sentences.")
        if not res.final_prompt:
            res.final_prompt = self.rounds[-1].repaired_text
            res.notes.append("The model returned an empty prompt; showing the repaired text instead.")

    # ---- history

    def save(self) -> Path | None:
        folder = resolve(self.cfg["history_dir"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
            record = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "model_used": self.model,
                "context": self.context.key if self.context else None,
                "output_language": self.out_lang,
                "original": self.text,
                "rounds": [{
                    "understanding": r.understanding,
                    "repaired_text": r.repaired_text,
                    "questions": [q.__dict__ for q in r.questions],
                    "correction": r.correction,
                } for r in self.rounds],
                "result": None if not self.result else {
                    k: v for k, v in self.result.__dict__.items() if k != "stats"},
            }
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return path
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
