# Changelog

What changed in each version, newest first. After an update the app shows the entries
between your old version and the new one. Keep each entry to one short line a user can
understand. The version number lives in `VERSION`; raising it is what makes an update reach everyone.

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
