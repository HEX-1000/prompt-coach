# Changelog

What changed in each version, newest first. After an update the app shows the entries
between your old version and the new one. Keep each entry to one short line a user can
understand. The version number lives in `VERSION`; raising it is what makes an update reach everyone.

## 2.0.0

- One model call instead of several: the coach restates what you mean and asks up to 3 questions. It no longer rewrites your text.
- Your text goes into the final prompt unchanged, followed by your note, your answers and the glossary lines of the terms it uses. The model does not write the prompt, so nothing is lost or reworded.
- In the question fields, Enter skips and `!` (or `ctrl+l`) leaves the question to the assistant. The note field takes anything you want to add.
- Output language English or Turkish translates the prompt part by part and checks every part. `o` shows the original, `t` translates again.
- `g` on the result screen saves answered term checks to the glossary of the project.
- The model and effort come from `targets/claude.json`, not from the model.
- The default context is 8192 tokens and the default is 3 questions. A long project context is cut with a warning instead of being cut silently.
- History records have format 2 with the app version, timings and what you copied. Older records still read.
- The coach no longer reads `rules/communication_rules.md`, and `rules_file` and `temperature_compose` in `config.json` are ignored.
- `tools/replay.py` scores saved records and replays the pipeline on them. It is a local tool, not part of the app.

## 1.3.0

- After an update the app lists what changed since your version.
- The app says when Ollama is not installed, with the download link and how to skip its sign-in prompt.
- `install.ps1` finds any Python 3.10 or newer, not only 3.14.

## 1.2.0

- The empty prompt box lists the first keys to know.
- `f1` opens the key sheet from anywhere, and the footer shows `f1` and `ctrl+q`.
- The Ollama notices say what to do: start Ollama, or run the `ollama pull` command (or press `f3` to use a model you have).
- The app checks Ollama again when you press `ctrl+s`.
- `install.ps1` checks that Ollama is installed and running and that a model is downloaded.

## 1.1.0

- The project list starts with General Context. The app always opens in it, with the prompt box ready to type.
- The app checks GitHub for a newer version when it starts and updates itself. Turn it off with `"auto_update": false` in `config.json`.
- An update never touches your contexts, rules, history or `config.json`, and copies the files it replaces to `.update_backup/`.
