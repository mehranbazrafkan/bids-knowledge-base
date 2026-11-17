"""Stage 1: source discovery and inventory.

Walks every configured source, records what exists (files, pages, sizes,
revisions), and writes artifacts/inventory.json.  Discovery is read-only:
nothing is parsed into knowledge records yet.
"""

from __future__ import annotations

import os

from . import acquire, config, util

# Directories that never contain extractable knowledge.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    "images", "fonts", "_static", ".pytest_cache", ".mypy_cache", "site",
}

# File classes we track explicitly (extension -> class).
EXT_CLASSES = {
    ".yaml": "structured_yaml",
    ".yml": "structured_yaml",
    ".json": "structured_json",
    ".md": "markdown",
    ".ts": "typescript_source",
    ".tsx": "typescript_source",
    ".js": "javascript_source",
    ".py": "python_source",
    ".tsv": "tabular",
    ".csv": "tabular",
    ".txt": "text",
    ".cff": "citation",
    ".toml": "config",
    ".cfg": "config",
    ".ini": "config",
}


def classify(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if os.path.basename(path) in ("LICENSE", "README", "CHANGES", "Makefile", "CODEOWNERS"):
        return "root_text"
    return EXT_CLASSES.get(ext, "other")


def walk(root: str) -> list[dict]:
    """Inventory of files under root (relative paths, sizes, classes)."""
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            out.append({"path": rel, "bytes": size, "class": classify(rel)})
    return out


def inventory_git_source(source_id: str) -> dict:
    src = config.SOURCES[source_id]
    local = os.path.join(config.SOURCES_DIR, src["local"])
    entry = {
        "source_id": source_id,
        "url": src["url"],
        "role": src["role"],
        "kind": src["kind"],
        "authority": src["authority"],
        "local_path": local,
        "revision": util.repo_revision(local) if os.path.isdir(local) else {"status": "missing"},
    }
    if not os.path.isdir(local):
        entry["status"] = "not_available"
        entry["files"] = []
        entry["counts"] = {}
        return entry
    files = walk(local)
    counts: dict[str, int] = {}
    for f in files:
        counts[f["class"]] = counts.get(f["class"], 0) + 1
    entry["files"] = files
    entry["counts"] = counts
    entry["file_total"] = len(files)
    entry["status"] = "inventoried"
    return entry


def inventory_docs() -> dict:
    """Discover documentation pages from the sitemap (fetched in stage 2 if needed)."""
    src = config.SOURCES["bids-specification-docs"]
    urls, meta = acquire.fetch_sitemap()
    entry = {
        "source_id": "bids-specification-docs",
        "url": src["url"],
        "role": src["role"],
        "kind": src["kind"],
        "authority": src["authority"],
        "sitemap": {"url": src["sitemap"], **meta},
        "pages": [{"url": u} for u in urls],
        "page_count": len(urls),
        "status": "inventoried" if urls else "sitemap_unavailable",
    }
    return entry


def run() -> dict:
    """Stage 1: discovery and inventory."""
    inventory = {
        "generated_at": util.utc_now_iso(),
        "source_urls": list(config.SOURCE_URLS),
        "sources": {},
    }
    # Docs first (network), then git sources.
    docs = inventory_docs()
    inventory["sources"]["bids-specification-docs"] = docs

    for sid in ("bids-specification-repo", "bids-validator", "bids-examples", "bids-schema"):
        inventory["sources"][sid] = inventory_git_source(sid)

    # Quick focus-area annotations for downstream stages.
    spec = inventory["sources"]["bids-specification-repo"]
    schema_files = [f["path"] for f in spec.get("files", []) if f["path"].startswith("src/schema/")]
    spec["focus"] = {"schema_files": schema_files}
    val = inventory["sources"]["bids-validator"]
    val["focus"] = {
        "issue_files": [f["path"] for f in val.get("files", []) if f["path"].startswith("src/issues/")],
        "doc_files": [f["path"] for f in val.get("files", []) if f["path"].startswith("docs/") and f["class"] == "markdown"],
        "test_files": [f["path"] for f in val.get("files", []) if f["path"].endswith(".test.ts")],
    }
    ex = inventory["sources"]["bids-examples"]
    ex["focus"] = {
        "dataset_dirs": sorted(
            {
                f["path"].split("/")[0]
                for f in ex.get("files", [])
                if "/" in f["path"] and not f["path"].startswith((".github/", "docs/"))
            }
        ),
    }
    sc = inventory["sources"]["bids-schema"]
    sc["focus"] = {
        "version_dirs": sorted(
            {
                f["path"].split("/")[1]
                for f in sc.get("files", [])
                if f["path"].startswith("versions/") and f["path"].count("/") >= 2
            }
        ),
        "snapshot_trees": sorted(
            {f["path"].split("/")[0] for f in sc.get("files", []) if f["path"].split("/")[0] in ("BEPs", "PRs")}
        ),
    }

    util.write_json(config.INVENTORY_PATH, inventory)
    return inventory
