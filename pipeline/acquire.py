"""Stage 2 (plus helpers used by stage 1): content acquisition.

- Verifies/clones the four git sources and records their revisions.
- Fetches the documentation sitemap and every documentation page with a
  disk cache, retries, backoff and rate limiting, so runs are resumable.
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET

import httpx

from . import config, util

_cache_lock_file = None  # reserved for future concurrency
_last_request = [0.0]


def _cache_paths(url: str) -> tuple[str, str]:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(config.DOCS_CACHE_DIR, h + ".html"), os.path.join(
        config.DOCS_CACHE_DIR, h + ".meta.json"
    )


def fetch(url: str, refresh: bool = False, accept: str = "text/html") -> tuple[bytes | None, dict]:
    """Fetch a URL through the cache.

    Returns (payload, meta).  payload is None when both cache and network
    failed; meta always records provenance ('cache' or 'network') or the
    failure reason.
    """
    html_path, meta_path = _cache_paths(url)
    if not refresh and os.path.exists(html_path) and os.path.exists(meta_path):
        try:
            meta = util.read_json(meta_path)
            with open(html_path, "rb") as fh:
                return fh.read(), {**meta, "provenance": "cache"}
        except (OSError, ValueError):
            pass  # corrupt cache entry -> refetch

    util.ensure_dir(config.DOCS_CACHE_DIR)
    headers = {"User-Agent": config.USER_AGENT, "Accept": accept}
    last_err = None
    for attempt in range(config.HTTP_RETRIES):
        # simple client-side rate limit
        wait = config.HTTP_RATE_LIMIT - (util.time.time() - _last_request[0])
        if wait > 0:
            util.time.sleep(wait)
        _last_request[0] = util.time.time()
        try:
            with httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True, headers=headers) as client:
                resp = client.get(url)
            _last_request[0] = util.time.time()
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                util.retry_sleep(attempt)
                continue
            if resp.status_code >= 400:
                meta = {
                    "url": url,
                    "status": resp.status_code,
                    "retrieved_at": util.utc_now_iso(),
                    "error": f"HTTP {resp.status_code}",
                }
                return None, meta
            payload = resp.content
            meta = {
                "url": url,
                "status": resp.status_code,
                "retrieved_at": util.utc_now_iso(),
                "content_type": resp.headers.get("content-type"),
                "etag": resp.headers.get("etag"),
                "last_modified": resp.headers.get("last-modified"),
                "bytes": len(payload),
            }
            tmp = html_path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, html_path)
            util.write_json(meta_path, meta)
            return payload, {**meta, "provenance": "network"}
        except (httpx.HTTPError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            util.retry_sleep(attempt)
    return None, {"url": url, "retrieved_at": util.utc_now_iso(), "error": last_err or "unknown"}


# ---------------------------------------------------------------------------
# Documentation (source A)
# ---------------------------------------------------------------------------

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fetch_sitemap(refresh: bool = False) -> tuple[list[str], dict]:
    url = config.SOURCES["bids-specification-docs"]["sitemap"]
    payload, meta = fetch(url, refresh=refresh, accept="application/xml")
    if payload is None:
        return [], meta
    try:
        root = ET.fromstring(payload.decode("utf-8", "replace"))
        urls = [
            loc.text.strip()
            for loc in root.iter(f"{_SITEMAP_NS}loc")
            if loc.text and loc.text.strip()
        ]
        if not urls:  # some sitemaps omit the namespace
            urls = [loc.text.strip() for loc in root.iter("loc") if loc.text]
    except ET.ParseError as exc:
        return [], {**meta, "error": f"sitemap parse error: {exc}"}
    return urls, meta


def acquire_docs(refresh: bool = False) -> dict:
    """Fetch the sitemap and every documentation page it declares."""
    report = {"source": "bids-specification-docs", "pages": [], "failures": []}
    urls, sitemap_meta = fetch_sitemap(refresh=refresh)
    report["sitemap"] = {"url": config.SOURCES["bids-specification-docs"]["sitemap"], **sitemap_meta}
    if not urls:
        report["failures"].append({"url": report["sitemap"]["url"], "error": sitemap_meta.get("error", "no urls")})
        return report
    for url in urls:
        payload, meta = fetch(url, refresh=refresh)
        entry = {"url": url, **meta}
        if payload is None:
            entry["error"] = meta.get("error", "fetch failed")
            report["failures"].append(entry)
        report["pages"].append(entry)
    return report


# ---------------------------------------------------------------------------
# Git sources (sources B-E)
# ---------------------------------------------------------------------------

def ensure_repo(source_id: str, refresh: bool = False) -> dict:
    """Verify a cloned repository; shallow-clone it when missing.

    refresh=True pulls the latest default-branch commit (depth 1).
    """
    src = config.SOURCES[source_id]
    local = os.path.join(config.SOURCES_DIR, src["local"])
    meta = {"source_id": source_id, "url": src["url"], "path": local}
    if not os.path.isdir(os.path.join(local, ".git")):
        if os.path.isdir(local) and not refresh:
            meta.update(util.repo_revision(local))
            return meta
        util.ensure_dir(config.SOURCES_DIR)
        # clone to a temp dir first, so an interrupted clone never leaves a
        # half-populated sources/<name> directory behind
        tmp = local + ".cloning"
        if os.path.isdir(tmp):
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        res = util.git(["clone", "--depth", "1", src["url"], tmp], cwd=config.SOURCES_DIR)
        if res is None and not os.path.isdir(os.path.join(tmp, ".git")):
            meta.update({"status": "clone_failed", "url": src["url"]})
            return meta
        os.replace(tmp, local)
    elif refresh:
        util.git(["fetch", "--depth", "1", "origin"], local)
        branch = util.git(["rev-parse", "--abbrev-ref", "HEAD"], local) or "HEAD"
        util.git(["merge", "--ff-only", f"origin/{branch}"], local)
    meta.update(util.repo_revision(local))
    return meta


def acquire_sources(refresh: bool = False) -> dict:
    report = {"repos": [], "failures": []}
    for sid in ("bids-specification-repo", "bids-validator", "bids-examples", "bids-schema"):
        meta = ensure_repo(sid, refresh=refresh)
        report["repos"].append(meta)
        if meta.get("status") not in ("cloned", "present_without_git_metadata"):
            report["failures"].append(meta)
    return report


def run(refresh: bool = False) -> dict:
    """Stage 2: content acquisition."""
    out = {"docs": acquire_docs(refresh=refresh), "sources": acquire_sources(refresh=refresh)}
    util.write_json(os.path.join(config.ARTIFACTS_DIR, "acquisition_report.json"), out)
    return out
