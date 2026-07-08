"""Mirror the local data to a private git repository (e.g. a GitHub backup).

This keeps a small git working tree (``config.GIT_BACKUP_DIR``) into which the
live data files are copied, then commits and pushes them to the configured
remote. It is intentionally separate from the live ``data/`` directory so the
SQLite DB can be copied safely and transient caches/logs are never pushed.

Authentication relies on whatever git credential helper the user already has
configured (the same one that lets the main repo push). No tokens are stored
or read by this module.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import config
from .profiles import list_profiles


class GitBackupError(RuntimeError):
    """Raised when a git backup/restore step fails."""


def _run(args: list[str], cwd: Path) -> str:
    """Run a git command, returning stdout; raise GitBackupError on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitBackupError(
            "Git is not installed or not on PATH — install Git to use GitHub backup."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitBackupError(f"git {' '.join(args)} failed: {detail}")
    return (proc.stdout or "").strip()


def _remote_url() -> str:
    url = (config.DATA_BACKUP_REMOTE or "").strip()
    if not url:
        raise GitBackupError(
            "No backup repo configured. Set 'DATA_BACKUP_REMOTE' in Settings "
            "to your private git repo URL first."
        )
    return url


# Files/dirs to ignore inside the backup repo (transient caches & logs).
_GITIGNORE = """\
tts_cache/
*.out
*.err
*.log
"""

_README = """\
# Job Tracker data backup

Private mirror of the Job Tracker data created by the app's
**Back up to GitHub** button. Contains ALL profiles (`profiles/<name>/`):
each profile's SQLite database, match profile, pitch, generated/tailored
resumes and settings, plus (optionally) the default profile's `.env`.

Restore with the app's **Restore from GitHub** button, or copy the files back
into your local `data/profiles/` folder and `.env`.
"""


def _ensure_repo(repo: Path, url: str) -> None:
    """Make sure ``repo`` is a git repo on branch ``main`` with origin=url."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _run(["init"], repo)
        _run(["branch", "-M", "main"], repo)

    # Point origin at the configured URL (add or update).
    remotes = _run(["remote"], repo).split()
    if "origin" in remotes:
        _run(["remote", "set-url", "origin", url], repo)
    else:
        _run(["remote", "add", "origin", url], repo)


def _copy_data_in(repo: Path, include_env: bool) -> None:
    """Copy ALL profiles' data files into the backup working tree."""
    (repo / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (repo / "README.md").write_text(_README, encoding="utf-8")

    # Mirror every profile folder, dropping deletions (each profile's .env,
    # DB, pitch, resumes... live inside its folder; the historical default
    # profile keeps its settings in the root .env, copied below).
    dst_profiles = repo / "profiles"
    if dst_profiles.exists():
        shutil.rmtree(dst_profiles)
    for name in list_profiles():
        shutil.copytree(
            config.PROFILES_DIR / name, dst_profiles / name,
            ignore=shutil.ignore_patterns("tts_cache",
                                          *(() if include_env else (".env",))))

    if config.ACTIVE_PROFILE_FILE.exists():
        shutil.copy2(config.ACTIVE_PROFILE_FILE, repo / "active_profile")
    if include_env and config.ROOT_ENV_PATH.exists():
        shutil.copy2(config.ROOT_ENV_PATH, repo / ".env")


def push_to_github(message: str | None = None, *, include_env: bool = True) -> str:
    """Copy data into the backup repo, commit and push. Returns a status note."""
    url = _remote_url()
    repo = config.GIT_BACKUP_DIR
    _ensure_repo(repo, url)
    _copy_data_in(repo, include_env)

    _run(["add", "-A"], repo)
    if not _run(["status", "--porcelain"], repo):
        return "Already up to date — no changes to back up."

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _run(["commit", "-m", message or f"Backup {stamp}"], repo)
    # Push current branch to origin/main, setting upstream on first push.
    _run(["push", "-u", "origin", "HEAD:main"], repo)
    return f"Backed up to GitHub at {stamp}."


def restore_from_github() -> list[str]:
    """Pull the latest backup and copy it back over the live data. Overwrites
    every backed-up profile (and .env if present in the backup). Backups made
    before multi-profile support restore into the currently active profile."""
    url = _remote_url()
    repo = config.GIT_BACKUP_DIR
    _ensure_repo(repo, url)

    _run(["fetch", "origin", "main"], repo)
    _run(["reset", "--hard", "origin/main"], repo)

    restored: list[str] = []
    src_profiles = repo / "profiles"

    if src_profiles.is_dir():
        for pdir in sorted(p for p in src_profiles.iterdir() if p.is_dir()):
            shutil.copytree(pdir, config.PROFILES_DIR / pdir.name,
                            dirs_exist_ok=True)
            restored.append(f"profiles/{pdir.name}/")
        if (repo / "active_profile").exists():
            shutil.copy2(repo / "active_profile", config.ACTIVE_PROFILE_FILE)
            restored.append("active_profile")
        if (repo / ".env").exists():
            shutil.copy2(repo / ".env", config.ROOT_ENV_PATH)
            restored.append(".env")
    else:
        # Legacy single-profile backup layout -> active profile.
        mapping = {
            config.DB_PATH.name: config.DB_PATH,
            config.PROFILE_PATH.name: config.PROFILE_PATH,
            config.PITCH_PATH.name: config.PITCH_PATH,
            config.BUILT_RESUME_PATH.name: config.BUILT_RESUME_PATH,
            ".env": config.ENV_PATH,
        }
        for name, target in mapping.items():
            src = repo / name
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                restored.append(name)
        src_tailored = repo / "tailored"
        if src_tailored.is_dir():
            config.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_tailored, config.TAILORED_DIR, dirs_exist_ok=True)
            restored.append("tailored/")

    config.reload()
    return restored
