"""Stage 7: knowledge-base generation (knowledge_base.jsonl)."""

from __future__ import annotations

from collections import Counter

from . import config, util


def generate(records: list[dict]) -> dict:
    """Write knowledge_base.jsonl and return generation statistics."""
    # Deterministic ordering: by knowledge family, then type, then id, so that
    # regenerated datasets are comparable.
    ordered = sorted(
        records,
        key=lambda r: (
            str(r.get("knowledge_family") or ""),
            str(r.get("knowledge_type") or ""),
            str(r.get("id") or ""),
        ),
    )
    n = util.write_jsonl(config.KB_PATH, ordered)

    counts = Counter(r["knowledge_type"] for r in ordered)
    authority_counts = Counter(r.get("authority") for r in ordered)
    issue_codes = sorted(
        {
            (r.get("facts") or {}).get("issue_code")
            for r in ordered
            if r["knowledge_type"] == "ValidationIssue" and (r.get("facts") or {}).get("issue_code")
        }
    )
    rule_ids = sorted(
        {
            (r.get("facts") or {}).get("rule_id")
            for r in ordered
            if (r.get("facts") or {}).get("rule_id")
        }
    )
    stats = {
        "path": config.KB_PATH,
        "records_written": n,
        "counts_by_knowledge_type": dict(sorted(counts.items())),
        "counts_by_authority": dict(sorted(authority_counts.items())),
        "unique_issue_codes": len(issue_codes),
        "issue_codes": issue_codes,
        "distinct_rules": len(rule_ids),
        "rule_ids": rule_ids,
        "bids_versions_present": sorted(
            {str(r["bids_version"]) for r in ordered if r.get("bids_version")}
        ),
        "schema_versions_present": sorted(
            {str(r["schema_version"]) for r in ordered if r.get("schema_version")}
        ),
    }
    util.write_json(
        __import__("os").path.join(config.ARTIFACTS_DIR, "kb_stats.json"), stats
    )
    return stats
