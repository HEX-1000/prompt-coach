"""Local prompt coach: cleans up a prompt and recommends a Claude model and effort.

Runs on the local Ollama model, so it costs no Claude usage.
Usage:
    python -B tools/prompt_coach.py "your prompt"
    python -B tools/prompt_coach.py --file prompt.txt
    python -B tools/prompt_coach.py            (reads the Windows clipboard)
The corrected prompt is copied back to the clipboard on Windows.
Standard library only. Requires Ollama running on localhost:11434.
"""
import argparse
import json
import subprocess
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3.5:4b"

SYSTEM = """You review a prompt that the user is about to send to Claude.
Tasks:
1. Fix spelling and grammar. Keep the user's language, voice and meaning. Do not add requests the user did not make.
2. List missing context that Claude would need to ask about (files, goal, constraints, output format). Empty list if none.
3. Recommend the cheapest model that can do the task well:
   - "haiku": lookups, formatting, short rewrites, simple factual questions.
   - "sonnet": routine coding, editing, summaries, most writing, step-by-step execution of an agreed plan.
   - "opusplan": a coding task that needs a design decision first, then implementation (Claude Code only).
   - "opus": architecture, experiment design, decisions with trade-offs, hard debugging, reviewing evidence.
4. Recommend effort: "low" for trivial tasks, "medium" by default, "high" for decisions and hard reasoning, "max" only for rare, high-stakes reviews.
Answer with JSON only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "corrected_prompt": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
        "model": {"type": "string", "enum": ["haiku", "sonnet", "opusplan", "opus"]},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "max"]},
        "reason": {"type": "string"},
    },
    "required": ["corrected_prompt", "missing_context", "model", "effort", "reason"],
}


def read_clipboard():
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, encoding="utf-8").stdout
    except FileNotFoundError:
        return ""


def write_clipboard(text):
    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False)
    except FileNotFoundError:
        pass


def ask(prompt):
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM + "\nJSON schema: " + json.dumps(SCHEMA)},
            {"role": "user", "content": "PROMPT TO REVIEW:\n" + prompt},
        ],
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return json.loads(data["message"]["content"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--file")
    args = ap.parse_args()
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            prompt = f.read()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = read_clipboard()
    if not prompt.strip():
        print("No prompt given (argument, --file, or clipboard).")
        return 1
    result = ask(prompt)
    print(f"Model: {result['model']}   Effort: {result['effort']}")
    print(f"Why: {result['reason']}")
    if result["missing_context"]:
        print("Missing context:")
        for item in result["missing_context"]:
            print(f"  - {item}")
    print("\nCorrected prompt (copied to clipboard):\n")
    print(result["corrected_prompt"])
    write_clipboard(result["corrected_prompt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
