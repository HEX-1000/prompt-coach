# Contributing

Bug reports and small fixes are welcome. For anything larger, open an issue first so we can agree on the change before you write it.

## Setup

You need Python 3.10 or newer.

```
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -v
```

The tests need no Ollama and no network.

## Rules the tests enforce

- No em dash character in tracked text files.
- PowerShell scripts are plain ASCII.
- `VERSION` is one line.

## Code

`coach_core.py` and `coach_update.py` use the standard library only. The TUI needs Textual. Code and comments are in English.

## Personal data

Never commit `contexts/` (except `_template.md`), `rules/` (except `README.md`), `history/` or `.state.json`. `.gitignore` covers them. Do not paste private prompts into issues.

## Changes and versions

A change a user will notice gets one short line in `CHANGELOG.md`. Do not change `VERSION` in a pull request: the maintainer raises it at release, and raising it updates every installed copy.

## License

Contributions are under the MIT license of this repository.
