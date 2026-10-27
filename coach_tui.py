"""Prompt coach, terminal UI. Keyboard driven; runs on the local Ollama model.

Flow: pick a project context, write or paste the prompt, answer the coach's
questions until its understanding is right, then copy the final prompt.

Run:  python -B coach_tui.py      (needs: pip install textual)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

import coach_core as core

STATE_FILE = core.APP_DIR / ".state.json"

# Sidebar ids. The general context is always the startup choice; it has no project file.
GENERAL_KEY = "__general__"
HEADER_KEY = "__projects_header__"

# Messages from the launch-time update, shown once when the UI is up: (text, severity).
STARTUP_NOTES: list[tuple[str, str]] = []

# Quiet pastel-violet palette. One gradient only: the wordmark.
VIOLET = "#c4b5fd"
VIOLET_DIM = "#8f7fc4"
MUTED = "#8a83a0"
TEXT = "#e8e3f3"
MINT = "#a8e6cf"
ROSE = "#f4a6c6"
PEACH = "#f6c9a8"
SHEEN = ["#a99cf0", "#c4b5fd", "#ddb8f2", "#f0b4e4", "#ddb8f2", "#c4b5fd", "#a99cf0"]

STEPS = [("write", "write"), ("clarify", "clarify"), ("result", "result")]

FOOTER_HINTS = {
    "write": [("ctrl+s", "send"), ("f2", "paste clipboard"), ("tab", "focus"), ("f3", "model"),
              ("f4", "language"), ("?", "keys")],
    "clarify": [("ctrl+s", "send / confirm"), ("ctrl+b", "build now"), ("enter", "next field"),
                ("esc", "back"), ("?", "keys")],
    "result": [("y / ctrl+y", "copy"), ("tab", "edit"), ("ctrl+n", "new"), ("esc", "back"),
               ("?", "keys")],
}

HELP_TEXT = """\
[b]anywhere[/b]
  ctrl+s / f5   next step (send, send answers, confirm and build)
  ctrl+n        new prompt
  f3            switch local model
  f4            output language: same, English, Turkish
  tab           move focus (projects, text, fields)
  ?  / f1       this sheet
  ctrl+q        quit

[b]write[/b]
  ctrl+v        paste (terminal paste)
  f2            replace the text with the clipboard

[b]clarify[/b]
  enter         next answer field; on the correction field, send
  empty answer  skips the question
  ctrl+b        build the final prompt now, without another round
  esc           back to the text

[b]result[/b]
  y / ctrl+y    copy the final prompt (your edits included)
  tab           edit the final prompt; ctrl+y copies while editing
  esc           back to clarify

[dim]esc closes this sheet[/dim]"""


def sheen(text: str) -> Text:
    """Wordmark with the app's single gradient."""
    out = Text()
    visible = sum(1 for ch in text if ch != " ")
    n = 0
    for ch in text:
        if ch == " ":
            out.append(" ")
            continue
        pos = n * (len(SHEEN) - 1) / max(1, visible - 1)
        out.append(ch, style=f"bold {SHEEN[round(pos)]}")
        n += 1
    return out


def hint_line(pairs) -> Text:
    out = Text()
    for i, (key, label) in enumerate(pairs):
        if i:
            out.append("   ")
        out.append(key, style=f"bold {VIOLET}")
        out.append(" " + label, style=MUTED)
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,f1,q", "app.pop_screen", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(sheen("k e y s"), id="help-title")
            yield Static(HELP_TEXT, id="help-body")


class CoachApp(App):
    TITLE = "prompt coach"
    ENABLE_COMMAND_PALETTE = False

    CSS = f"""
    Screen {{ background: #16131f; color: {TEXT}; }}
    #top {{ height: 3; padding: 1 2 0 2; }}
    #wordmark {{ width: 1fr; }}
    #status {{ width: auto; color: {MUTED}; }}
    #body {{ height: 1fr; padding: 0 1; }}

    #sidebar {{ width: 32; padding: 0 1; border-right: vkey #2c263d; }}
    .label {{ color: {VIOLET_DIM}; text-style: bold; margin-top: 1; }}
    #projects {{ height: auto; max-height: 14; background: #16131f; border: none; padding: 0; }}
    #projects:focus {{ background: #1b1726; }}
    #projects > .option-list--option-highlighted {{ background: #2a2340; color: {VIOLET}; text-style: bold; }}
    #projects > .option-list--option-disabled {{ color: {VIOLET_DIM}; text-style: bold; }}
    #project-info, #settings, #steps {{ color: {MUTED}; }}

    #main {{ width: 1fr; padding: 0 2; }}
    TextArea {{ background: #1b1726; border: round #2c263d; padding: 0 1; }}
    TextArea:focus {{ border: round {VIOLET_DIM}; }}
    #prompt, #final {{ height: 1fr; }}
    .section {{ color: {VIOLET_DIM}; text-style: bold; margin-top: 1; }}
    .prose {{ color: {TEXT}; padding: 0 0 0 2; }}
    .muted {{ color: {MUTED}; padding: 0 0 0 2; }}
    .question {{ color: {TEXT}; margin-top: 1; }}
    .why {{ color: {MUTED}; padding-left: 4; }}
    Input {{ background: #1b1726; border: round #2c263d; margin-left: 3; }}
    Input:focus {{ border: round {VIOLET_DIM}; }}
    #questions {{ height: auto; }}
    #result-meta {{ height: auto; }}

    #footer {{ height: 1; padding: 0 2; background: #1b1726; }}

    HelpScreen {{ align: center middle; background: #0d0b12 70%; }}
    #help-box {{ width: 80; height: auto; padding: 1 2; background: #1d1929; border: round {VIOLET_DIM}; }}
    #help-title {{ margin-bottom: 1; }}
    """

    BINDINGS = [
        Binding("ctrl+s,f5", "next", "next", priority=True),
        Binding("ctrl+b", "build_now", "build now", priority=True),
        Binding("ctrl+n", "new", "new", priority=True),
        Binding("f2", "paste_clipboard", "paste clipboard"),
        Binding("f3", "cycle_model", "model", priority=True),
        Binding("f4", "cycle_lang", "language", priority=True),
        Binding("question_mark,f1", "help", "keys"),
        Binding("y,ctrl+y", "copy", "copy"),
        Binding("escape", "back", "back", priority=True),
        Binding("ctrl+q", "quit", "quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = core.load_config()
        self.state = load_state()
        self.contexts = core.list_contexts(self.cfg)
        self.context: core.ProjectContext | None = None
        models = self.cfg["models"]
        self.model = self.state.get("model") if self.state.get("model") in models else models[0]
        self.out_lang = self.state.get("out_lang", "same")
        self.step = "write"
        self.session: core.CoachSession | None = None
        self.answer_inputs: list[Input] = []
        self.busy_since: float | None = None
        self.busy_label = ""
        self.ollama_ok: bool | None = None
        self.last_stats = ""

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(sheen("◆ p r o m p t   c o a c h"), id="wordmark")
            yield Static("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield OptionList(*self._project_options(), id="projects")
                yield Static("", id="project-info")
                yield Static("SETTINGS", classes="label")
                yield Static("", id="settings")
                yield Static("STEP", classes="label")
                yield Static("", id="steps")
            with ContentSwitcher(initial="write", id="main"):
                with Vertical(id="write"):
                    yield Static("your prompt", classes="section")
                    yield TextArea("", id="prompt", soft_wrap=True)
                with VerticalScroll(id="clarify"):
                    yield Static("what you mean", classes="section")
                    yield Static("", id="understanding", classes="prose")
                    yield Static("repaired text", classes="section")
                    yield Static("", id="repaired", classes="muted")
                    yield Static("", id="questions-title", classes="section")
                    yield Vertical(id="questions")
                    yield Static("correction (optional)", classes="section")
                    yield Input(placeholder="type what is wrong in the understanding, or leave empty",
                                id="correction")
                with Vertical(id="result"):
                    yield Static("", id="result-meta")
                    yield Static("final prompt", classes="section")
                    yield TextArea("", id="final", soft_wrap=True)
        yield Static("", id="footer")

    def _project_options(self) -> list[Option]:
        """General Context on its own at the top, then a PROJECTS heading and the context files."""
        options = [Option("General Context", id=GENERAL_KEY)]
        if self.contexts:
            options.append(Option(Text("\nPROJECTS"), id=HEADER_KEY, disabled=True))
            options += [Option(c.name, id=c.key) for c in self.contexts]
        return options

    def on_mount(self) -> None:
        # Always start in General Context; a project is picked on purpose from the list.
        projects = self.query_one("#projects", OptionList)
        projects.highlighted = projects.get_option_index(GENERAL_KEY)
        self._set_context(GENERAL_KEY)
        self.query_one("#prompt", TextArea).focus()
        self.refresh_chrome()
        self.set_interval(0.25, self._tick)
        self.run_worker(self._check_ollama, thread=True, group="ping")
        for text, severity in STARTUP_NOTES:
            self.notify(text, severity=severity, timeout=8)

    # ------------------------------------------------------------------ chrome

    def refresh_chrome(self) -> None:
        dot = {True: ("●", MINT), False: ("●", ROSE), None: ("○", MUTED)}[self.ollama_ok]
        status = Text()
        if self.busy_since is not None:
            status.append(f"{self.busy_label} {time.monotonic() - self.busy_since:4.0f}s   ", style=PEACH)
        status.append(self.model, style=VIOLET)
        status.append("  ollama ", style=MUTED)
        status.append(dot[0], style=dot[1])
        self.query_one("#status", Static).update(status)

        settings = Text()
        settings.append("model    ", style=MUTED)
        settings.append(self.model + "\n", style=TEXT)
        settings.append("output   ", style=MUTED)
        settings.append(self.out_lang, style=TEXT)
        self.query_one("#settings", Static).update(settings)

        steps = Text()
        for i, (key, label) in enumerate(STEPS, 1):
            on_step = key == self.step
            steps.append(("▸ " if on_step else "  ") + f"{i} {label}\n",
                         style=f"bold {VIOLET}" if on_step else MUTED)
        if self.last_stats:
            steps.append("\n" + self.last_stats, style=MUTED)
        self.query_one("#steps", Static).update(steps)

        footer = hint_line(FOOTER_HINTS[self.step])
        self.query_one("#footer", Static).update(footer)

    def _tick(self) -> None:
        if self.busy_since is not None:
            self.refresh_chrome()

    def _check_ollama(self) -> None:
        try:
            names = core.ollama_models(self.cfg)
            ok = True
            missing = [m for m in self.cfg["models"] if m not in names]
        except core.CoachError:
            ok, missing = False, []
        self.call_from_thread(self._ollama_result, ok, missing)

    def _ollama_result(self, ok: bool, missing: list[str]) -> None:
        self.ollama_ok = ok
        if not ok:
            self.notify("Ollama is not reachable. Start it, then try again.", severity="error")
        elif missing:
            self.notify("Not pulled yet: " + ", ".join(missing), severity="warning")
        self.refresh_chrome()

    def go(self, step: str) -> None:
        self.step = step
        self.query_one("#main", ContentSwitcher).current = step
        self.refresh_chrome()

    # ------------------------------------------------------------------ project

    def _set_context(self, key: str) -> None:
        self.context = next((c for c in self.contexts if c.key == key), None)
        info = Text()
        if self.context:
            text, warnings = self.context.render(self.cfg["include_max_chars"])
            info.append(f"{len(text) / 1000:.1f}K chars", style=MUTED)
            if self.context.includes:
                info.append(f", {len(self.context.includes)} include(s)", style=MUTED)
            for w in warnings:
                info.append("\n! " + w, style=ROSE)
        else:
            info.append("no glossary: every project\nterm will be asked about", style=MUTED)
            if not self.contexts:
                info.append("\n\nno projects yet: copy\ncontexts/_template.md", style=MUTED)
        self.query_one("#project-info", Static).update(info)

    @on(OptionList.OptionHighlighted, "#projects")
    def _project_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._set_context(event.option.id)

    @on(OptionList.OptionSelected, "#projects")
    def _project_selected(self, event: OptionList.OptionSelected) -> None:
        self._set_context(event.option.id)
        if self.step == "write":
            self.query_one("#prompt", TextArea).focus()

    # ------------------------------------------------------------------ actions

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_cycle_model(self) -> None:
        models = self.cfg["models"]
        self.model = models[(models.index(self.model) + 1) % len(models)] if self.model in models else models[0]
        if self.session:
            self.session.model = self.model
        self.state["model"] = self.model
        save_state(self.state)
        self.refresh_chrome()

    def action_cycle_lang(self) -> None:
        langs = core.OUTPUT_LANGUAGES
        self.out_lang = langs[(langs.index(self.out_lang) + 1) % len(langs)]
        if self.session:
            self.session.out_lang = self.out_lang
        self.state["out_lang"] = self.out_lang
        save_state(self.state)
        self.refresh_chrome()

    def action_paste_clipboard(self) -> None:
        if self.step != "write":
            return
        text = core.read_clipboard()
        if not text.strip():
            self.notify("Clipboard is empty.", severity="warning")
            return
        area = self.query_one("#prompt", TextArea)
        area.load_text(text)
        area.focus()

    def action_new(self) -> None:
        if self.busy_since is not None:
            return
        self.session = None
        self.query_one("#prompt", TextArea).load_text("")
        self.query_one("#final", TextArea).load_text("")
        self.last_stats = ""
        self.go("write")
        self.query_one("#prompt", TextArea).focus()

    def action_back(self) -> None:
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
            return
        if self.busy_since is not None:
            return
        if self.step == "clarify":
            self.session = None
            self.go("write")
            self.query_one("#prompt", TextArea).focus()
        elif self.step == "result":
            self.go("clarify")

    def action_copy(self) -> None:
        if self.step != "result":
            return
        text = self.query_one("#final", TextArea).text
        if core.copy_to_clipboard(text):
            self.notify("Final prompt copied.")
        else:
            self.copy_to_clipboard(text)  # terminal OSC 52 fallback
            self.notify("Copied through the terminal (OSC 52).")

    def action_next(self) -> None:
        if self.busy_since is not None:
            return
        if self.step == "write":
            self._start_session()
        elif self.step == "clarify":
            self._send_answers()
        elif self.step == "result":
            self.action_copy()

    def action_build_now(self) -> None:
        if self.busy_since is None and self.step == "clarify" and self.session:
            self._collect_answers()
            self._run("building", self._compose_job)

    @on(Input.Submitted)
    def _input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "correction":
            self.action_next()
        else:
            self.screen.focus_next()

    # ------------------------------------------------------------------ stage logic

    def _start_session(self) -> None:
        text = self.query_one("#prompt", TextArea).text
        if not text.strip():
            self.notify("Write or paste a prompt first.", severity="warning")
            return
        self.session = core.CoachSession(self.cfg, text, self.context, self.model, self.out_lang)
        for w in self.session.warnings:
            self.notify(w, severity="warning")
        self._run("understanding", self._understand_job)

    def _collect_answers(self) -> None:
        rnd = self.session.rounds[-1]
        for q, inp in zip(rnd.questions, self.answer_inputs):
            q.answer = inp.value.strip()
        self.session.set_correction(self.query_one("#correction", Input).value)

    def _send_answers(self) -> None:
        if not self.session or not self.session.rounds:
            return
        self._collect_answers()
        rnd = self.session.rounds[-1]
        if any(q.answer for q in rnd.questions) or rnd.correction:
            self._run("understanding", self._understand_job)
        else:
            self._run("building", self._compose_job)  # nothing to add: understanding confirmed

    def _run(self, label: str, job) -> None:
        self.busy_since = time.monotonic()
        self.busy_label = label
        self.query_one("#main").loading = True
        self.refresh_chrome()
        self.run_worker(job, thread=True, exclusive=True, group="llm")

    def _done(self) -> None:
        self.busy_since = None
        self.query_one("#main").loading = False
        self.refresh_chrome()

    def _understand_job(self) -> None:
        try:
            rnd = self.session.understand()
        except core.CoachError as exc:
            self.call_from_thread(self._failed, str(exc))
            return
        self.call_from_thread(self._show_round, rnd)

    def _compose_job(self) -> None:
        try:
            res = self.session.compose()
            self.session.save()
        except core.CoachError as exc:
            self.call_from_thread(self._failed, str(exc))
            return
        self.call_from_thread(self._show_result, res)

    def _failed(self, message: str) -> None:
        self._done()
        self.notify(message, severity="error", timeout=10)

    async def _show_round(self, rnd: core.Round) -> None:
        self.query_one("#understanding", Static).update(Text(rnd.understanding or "(empty)"))
        self.query_one("#repaired", Static).update(Text(rnd.repaired_text))
        box = self.query_one("#questions", Vertical)
        await box.remove_children()
        self.answer_inputs = []
        title = self.query_one("#questions-title", Static)
        if rnd.questions:
            title.update(f"questions ({len(rnd.questions)})")
            widgets = []
            for i, q in enumerate(rnd.questions, 1):
                label = Text(f"{i}. ", style=f"bold {VIOLET}")
                label.append(q.question, style=TEXT)
                if q.source == "term-check":
                    label.append("  [term check]", style=MUTED)
                widgets.append(Static(label, classes="question"))
                if q.why:
                    widgets.append(Static(Text(q.why), classes="why"))
                inp = Input(placeholder="answer, or leave empty to skip")
                self.answer_inputs.append(inp)
                widgets.append(inp)
            await box.mount_all(widgets)
        else:
            title.update("no open questions. ctrl+s confirms and builds the prompt")
        self.query_one("#correction", Input).value = ""
        if rnd.stats:
            self.last_stats = rnd.stats.line()
        self._done()
        self.go("clarify")
        self.query_one("#clarify", VerticalScroll).scroll_home(animate=False)
        (self.answer_inputs[0] if self.answer_inputs else self.query_one("#correction", Input)).focus()

    def _show_result(self, res: core.Result) -> None:
        meta = Text()
        meta.append("model ", style=MUTED)
        meta.append(res.model, style=f"bold {VIOLET}")
        meta.append("   effort ", style=MUTED)
        meta.append(res.effort, style=f"bold {VIOLET}")
        if res.reason:
            meta.append("\n" + res.reason, style=TEXT)
        for n in res.notes:
            meta.append("\n• " + n, style=PEACH)
        if res.missing_context:
            meta.append("\nstill missing", style=f"bold {ROSE}")
            for item in res.missing_context:
                meta.append("\n  - " + item, style=ROSE)
        self.query_one("#result-meta", Static).update(meta)
        final = self.query_one("#final", TextArea)
        final.load_text(res.final_prompt)
        if res.stats:
            self.last_stats = res.stats.line()
        self._done()
        self.go("result")
        self.set_focus(None)  # so "y" copies; tab moves into the text to edit it
        if core.copy_to_clipboard(res.final_prompt):
            self.notify("Final prompt copied. Edit it here and press y or ctrl+y to copy again.")


def auto_update() -> int | None:
    """Look for a newer version before the UI starts (see coach_update.py).

    Returns an exit code when an update was installed and the new copy already ran in this
    process's place. Returns None to carry on starting this copy. Never raises."""
    done = os.environ.get("PROMPT_COACH_UPDATED")
    if done:  # this is the relaunch after an update
        STARTUP_NOTES.append((f"Prompt Coach updated ({done}).", "information"))
        return None
    try:
        if not core.load_config().get("auto_update", True):
            return None
        import coach_update

        result = coach_update.check_and_update(core.APP_DIR)
    except Exception:
        return None
    if result.status == "updated":
        label = f"{result.old} to {result.new}"
        print(f"Prompt Coach: updated {label}. Starting the new version...", flush=True)
        env = dict(os.environ, PROMPT_COACH_UPDATED=label)
        return subprocess.call([sys.executable, "-B", str(core.APP_DIR / "coach_tui.py")], env=env)
    if result.status in ("failed", "skipped") and result.message:
        STARTUP_NOTES.append((f"Update check: {result.message}. Still on {result.old}.", "warning"))
    return None


def main() -> None:
    core.ensure_utf8_console()
    code = auto_update()
    if code is not None:
        sys.exit(code)
    CoachApp().run()


if __name__ == "__main__":
    main()
