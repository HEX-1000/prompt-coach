"""Local prompt coach, plain terminal version. The TUI is coach_tui.py.

Usage:
    python -B prompt_coach.py "your prompt" [--project KEY] [--model NAME] [--lang same|English|Turkish]
    python -B prompt_coach.py --file prompt.txt
    python -B prompt_coach.py                  (reads the clipboard)
    python -B prompt_coach.py --list           (lists project context files)

Stage 1 restates what you mean and asks questions. Answer each one (Enter skips),
then confirm with Enter or type a correction. Stage 2 prints the final prompt and
the model/effort recommendation and copies the prompt to the clipboard.
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
    print("\nUnderstanding:")
    print("  " + rnd.understanding.replace("\n", "\n  "))
    print("\nRepaired text:")
    print("  " + rnd.repaired_text.replace("\n", "\n  "))
    if rnd.stats:
        print(f"\n[{rnd.stats.line()}]")


def main():
    core.ensure_utf8_console()
    cfg = core.load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--file")
    ap.add_argument("--project", help="context file name without .md")
    ap.add_argument("--model", default=cfg["models"][0])
    ap.add_argument("--lang", default="same", choices=core.OUTPUT_LANGUAGES)
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

    session = core.CoachSession(cfg, text, pick_context(cfg, args.project), args.model, args.lang)
    for w in session.warnings:
        print(f"warning: {w}")

    try:
        rnd = session.understand()
        while True:
            show_round(rnd)
            if rnd.questions:
                print("\nQuestions (Enter skips):")
                for i, q in enumerate(rnd.questions, 1):
                    print(f"\n {i}. {q.question}")
                    if q.why:
                        print(f"    why: {q.why}")
                    q.answer = input("    > ").strip()
                if any(q.answer for q in rnd.questions):
                    rnd = session.understand()
                    continue
            fix = input("\nEnter = understanding is right. Or type a correction: ").strip()
            if not fix:
                break
            session.set_correction(fix)
            rnd = session.understand()

        res = session.compose()
    except core.CoachError as exc:
        print(f"error: {exc}")
        return 1

    print(f"\nModel: {res.model}   Effort: {res.effort}")
    print(f"Why: {res.reason}")
    for n in res.notes:
        print(f"note: {n}")
    if res.missing_context:
        print("Still missing:")
        for item in res.missing_context:
            print(f"  - {item}")
    copied = core.copy_to_clipboard(res.final_prompt)
    print("\nFinal prompt" + (" (copied to clipboard)" if copied else "") + ":\n")
    print(res.final_prompt)
    if res.stats:
        print(f"\n[{res.stats.line()}]")
    session.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
