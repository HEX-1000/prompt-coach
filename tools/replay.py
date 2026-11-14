"""Replay saved Prompt Coach records. Local only, not run in CI.

python -B tools/replay.py --saved     score what the records already hold, no model

Live stages (understand, build, translate) are not written yet. See docs/DESIGN-core.md, section 12.
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


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Replay saved Prompt Coach records.")
    ap.add_argument("--saved", action="store_true", help="score what the records already hold, no model")
    ap.add_argument("--records", default=str(ROOT / "history" / "*.json"), help="glob of record files")
    ap.add_argument("--no-save", action="store_true", help="print only, write no JSON report")
    args = ap.parse_args(argv)
    if not args.saved:
        print("Live stages are not written yet (design step 5). Use --saved.", file=sys.stderr)
        return 2

    files = sorted(glob.glob(args.records))
    if not files:
        print(f"No records match {args.records}", file=sys.stderr)
        return 1
    entries: list[tuple[str, dict]] = []
    for f in files:
        try:
            record = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Skipped {Path(f).name}: {e}", file=sys.stderr)
            continue
        entries.append((Path(f).stem, score_saved(record)))
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
