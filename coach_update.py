"""Self-update for Prompt Coach. Standard library only.

On launch the TUI calls check_and_update(). It reads one small file, VERSION, from the
official GitHub repository. If that version is newer than the local VERSION file, it
installs the new code, then the TUI starts again from the updated files.

What an update touches
  - Code, docs, assets, requirements.txt, contexts/_template.md and rules/README.md are replaced.
  - Never touched: your contexts, rules, history, .state.json, config.json, .venv, .git.
  - Nothing is deleted. The files an update replaces are copied to .update_backup/ first.

How it updates
  - A git clone whose origin is the official repository: git pull --ff-only, and only when
    no tracked file other than config.json has local edits.
  - Any other copy (a downloaded ZIP): the repository ZIP is downloaded, every Python file is
    syntax-checked, and only then are files written.

After an update the app shows the entries of CHANGELOG.md between your old version and the
new one (whats_new). CHANGELOG.md is shipped like any other file of the repository.

Nothing about you is sent: the requests are plain GETs to raw.githubusercontent.com and
github.com. Turn it off with "auto_update": false in config.json.

Manual run:  python -B coach_update.py
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO = "HEX-1000/prompt-coach"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

BACKUP_DIR = ".update_backup"
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024

# Paths an update never writes, wherever they appear.
PROTECTED_PARTS = {".git", ".venv", "history", BACKUP_DIR, "__pycache__"}
PROTECTED_FILES = {".state.json", "config.json"}
# Inside contexts/ and rules/ only these shipped files are updated; the rest is the user's.
SHIPPED_IN_USER_DIRS = {"contexts/_template.md", "rules/README.md"}
USER_DIRS = {"contexts", "rules"}
REQUIRED_FILES = ("VERSION", "coach_core.py", "coach_tui.py")


class UpdateError(Exception):
    """An expected failure with a message the user can read."""


@dataclass
class UpdateResult:
    # current | updated | offline | skipped | failed
    status: str
    old: str = ""
    new: str = ""
    message: str = ""


# --------------------------------------------------------------------------- versions


def parse_version(text: str) -> tuple[int, ...] | None:
    text = text.strip()
    if not re.fullmatch(r"\d+(\.\d+){0,3}", text):
        return None
    return tuple(int(p) for p in text.split("."))


def read_local_version(app_dir: Path) -> str:
    try:
        return (app_dir / "VERSION").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


def is_newer(remote: str, local: str) -> bool:
    r, loc = parse_version(remote), parse_version(local) or (0,)
    return r is not None and r > loc


# --------------------------------------------------------------------------- paths


def is_protected(rel: str) -> bool:
    """True for a path inside the zip that must never overwrite the user's files."""
    parts = PurePosixPath(rel).parts
    if not parts or any(p in PROTECTED_PARTS for p in parts):
        return True
    if len(parts) == 1 and parts[0] in PROTECTED_FILES:
        return True
    if parts[0] in USER_DIRS and rel not in SHIPPED_IN_USER_DIRS:
        return True
    return False


def _safe_relative(name: str) -> str:
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UpdateError(f"unsafe path in the download: {name}")
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("..", "") for p in parts):
        raise UpdateError(f"unsafe path in the download: {name}")
    return "/".join(parts)


def _same(a: bytes, b: bytes) -> bool:
    """Equal, ignoring CRLF versus LF (Git on Windows may convert line endings)."""
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


# --------------------------------------------------------------------------- network


def _fetch(url: str, timeout: float, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "prompt-coach-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is larger than expected")
    return data


# --------------------------------------------------------------------------- pip


def _pip_install(requirements: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)],
        capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise UpdateError(f"pip could not install the new requirements: {last[0]}")


# --------------------------------------------------------------------------- zip update


def _read_zip(data: bytes) -> dict[str, bytes]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UpdateError("the download is not a valid zip file") from exc
    files: dict[str, bytes] = {}
    with zf:
        members = [i for i in zf.infolist() if not i.filename.endswith("/")]
        tops = {i.filename.split("/", 1)[0] for i in members}
        if len(tops) != 1 or any("/" not in i.filename for i in members):
            raise UpdateError("the zip does not have the expected single top folder")
        top = tops.pop()
        for info in members:
            rel = _safe_relative(info.filename[len(top) + 1:])
            if info.file_size > MAX_FILE_BYTES:
                raise UpdateError(f"file too large in the download: {rel}")
            files[rel] = zf.read(info)
    return files


def _check_files(files: dict[str, bytes], old: str) -> str:
    for name in REQUIRED_FILES:
        if name not in files:
            raise UpdateError(f"the download has no {name}")
    new = files["VERSION"].decode("utf-8", "replace").strip()
    if not is_newer(new, old):
        raise UpdateError(f"the download is version {new}, not newer than {old}")
    for rel, data in files.items():
        if rel.endswith(".py"):
            try:
                compile(data, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                raise UpdateError(f"{rel} in the download does not compile: {exc}") from exc
    return new


def _target_for(app_dir: Path, rel: str) -> Path | None:
    """Where an update may write this file, or None to leave it alone."""
    target = app_dir / rel
    root = app_dir.resolve()
    if root != target.resolve() and root not in target.resolve().parents:
        raise UpdateError(f"unsafe path in the download: {rel}")
    if is_protected(rel):
        # config.json is the one protected file that is created when it is missing.
        if rel == "config.json" and not target.exists():
            return target
        return None
    return target


def _update_zip(app_dir: Path, zip_url: str, old: str, timeout: float) -> UpdateResult:
    try:
        data = _fetch(zip_url, max(timeout, 30.0), MAX_ZIP_BYTES)
    except (OSError, ValueError) as exc:
        raise UpdateError(f"could not download the update: {exc}") from exc
    files = _read_zip(data)
    new = _check_files(files, old)

    changes: list[tuple[str, Path]] = []
    for rel in sorted(files):
        if rel == "VERSION":
            continue
        target = _target_for(app_dir, rel)
        if target is None:
            continue
        if target.exists() and _same(target.read_bytes(), files[rel]):
            continue
        changes.append((rel, target))

    if any(rel == "requirements.txt" for rel, _ in changes):
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_bytes(files["requirements.txt"])
            _pip_install(req)

    backup = app_dir / BACKUP_DIR
    shutil.rmtree(backup, ignore_errors=True)
    done: list[tuple[str, Path]] = []
    try:
        for rel, target in changes:
            if target.exists():
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = target.with_name(target.name + ".new")
            tmp_file.write_bytes(files[rel])
            os.replace(tmp_file, target)
            done.append((rel, target))
        version_file = app_dir / "VERSION"
        tmp_file = version_file.with_name("VERSION.new")
        tmp_file.write_bytes(files["VERSION"])
        os.replace(tmp_file, version_file)
    except OSError as exc:
        for rel, target in done:
            saved = backup / rel
            if saved.exists():
                shutil.copy2(saved, target)
            else:
                target.unlink(missing_ok=True)
        raise UpdateError(f"could not write the update, old files restored: {exc}") from exc
    return UpdateResult("updated", old, new, f"{len(changes)} file(s) replaced")


# --------------------------------------------------------------------------- git update


def _git(app_dir: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.run(
        ["git", "-C", str(app_dir), *args], capture_output=True, text=True,
        timeout=timeout, stdin=subprocess.DEVNULL, env=env,
    )
    # rstrip only: the first column of `git status --porcelain` can be a space.
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def _update_git(app_dir: Path, old: str, origin_match: str, branch: str) -> UpdateResult:
    rc, origin = _git(app_dir, "remote", "get-url", "origin")
    if rc != 0 or origin_match.lower() not in origin.lower():
        return UpdateResult("skipped", old, "", "this clone does not point at the official repository")
    rc, current = _git(app_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or current != branch:
        return UpdateResult("skipped", old, "", f"not on branch {branch}")
    rc, status = _git(app_dir, "status", "--porcelain", "--untracked-files=no")
    dirty = [ln for ln in status.splitlines() if ln[3:].strip().strip('"') != "config.json"]
    if rc != 0 or dirty:
        return UpdateResult("skipped", old, "", "tracked files have local edits; run git pull yourself")

    req_path = app_dir / "requirements.txt"
    req_before = req_path.read_bytes() if req_path.exists() else b""
    rc, out = _git(app_dir, "pull", "--ff-only", "--quiet", "origin", branch, timeout=120)
    if rc != 0:
        last = out.splitlines()[-1:] or ["no output"]
        raise UpdateError(f"git pull failed: {last[0]}")
    new = read_local_version(app_dir)
    if req_path.exists() and not _same(req_before, req_path.read_bytes()):
        _pip_install(req_path)
    return UpdateResult("updated" if is_newer(new, old) else "current", old, new, "git pull")


# --------------------------------------------------------------------------- entry point


def check_and_update(
    app_dir: Path,
    *,
    version_url: str = VERSION_URL,
    zip_url: str = ZIP_URL,
    origin_match: str = REPO,
    branch: str = BRANCH,
    timeout: float = 4.0,
) -> UpdateResult:
    """Compare VERSION with GitHub and install a newer copy. Never raises."""
    app_dir = Path(app_dir).resolve()
    old = read_local_version(app_dir)
    try:
        remote = _fetch(version_url, timeout, 64).decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return UpdateResult("offline", old)
    if parse_version(remote) is None:
        return UpdateResult("failed", old, remote[:20], "the version file on GitHub is not a version number")
    if not is_newer(remote, old):
        return UpdateResult("current", old, remote)
    try:
        if (app_dir / ".git").exists() and shutil.which("git"):
            result = _update_git(app_dir, old, origin_match, branch)
        else:
            result = _update_zip(app_dir, zip_url, old, timeout)
    except UpdateError as exc:
        return UpdateResult("failed", old, remote, str(exc))
    except Exception as exc:  # an update problem must never stop the app from starting
        return UpdateResult("failed", old, remote, f"{type(exc).__name__}: {exc}")
    return result


# --------------------------------------------------------------------------- what's new

CHANGELOG = "CHANGELOG.md"
SECTION_RE = re.compile(r"^##\s+v?(\d+(?:\.\d+){0,3})\s*$")


def changes_between(text: str, old: str, new: str, limit: int = 5) -> list[str]:
    """The entries of CHANGELOG.md for versions after old, up to and including new.

    One version gives its lines as they are. Several give each line a "version: " prefix.
    More than limit lines are cut, with a pointer to the file."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = SECTION_RE.match(line.strip())
        if heading:
            sections.append((heading.group(1), []))
        elif sections and line.lstrip().startswith("- "):
            sections[-1][1].append(line.lstrip()[2:].strip())
    wanted = [(v, items) for v, items in sections if is_newer(v, old) and not is_newer(v, new)]
    several = len(wanted) > 1
    lines = [f"{v}: {item}" if several else item for v, items in wanted for item in items]
    if len(lines) > limit:
        lines = lines[:limit] + [f"and {len(lines) - limit} more in {CHANGELOG}"]
    return lines


def whats_new(app_dir: Path | None, old: str, new: str) -> list[str]:
    """What changed between two versions, from CHANGELOG.md in the app folder. Never raises."""
    if app_dir is None:
        return []
    try:
        return changes_between((Path(app_dir) / CHANGELOG).read_text(encoding="utf-8"), old, new)
    except (OSError, ValueError):
        return []


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    result = check_and_update(app_dir)
    print(f"{result.status}: local {result.old}, GitHub {result.new or '?'} {result.message}".strip())
    if result.status == "updated":
        news = whats_new(app_dir, result.old, result.new)
        if news:
            print("What's new:")
            for line in news:
                print(f"  - {line}")
    return 0 if result.status in ("current", "updated", "offline") else 1


if __name__ == "__main__":
    sys.exit(main())
