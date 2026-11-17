"""Single authoritative configuration for the BIDS knowledge-base pipeline.

All source URLs live here exactly once (SOURCE_URLS).  Every other module
imports them from this module; no other file may hard-code a source URL.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Authoritative source list (do not duplicate elsewhere)
# ---------------------------------------------------------------------------
SOURCE_URLS = [
    "https://bids-specification.readthedocs.io/en/stable/",
    "https://github.com/bids-standard/bids-specification",
    "https://github.com/bids-standard/bids-validator",
    "https://github.com/bids-standard/bids-examples",
    "https://github.com/bids-standard/bids-schema",
]

# ---------------------------------------------------------------------------
# Source registry: stable identifier + role per source
# ---------------------------------------------------------------------------
SOURCES: dict[str, dict] = {
    "bids-specification-docs": {
        "url": SOURCE_URLS[0],
        "role": "human-readable specification documentation",
        "kind": "docs",
        "authority": "published_documentation",
        "version_label": "stable (published documentation build)",
        "sitemap": SOURCE_URLS[0] + "sitemap.xml",
    },
    "bids-specification-repo": {
        "url": SOURCE_URLS[1],
        "role": "structured rules and specification source",
        "kind": "git",
        "authority": "normative_specification",
        "version_label": "development (default branch)",
        "local": "bids-specification",
    },
    "bids-validator": {
        "url": SOURCE_URLS[2],
        "role": "implementation and validation issue behavior",
        "kind": "git",
        "authority": "validator_implementation",
        "version_label": "development (default branch)",
        "local": "bids-validator",
    },
    "bids-examples": {
        "url": SOURCE_URLS[3],
        "role": "representative dataset examples",
        "kind": "git",
        "authority": "dataset_example",
        "version_label": "development (default branch)",
        "local": "bids-examples",
    },
    "bids-schema": {
        "url": SOURCE_URLS[4],
        "role": "historical and versioned schema definitions",
        "kind": "git",
        "authority": "schema_archive",
        "version_label": "development (default branch)",
        "local": "bids-schema",
    },
}

SOURCE_IDS = list(SOURCES.keys())

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_DIR = os.path.join(ROOT, "sources")
CACHE_DIR = os.path.join(ROOT, "cache")
DOCS_CACHE_DIR = os.path.join(CACHE_DIR, "docs")
ARTIFACTS_DIR = os.path.join(ROOT, "artifacts")
PIPELINE_DIR = os.path.join(ROOT, "pipeline")

KB_PATH = os.path.join(ROOT, "knowledge_base.jsonl")
FT_PATH = os.path.join(ROOT, "finetuning_dataset.jsonl")
MANIFEST_PATH = os.path.join(ARTIFACTS_DIR, "extraction_manifest.json")
INVENTORY_PATH = os.path.join(ARTIFACTS_DIR, "inventory.json")
STATE_PATH = os.path.join(ARTIFACTS_DIR, "pipeline_state.json")
VALIDATION_REPORT_PATH = os.path.join(ARTIFACTS_DIR, "validation_report.json")
FT_MANIFEST_PATH = os.path.join(ARTIFACTS_DIR, "ft_manifest.jsonl")

# ---------------------------------------------------------------------------
# Knowledge taxonomy
# ---------------------------------------------------------------------------
KNOWLEDGE_TYPES = [
    "ValidationIssue",
    "ValidationRule",
    "FilenameRule",
    "MetadataRule",
    "TabularRule",
    "Concept",
    "Version",
    "Troubleshooting",
    "Relationship",
    "Example",
    "Conflict",  # explicit conflict records (taxonomy extension)
]

AUTHORITIES = [
    "normative_specification",
    "published_documentation",
    "validator_implementation",
    "dataset_example",
    "schema_archive",
    "generated",
]

# ---------------------------------------------------------------------------
# Network behaviour
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 4
HTTP_BACKOFF = 1.5      # seconds, exponential
HTTP_RATE_LIMIT = 0.2   # seconds between requests
USER_AGENT = "bids-knowledge-base-pipeline/1.0 (research; +https://github.com/bids-standard/bids-specification)"

# ---------------------------------------------------------------------------
# Schema sub-locations inside the specification repository
# ---------------------------------------------------------------------------
SPEC_SCHEMA_DIR = os.path.join("src", "schema")
SPEC_MD_DIR = "src"

# Docs URL path -> specification repository markdown path
# (used to link published documentation sections with development sources)
DOCS_URL_PREFIX = SOURCE_URLS[0]
