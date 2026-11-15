"""Replay saved Prompt Coach records. Local only, not run in CI.

python -B tools/replay.py --saved     score what the records already hold, no model
python -B tools/replay.py             run the current pipeline on every record (needs Ollama)

A replay never writes to history/*.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import coach_core as core  # noqa: E402
import coach_text as ct  # noqa: E402

LANG_CODES = {"english": "en", "turkish": "tr"}
# Phrases of the coach's own instructions that must not reach a final prompt.
LEAK_PHRASES = ("writing rules", "role line", "repaired text", "project context", "final prompt",
                "the user's text", "return json")


def app_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def answer_state(answer: str) -> str:
    a = (answer or "").strip()
    return "skipped" if not a else ("left" if a == "!" else "answered")


def score_saved(record: dict) -> dict:
    """Score result.final_prompt against original. No model call."""
    original = record.get("original", "")
    result = record.get("result") or {}
    final = result.get("final_prompt", "")
    fmt = record.get("format", 1)

    text_lang = ct.guess_language(original)
    asked_name = str(record.get("output_language", "")).strip()
    asked = LANG_CODES.get(asked_name.lower(), text_lang if asked_name.lower() == "same" else "")
    found = ct.guess_language(final)

    missing_urls = sorted(ct.url_set(original) - ct.url_set(final))
    missing_numbers = sorted(ct.number_set(original) - ct.number_set(final))
    missing_codes = sorted(ct.code_set(original) - ct.code_set(final))
    lowered_final, lowered_original = final.lower(), original.lower()
    leaks = [p for p in LEAK_PHRASES if p in lowered_final and p not in lowered_original]

    counts = {"answered": 0, "skipped": 0, "left": 0}
    questions = 0
    rounds = record.get("rounds") or []
    for r in rounds:
        for q in r.get("questions", []):
            counts[answer_state(q.get("answer", ""))] += 1
            questions += 1

    row = {
        "format": fmt,
        "context": record.get("context"),
        "original_chars": len(original),
        "final_chars": len(final),
        "length_ratio": round(len(final) / len(original), 2) if original else 0.0,
        "lang_text": text_lang,
        "lang_asked": asked,
        "lang_found": found,
        "lang_ok": bool(asked) and found == asked,
        "lang_unsure": not asked or not found,
        "missing_urls": missing_urls,
        "missing_numbers": missing_numbers,
        "missing_codes": missing_codes,
        "leaks": leaks,
        "starts_with_original": final.startswith(original.strip()),
        "rounds": len(rounds),
        "questions": questions,
        **counts,
    }
    if fmt >= 2:
        calls = [r.get("stats") or {} for r in rounds]
        calls += (record.get("translation") or {}).get("stats") or []
        row["seconds_per_call"] = [c.get("seconds") for c in calls]
        row["tokens_per_call"] = [[c.get("prompt_tokens"), c.get("output_tokens")] for c in calls]
        copied = record.get("copied")
        row["copied_differs"] = None if not copied else copied.get("text") != final
    return row


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    ratios = [r["length_ratio"] for r in rows]
    return {
        "records": n,
        "wrong_language": sum(1 for r in rows if not r["lang_ok"] and not r["lang_unsure"]),
        "language_unsure": sum(1 for r in rows if r["lang_unsure"]),
        "length_ratio_min": min(ratios) if ratios else None,
        "length_ratio_max": max(ratios) if ratios else None,
        "records_missing_urls": sum(1 for r in rows if r["missing_urls"]),
        "records_missing_numbers": sum(1 for r in rows if r["missing_numbers"]),
        "records_missing_codes": sum(1 for r in rows if r["missing_codes"]),
        "records_with_leaks": sum(1 for r in rows if r["leaks"]),
        "not_starting_with_original": sum(1 for r in rows if not r["starts_with_original"]),
        "questions": sum(r["questions"] for r in rows),
        "answered": sum(r["answered"] for r in rows),
        "skipped": sum(r["skipped"] for r in rows),
        "left": sum(r["left"] for r in rows),
    }


def print_report(entries: list[tuple[str, dict]], summary: dict) -> None:
    cols = ("record", "stage", "ratio", "asked", "found", "miss url/num/code", "leaks", "starts", "rounds", "ans/skip/left")
    print("  ".join(cols))
    for name, r in entries:
        miss = f"{len(r['missing_urls'])}/{len(r['missing_numbers'])}/{len(r['missing_codes'])}"
        print("  ".join([
            name, "saved", f"{r['length_ratio']:.2f}", r["lang_asked"] or "?", r["lang_found"] or "?", miss,
            ",".join(r["leaks"]) or "-", "yes" if r["starts_with_original"] else "NO", str(r["rounds"]),
            f"{r['answered']}/{r['skipped']}/{r['left']}",
        ]))
        details = []
        if r["missing_codes"]:
            details.append("codes " + ",".join(r["missing_codes"]))
        if r["missing_numbers"]:
            details.append("numbers " + ",".join(r["missing_numbers"]))
        if r["missing_urls"]:
            details.append("urls " + ",".join(r["missing_urls"]))
        if details:
            print("    missing: " + "; ".join(details))
    print()
    print("summary")
    for k, v in summary.items():
        print(f"  {k}: {v}")


# --------------------------------------------------------------------------- live stages

ALL_STAGES = ("understand", "build", "translate")


def check_text(original: str, final: str, asked_name: str) -> dict:
    """The same checks as score_saved, for one text produced by a live stage."""
    text_lang = ct.guess_language(original)
    asked = LANG_CODES.get(asked_name.lower(), text_lang if asked_name.lower() == "same" else "")
    found = ct.guess_language(final)
    lo_final, lo_original = final.lower(), original.lower()
    return {
        "length_ratio": round(len(final) / len(original), 2) if original else 0.0,
        "lang_asked": asked, "lang_found": found, "lang_ok": bool(asked) and found == asked,
        "missing_urls": sorted(ct.url_set(original) - ct.url_set(final)),
        "missing_numbers": sorted(ct.number_set(original) - ct.number_set(final)),
        "missing_codes": sorted(ct.code_set(original) - ct.code_set(final)),
        "leaks": [p for p in LEAK_PHRASES if p in lo_final and p not in lo_original],
    }


def saved_questions(record: dict) -> tuple[list[core.Question], str]:
    """Questions with answers and the joined notes of every round of a record (format 1 or 2)."""
    questions, notes = [], []
    for r in record.get("rounds") or []:
        for q in r.get("questions", []):
            questions.append(core.Question(q.get("question", ""), missing=q.get("missing") or q.get("why", ""),
                                           answer=q.get("answer", ""), source=q.get("source", "model"),
                                           term=q.get("term", "")))
        note = (r.get("note") or r.get("correction") or "").strip()
        if note:
            notes.append(note)
    return questions, " ".join(notes)


class Spy:
    """Records what the model returned, so dropped questions can be counted."""

    def __init__(self) -> None:
        self.raw: list = []
        self._real = core.chat_json

    def __enter__(self):
        def wrapper(*args, **kwargs):
            data, stats = self._real(*args, **kwargs)
            self.raw.append(data)
            return data, stats
        core.chat_json = wrapper
        return self

    def __exit__(self, *exc):
        core.chat_json = self._real


def run_understand(cfg: dict, model: str, record: dict, samples: int) -> list[dict]:
    rows = []
    for i in range(samples):
        session = core.CoachSession(cfg, record["original"], None, model, "same")
        with Spy() as spy:
            rnd = session.understand()
        raw = spy.raw[-1] if spy.raw and isinstance(spy.raw[-1], dict) else {}
        model_kept = [q for q in rnd.questions if q.source == "model"]
        rows.append({
            "sample": i + 1,
            "seconds": round(rnd.stats.seconds, 1),
            "prompt_tokens": rnd.stats.prompt_tokens, "output_tokens": rnd.stats.output_tokens,
            "restatement_lang": ct.guess_language(rnd.understanding) or "?",
            "text_lang": session.text_lang,
            "questions_kept": len(model_kept),
            "dropped_as_choice": sum(1 for q in (raw.get("questions") or [])
                                     if isinstance(q, dict) and q.get("kind") == "choice"),
            "term_checks": len(rnd.questions) - len(model_kept),
            "depth": rnd.depth,
        })
    return rows


def run_build(cfg: dict, model: str, record: dict) -> tuple[core.CoachSession, dict]:
    session = core.CoachSession(cfg, record["original"], None, model, record.get("output_language") or "same")
    questions, note = saved_questions(record)
    session.round = core.Round(understanding="", questions=questions, note=note, depth="standard")
    final = session.build().final_prompt
    answers = [q.answer.strip() for q in questions if q.answer.strip() and q.answer.strip() != "!"]
    return session, {
        "starts_with_original": final.startswith(session.text),
        "answers": len(answers),
        "answers_missing": sum(1 for a in answers if ct.one_line(a) not in final),
        "note_in_prompt": (not note) or ct.one_line(note) in final,
        "final_chars": len(final),
        "final_prompt": final,
        **check_text(record["original"], final, "same"),
    }


def run_translate(session: core.CoachSession, record: dict) -> dict:
    sent: list[int] = []
    real = session._translate_call

    def spy(parts, stats):
        sent.append(len(parts))
        return real(parts, stats)

    session._translate_call = spy
    tr = session.translate()
    st = tr.stats
    return {
        "language": tr.language, "calls": len(st),
        "seconds": round(sum(s.seconds for s in st), 1),
        "prompt_tokens": sum(s.prompt_tokens for s in st), "output_tokens": sum(s.output_tokens for s in st),
        "parts": tr.parts, "second_try": sum(sent) - tr.parts, "kept_in_original": len(tr.kept),
        "problems": [p for k in tr.kept for p in k["problems"]],
        **check_text(record["original"], tr.text, tr.language),
        "text": tr.text,
    }


def run_live(records: list[tuple[str, dict]], cfg: dict, model: str, samples: int, stages: list[str]) -> list[dict]:
    out = []
    for name, record in records:
        entry: dict = {"record": name, "original_chars": len(record.get("original", ""))}
        print(f"== {name} ({entry['original_chars']} chars)", flush=True)
        try:
            if "understand" in stages:
                entry["understand"] = run_understand(cfg, model, record, samples)
                for u in entry["understand"]:
                    print(f"  understand #{u['sample']}: {u['seconds']} s, {u['prompt_tokens']}/{u['output_tokens']} tok, "
                          f"restatement {u['restatement_lang']} (text {u['text_lang']}), kept {u['questions_kept']}, "
                          f"choice dropped {u['dropped_as_choice']}, term checks {u['term_checks']}, depth {u['depth']}",
                          flush=True)
            session = None
            if "build" in stages or "translate" in stages:
                session, entry["build"] = run_build(cfg, model, record)
                b = entry["build"]
                print(f"  build: starts {'yes' if b['starts_with_original'] else 'NO'}, "
                      f"answers {b['answers'] - b['answers_missing']}/{b['answers']}, "
                      f"note {'yes' if b['note_in_prompt'] else 'NO'}, ratio {b['length_ratio']}, "
                      f"leaks {','.join(b['leaks']) or '-'}", flush=True)
            if "translate" in stages and session:
                if session.needs_translation():
                    entry["translate"] = run_translate(session, record)
                    t = entry["translate"]
                    print(f"  translate to {t['language']}: {t['seconds']} s, {t['calls']} calls, parts {t['parts']}, "
                          f"second try {t['second_try']}, kept {t['kept_in_original']}, "
                          f"found {t['lang_found'] or '?'} ({'ok' if t['lang_ok'] else 'WRONG'}), ratio {t['length_ratio']}, "
                          f"miss {len(t['missing_urls'])}/{len(t['missing_numbers'])}/{len(t['missing_codes'])}"
                          + (f", problems {t['problems']}" if t["problems"] else ""), flush=True)
                else:
                    print("  translate: not needed")
        except core.CoachError as e:
            entry["error"] = str(e)
            print(f"  error: {e}", file=sys.stderr)
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Replay saved Prompt Coach records.")
    ap.add_argument("--saved", action="store_true", help="score what the records already hold, no model")
    ap.add_argument("--records", default=str(ROOT / "history" / "*.json"), help="glob of record files")
    ap.add_argument("--model", default="", help="default: the first model in config.json")
    ap.add_argument("--samples", type=int, default=2, help="stage 1 samples per record")
    ap.add_argument("--stages", default=",".join(ALL_STAGES), help="understand,build,translate")
    ap.add_argument("--no-save", action="store_true", help="print only, write no JSON report")
    args = ap.parse_args(argv)
    stages = [x.strip() for x in args.stages.split(",") if x.strip()]
    if not set(stages) <= set(ALL_STAGES):
        print(f"Unknown stage in {args.stages}. Use {','.join(ALL_STAGES)}.", file=sys.stderr)
        return 2

    files = sorted(glob.glob(args.records))
    if not files:
        print(f"No records match {args.records}", file=sys.stderr)
        return 1
    entries: list[tuple[str, dict]] = []
    records: list[tuple[str, dict]] = []
    for f in files:
        try:
            record = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Skipped {Path(f).name}: {e}", file=sys.stderr)
            continue
        records.append((Path(f).stem, record))
        entries.append((Path(f).stem, score_saved(record)))
    if not args.saved:
        cfg = core.load_config()
        model = args.model or (cfg.get("models") or [""])[0]
        if not model:
            print("No model: pass --model or set models in config.json.", file=sys.stderr)
            return 1
        print(f"Live replay, model {model}, app {app_version()}, stages {','.join(stages)}\n")
        live = run_live(records, cfg, model, args.samples, stages)
        if not args.no_save:
            out_dir = ROOT / "history" / "replays"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{app_version()}-live.json"
            payload = {"app_version": app_version(), "stage": "live", "model": model, "stages": stages,
                       "samples": args.samples, "records": live}
            out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"\nWrote {out.relative_to(ROOT).as_posix()}")
        return 0
    summary = summarize([r for _, r in entries])
    print_report(entries, summary)

    if not args.no_save:
        out_dir = ROOT / "history" / "replays"
        out_dir.mkdir(parents=True, exist_ok=True)
        version = app_version()
        out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{version}-saved.json"
        payload = {"app_version": version, "stage": "saved",
                   "records": [{"record": name, **r} for name, r in entries], "summary": summary}
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
