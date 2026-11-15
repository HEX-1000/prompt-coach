"""Local prompt coach, plain terminal version. The TUI is coach_tui.py.

Usage:
    python -B prompt_coach.py "your prompt" [--project KEY] [--model NAME] [--lang same|English|Turkish]
    python -B prompt_coach.py --file prompt.txt
    python -B prompt_coach.py                  (reads the clipboard)
    python -B prompt_coach.py --list           (lists project context files)
    --no-brief                                 (leaves the project context block out of the prompt)

One model call restates what you mean and asks a few questions. Answer each one (Enter
skips, ! leaves it to the assistant), add an optional note, and the final prompt is built
in code: your text unchanged, then the note, the answers, the project brief and the project
terms. It is printed and copied to the clipboard.
Standard library only. Requires Ollama on localhost:11434.
"""
import argparse
import sys

import coach_core as core


def pick_context(cfg, key):
    contexts = core.list_contexts(cfg)
    if not key:
        return None
    for c in contexts:
        if c.key.lower() == key.lower() or c.name.lower() == key.lower():
            return c
    names = ", ".join(c.key for c in contexts) or "none"
    raise SystemExit(f"Unknown project '{key}'. Available: {names}")


def show_round(rnd):
    print("\nWhat you mean:")
    print("  " + rnd.understanding.replace("\n", "\n  "))
    if rnd.stats:
        print(f"\n[{rnd.stats.line()}]")


def depth_line(session, res):
    effort = f", effort {res.effort}" if res.effort else ""
    return f"depth {res.depth}: {res.model}{effort} ({session.target['name']})"


def translation_line(tr):
    ok = tr.parts - len(tr.kept)
    line = f"translated to {tr.language}: {ok} of {tr.parts} parts passed the checks."
    for k in tr.kept:
        line += f"\n  part {k['index']} kept in the original language: {', '.join(k['problems'])}"
    if not tr.kept:
        line += " A wrong idiom can still slip through; read it before you send it."
    return line


def main():
    core.ensure_utf8_console()
    cfg = core.load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--file")
    ap.add_argument("--project", help="context file name without .md")
    ap.add_argument("--model", default=cfg["models"][0])
    ap.add_argument("--lang", default="same", choices=core.OUTPUT_LANGUAGES)
    ap.add_argument("--no-brief", action="store_true",
                    help="leave the project context block out of the final prompt")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for c in core.list_contexts(cfg):
            print(f"{c.key:30} {c.name}")
        return 0

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.prompt:
        text = args.prompt
    else:
        text = core.read_clipboard()
    if not text.strip():
        print("No prompt given (argument, --file, or clipboard).")
        return 1

    try:
        session = core.CoachSession(cfg, text, pick_context(cfg, args.project), args.model, args.lang,
                                    attach_brief=not args.no_brief)
        for w in session.warnings:
            print(f"warning: {w}")
        rnd = session.understand()
        show_round(rnd)
        if rnd.questions:
            print("\nQuestions (Enter skips, ! leaves it to the assistant):")
            for i, q in enumerate(rnd.questions, 1):
                tag = "  [term check]" if q.source == "term-check" else ""
                print(f"\n {i}. {q.question}{tag}")
                q.answer = input("    > ").strip()
        if rnd.more_terms:
            print("\nAlso not in the glossary: " + ", ".join(rnd.more_terms))
        session.set_note(input("\nNote to add (Enter for none): "))
        res = session.build()
    except core.CoachError as exc:
        print(f"error: {exc}")
        return 1

    print("\n" + depth_line(session, res))
    if res.brief_chars:
        print(f"project context attached: {res.brief_source}, {res.brief_chars:,} characters "
              "(--no-brief leaves it out)")
    if res.terms:
        print(f"{len(res.terms)} project term(s) attached: " + ", ".join(res.terms))
    for n in res.notes:
        print(f"note: {n}")
    final = res.final_prompt
    if session.needs_translation():
        print(f"\nTranslating to {session.out_lang}...")
        try:
            tr = session.translate()
        except core.CoachError as exc:
            print(f"error: {exc}\nShowing the original prompt instead.")
        else:
            final = tr.text
            print(translation_line(tr))
    copied = core.copy_to_clipboard(final)
    if copied:
        session.mark_copied(final)
    print("\nFinal prompt" + (" (copied to clipboard)" if copied else "") + ":\n")
    print(final)

    new = session.new_terms()
    if new and session.context:
        reply = input(f"\nSave {len(new)} new term(s) to the glossary of {session.context.name}? [y/N] ")
        if reply.strip().lower() in ("y", "yes"):
            try:
                print("saved: " + ", ".join(session.save_terms()))
            except core.CoachError as exc:
                print(f"error: {exc}")
    session.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
