#!/usr/bin/env python3
"""Merge the curated explanations into enriched_knowledge.jsonl.

There are two ways enrichment reaches the knowledge base, and this script is the
offline one. ``knowledge_base_enricher.py`` calls a language model and a web
search, needs API keys and network access, and costs money per record. This
script needs neither: it applies the explanations written by hand in
``curated_enrichment.py``, keyed by validator issue code.

The two are designed to coexist. Enrichment produced by either route lands in the
same file under the same ``ai_enrichment`` key, and each record records which
route produced it in ``ai_enrichment_metadata.source``. Existing entries are kept
unless ``--overwrite`` is given, so running this script cannot destroy work done
by the other one.

Usage
-----
    python build_enrichment.py              # add curated entries, keep existing
    python build_enrichment.py --overwrite  # replace existing entries too
    python build_enrichment.py --report     # show coverage, change nothing
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from curated_enrichment import ENRICHMENT

BASE_DIR = Path(__file__).parent
KNOWLEDGE_FILE = BASE_DIR / "knowledge.jsonl"
ENRICHED_FILE = BASE_DIR / "enriched_knowledge.jsonl"

CURATED_SOURCE = "curated"


def load_jsonl(path: Path) -> List[dict]:
    """Read a JSONL file, skipping lines that do not parse."""
    records: List[dict] = []
    if not path.exists():
        return records

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] {path.name}:{line_number} is not valid JSON: {exc}")
                continue
            if isinstance(data, dict):
                records.append(data)

    return records


def write_jsonl(path: Path, records: List[dict]) -> None:
    """Write records atomically, so an interrupted run cannot truncate the file."""
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temp.replace(path)


def issue_code_of(record: dict) -> str:
    """The validator issue code a knowledge record is about, if any."""
    scope = record.get("scope") or {}
    code = scope.get("issue_code")
    return code if isinstance(code, str) else ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply curated explanations to the knowledge base.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace enrichment that already exists for a record.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Report coverage without writing anything.",
    )
    args = parser.parse_args()

    knowledge = load_jsonl(KNOWLEDGE_FILE)
    if not knowledge:
        raise SystemExit(
            f"No knowledge records found at {KNOWLEDGE_FILE}. "
            f"Run extract_kb.py first."
        )

    existing = {r["id"]: r for r in load_jsonl(ENRICHED_FILE) if r.get("id")}

    # ---- coverage ----------------------------------------------------
    codes_in_kb = {issue_code_of(r) for r in knowledge if issue_code_of(r)}
    curated_codes = set(ENRICHMENT)

    print(f"Knowledge records:        {len(knowledge)}")
    print(f"Validator issue codes:    {len(codes_in_kb)}")
    print(f"Curated explanations:     {len(curated_codes)}")

    uncovered = sorted(codes_in_kb - curated_codes)
    if uncovered:
        print(f"Codes with no curated explanation ({len(uncovered)}):")
        for code in uncovered:
            print(f"  {code}")

    unused = sorted(curated_codes - codes_in_kb)
    if unused:
        print(
            f"Curated explanations for codes not in this schema version "
            f"({len(unused)}): {', '.join(unused)}"
        )

    if args.report:
        enriched_now = sum(1 for r in existing.values() if r.get("ai_enrichment"))
        print(f"Records already carrying enrichment: {enriched_now}")
        by_source = Counter(
            (r.get("ai_enrichment_metadata") or {}).get("source", "model")
            for r in existing.values()
            if r.get("ai_enrichment")
        )
        for source, count in by_source.most_common():
            print(f"  {source}: {count}")
        return

    # ---- merge -------------------------------------------------------
    stamp = datetime.now(timezone.utc).isoformat()
    output: List[dict] = []
    added, replaced, kept, untouched = 0, 0, 0, 0

    for record in knowledge:
        record_id = record.get("id", "")
        code = issue_code_of(record)
        curated = ENRICHMENT.get(code) if code else None

        previous = existing.get(record_id)
        previous_enrichment = (previous or {}).get("ai_enrichment")
        previous_meta = (previous or {}).get("ai_enrichment_metadata") or {}

        if curated is None:
            # Nothing curated for this record. Carry forward whatever the
            # model-driven pass produced, so the two routes accumulate rather
            # than overwrite each other.
            if previous_enrichment:
                merged = dict(record)
                merged["ai_enrichment"] = previous_enrichment
                merged["ai_enrichment_metadata"] = previous_meta
                output.append(merged)
                untouched += 1
            continue

        if previous_enrichment and not args.overwrite:
            # Keep an existing explanation. Two explanations of one rule that
            # disagree are worse than one, and the one already in the file may
            # have been reviewed.
            merged = dict(record)
            merged["ai_enrichment"] = previous_enrichment
            merged["ai_enrichment_metadata"] = previous_meta
            output.append(merged)
            kept += 1
            continue

        merged = dict(record)
        merged["ai_enrichment"] = dict(curated)
        merged["ai_enrichment_metadata"] = {
            "source": CURATED_SOURCE,
            "issue_code": code,
            "enriched_at": stamp,
        }
        output.append(merged)

        if previous_enrichment:
            replaced += 1
        else:
            added += 1

    write_jsonl(ENRICHED_FILE, output)

    print()
    print(f"Curated explanations added:    {added}")
    print(f"Curated explanations replaced: {replaced}")
    print(f"Existing enrichment kept:      {kept}")
    print(f"Model enrichment carried over: {untouched}")
    print(f"Total enriched records:        {len(output)}")
    print(f"Written to:                    {ENRICHED_FILE}")


if __name__ == "__main__":
    main()
