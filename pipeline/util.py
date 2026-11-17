"""Shared utilities: deterministic IDs, JSONL IO, hashing, git revisions,
encoding-safe file reading, and a small manifest helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timezone

from . import config

# ---------------------------------------------------------------------------
# Deterministic identifiers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase ASCII slug; deterministic for identical input."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_RE.sub("_", text).strip("_")
    return text[:max_len].rstrip("_") or "x"


def stable_id(prefix: str, *parts: str, length: int = 10) -> str:
    """Deterministic, collision-resistant ID from ordered string parts.

    The same inputs always produce the same ID, so regenerated datasets are
    comparable line by line.
    """
    key = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{slugify(prefix, 8)}_{digest}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Encoding-safe file reading
# ---------------------------------------------------------------------------

def read_text(path: str) -> tuple[str, str, list[str]]:
    """Read a text file trying common encodings.

    Returns (text, encoding_used, problems).  Never raises for individual
    decode failures: undecodable bytes are replaced and reported.
    """
    problems: list[str] = []
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as fh:
                text = fh.read()
            if enc != "utf-8":
                problems.append(f"decoded with fallback encoding {enc}")
            return text, enc, problems
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            problems.append(f"read error: {exc}")
            return "", "utf-8", problems
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    problems.append("undecodable bytes replaced with U+FFFD")
    return text, "utf-8/replace", problems


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# JSON / JSONL helpers
# ---------------------------------------------------------------------------

def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path: str, records: list[dict]) -> int:
    """Write one JSON object per line (UTF-8, no array wrapper)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    n = 0
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=False))
            fh.write("\n")
            n += 1
    os.replace(tmp, path)
    return n


def read_jsonl(path: str) -> tuple[list[dict], list[str]]:
    """Return (records, errors).  Blank lines are skipped, not errors."""
    records: list[dict] = []
    errors: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: {exc}")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {lineno}: not a JSON object")
                continue
            records.append(obj)
    return records, errors


def append_jsonl(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# Git revisions
# ---------------------------------------------------------------------------

def git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def repo_revision(path: str) -> dict:
    """Record revision metadata for a cloned repository (or 'unknown')."""
    meta = {
        "commit": None,
        "branch": None,
        "tag": None,
        "commit_date": None,
        "retrieved_at": utc_now_iso(),
        "clone_url": None,
        "status": "not_cloned",
    }
    if not os.path.isdir(path):
        return meta
    if not os.path.isdir(os.path.join(path, ".git")):
        meta["status"] = "present_without_git_metadata"
        return meta
    meta["commit"] = git(["rev-parse", "HEAD"], path)
    meta["branch"] = git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    meta["tag"] = git(["describe", "--tags", "--always"], path)
    meta["commit_date"] = git(["log", "-1", "--format=%cI"], path)
    meta["clone_url"] = git(["config", "--get", "remote.origin.url"], path)
    meta["status"] = "cloned"
    return meta


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def truncate(text: str, limit: int = 400) -> str:
    text = normalize_ws(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def find_line(raw: str, pattern: str, flags: int = re.M) -> int | None:
    """1-based line number of the first regex match, or None."""
    m = re.search(pattern, raw, flags)
    if not m:
        return None
    return raw.count("\n", 0, m.start()) + 1


# ---------------------------------------------------------------------------
# Pipeline state (resumability)
# ---------------------------------------------------------------------------

class PipelineState:
    """Tracks completed stages so an interrupted run can be resumed."""

    def __init__(self, path: str = config.STATE_PATH):
        self.path = path
        self.data: dict = {"stages": {}, "updated": None}
        if os.path.exists(path):
            try:
                self.data = read_json(path)
            except (OSError, json.JSONDecodeError):
                pass
        self.data.setdefault("stages", {})

    def done(self, stage: str) -> bool:
        return self.data["stages"].get(stage, {}).get("status") == "done"

    def mark(self, stage: str, status: str, **extra) -> None:
        self.data["stages"][stage] = {
            "status": status,
            "at": utc_now_iso(),
            **extra,
        }
        self.data["updated"] = utc_now_iso()
        write_json(self.path, self.data)

    def reset(self) -> None:
        self.data = {"stages": {}, "updated": None}
        write_json(self.path, self.data)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def retry_sleep(attempt: int, base: float = config.HTTP_BACKOFF) -> None:
    time.sleep(base ** attempt)
