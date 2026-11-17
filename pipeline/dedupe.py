"""Stage 6: deduplication, relationship linking, and conflict detection."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import normalize, util

MAX_LINKS = 25

CONCEPT_TOKEN_KINDS = {
    "suffix": "suffixes",
    "datatype": "datatypes",
    "entity": "entities",
    "metadata_field": "metadata_fields",
    "tabular_column": "columns",
    "modality": "modalities",
    "dataset_file": "dataset_files",
}


def dedupe_and_link(records: list[dict]) -> tuple[list[dict], dict]:
    stats: dict = {
        "input_records": len(records),
        "exact_duplicate_records_merged": 0,
        "same_text_records_kept": 0,
        "conflicts_detected": 0,
        "cross_source_issue_links": 0,
        "concept_links": 0,
        "version_links": 0,
        "message_variant_codes": 0,
        "duplicate_keys": [],
    }

    # --- 1. exact duplicate record keys ---
    seen_keys: dict[str, dict] = {}
    unique: list[dict] = []
    for rec in records:
        key = rec["_record_key"]
        if key in seen_keys:
            first = seen_keys[key]
            first["raw_content"].setdefault("duplicate_sources", []).append(
                {"source": rec.get("source"), "summary": rec.get("summary")}
            )
            stats["exact_duplicate_records_merged"] += 1
            stats["duplicate_keys"].append(key)
            continue
        seen_keys[key] = rec
        unique.append(rec)

    # --- 2. identical retrieval text (informational; provenance differs) ---
    text_index: dict[str, str] = {}
    for rec in unique:
        t = util.normalize_ws(rec.get("retrieval_text", ""))[:2000]
        if not t:
            continue
        h = util.short_hash(t, 16)
        if h in text_index:
            rec["raw_content"]["same_text_as"] = text_index[h]
            stats["same_text_records_kept"] += 1
        else:
            text_index[h] = rec["id"]

    # --- 3. indexes ---
    issues_by_code: dict[str, list[dict]] = defaultdict(list)
    concepts_by_token: dict[tuple[str, str], str] = {}   # (kind, key) -> record id
    concepts_by_family: dict[str, str] = {}
    versions_by_code: dict[str, list[dict]] = defaultdict(list)

    for rec in unique:
        if rec["knowledge_type"] == "ValidationIssue":
            code = (rec.get("facts") or {}).get("issue_code")
            if code:
                issues_by_code[code].append(rec)
        if rec["knowledge_type"] == "Version" and (rec.get("facts") or {}).get("issue_code"):
            versions_by_code[rec["facts"]["issue_code"]].append(rec)
        if rec["knowledge_type"] == "Concept":
            facts = rec.get("facts") or {}
            kind = facts.get("concept_kind")
            token_kind = CONCEPT_TOKEN_KINDS.get(kind)
            if token_kind:
                key = facts.get("object_key") or facts.get("json_key")
                if key:
                    concepts_by_token.setdefault((token_kind, str(key)), rec["id"])
            if facts.get("modality"):
                concepts_by_token.setdefault(("modalities", str(facts["modality"])), rec["id"])
            concepts_by_family.setdefault(rec.get("knowledge_family", ""), rec["id"])

    # --- 4. linking ---
    def add_link(rec: dict, ltype: str, target_id: str, label: str) -> None:
        if len(rec["relationships"]) >= MAX_LINKS:
            return
        if any(e["id"] == target_id and e["type"] == ltype for e in rec["relationships"]):
            return
        rec["relationships"].append({"type": ltype, "id": target_id, "label": label})
        if ltype == "concept_reference":
            stats["concept_links"] += 1
        elif ltype == "same_code_cross_source":
            stats["cross_source_issue_links"] += 1
        elif ltype == "version_specific_definition":
            stats["version_links"] += 1

    token_field_map = [
        ("related_suffixes", "suffixes"),
        ("related_datatypes", "datatypes"),
        ("related_fields", "metadata_fields"),
    ]
    for rec in unique:
        if rec["knowledge_type"] not in ("ValidationIssue", "ValidationRule", "MetadataRule", "FilenameRule", "TabularRule"):
            continue
        facts = rec.get("facts") or {}
        for fkey, token_kind in token_field_map:
            for token in facts.get(fkey) or []:
                cid = concepts_by_token.get((token_kind, str(token)))
                if cid and cid != rec["id"]:
                    add_link(rec, "concept_reference", cid, f"{token_kind}:{token}")

    # cross-source issue links + message variants
    message_variants = 0
    for code, recs in issues_by_code.items():
        authorities = {r["authority"] for r in recs}
        if len(authorities) > 1:
            for r in recs:
                for other in recs:
                    if other is not r and other["authority"] != r["authority"]:
                        add_link(r, "same_code_cross_source", other["id"], f"issue:{code}")
        messages = {
            (r.get("facts") or {}).get("message")
            for r in recs
            if (r.get("facts") or {}).get("message")
        }
        if len(messages) > 1:
            message_variants += 1
    stats["message_variant_codes"] = message_variants

    # version links
    for code, vrecs in versions_by_code.items():
        for irec in issues_by_code.get(code, []):
            for vrec in vrecs:
                add_link(irec, "version_specific_definition", vrec["id"], f"issue:{code}")
                add_link(vrec, "applies_to_issue", irec["id"], f"issue:{code}")

    # --- 5. conflicts: same code, materially different severity ---
    conflict_raw: list[dict] = []
    for code, recs in sorted(issues_by_code.items()):
        sev_map: dict[str, set] = defaultdict(set)
        for r in recs:
            sev = (r.get("facts") or {}).get("severity")
            if sev:
                sev_map[sev].add(r["authority"])
        if len(sev_map) <= 1:
            continue
        stats["conflicts_detected"] += 1
        sides = [
            {
                "id": r["id"],
                "authority": r["authority"],
                "severity": (r.get("facts") or {}).get("severity"),
                "message": (r.get("facts") or {}).get("message"),
                "source": r.get("source"),
                "record_key": r.get("_record_key"),
            }
            for r in recs
            if (r.get("facts") or {}).get("severity")
        ]
        spec_levels = {s["severity"] for s in sides if s["authority"] == "normative_specification"}
        impl_levels = {s["severity"] for s in sides if s["authority"] == "validator_implementation"}
        analysis = (
            "Normative specification defines: "
            + ", ".join(sorted(spec_levels))
            + "; validator implementation declares: "
            + ", ".join(sorted(impl_levels))
            + ". "
        )
        if len(spec_levels) > 1:
            analysis += "Multiple specification rules define this code with different levels; the applicable level depends on which rule the failing context matches."
        elif spec_levels and impl_levels and not (spec_levels & impl_levels):
            analysis += (
                "The divergence cannot be resolved from the configured sources alone: the specification schema "
                "level and the validator catalogue level disagree, and which one applies depends on the "
                "validator/schema version and the code path that raised the issue. Both facts are preserved."
            )
        else:
            analysis += "The divergence is recorded as unresolved because source evidence is insufficient to rank the sources automatically."
        conflict_raw.append(
            {
                "record_key": f"conflict|{code}|" + "|".join(sorted(f"{s['authority']}={s['severity']}" for s in sides)),
                "knowledge_type": "Conflict",
                "knowledge_family": f"issue:{code}",
                "authority": "generated",
                "title": f"Severity conflict for issue {code}",
                "summary": f"Sources disagree on the severity of {code}: "
                + "; ".join(f"{s['authority']} says {s['severity']}" for s in sides)
                + ". Status: unresolved.",
                "scope": {
                    "knowledge_domain": "source_conflict",
                    "issue_code": code,
                    "resolution": "unresolved",
                },
                "bids_version": None,
                "schema_version": None,
                "source": {
                    "repository": None,
                    "file": None,
                    "path": None,
                    "url": None,
                    "section": None,
                    "line_start": None,
                    "line_end": None,
                    "revision": None,
                    "source_id": "generated",
                },
                "raw_content": {
                    "issue_code": code,
                    "conflicting_facts": sides,
                    "analysis": analysis,
                    "resolution": "unresolved",
                },
                "facts": {
                    "issue_code": code,
                    "severity": None,
                    "conflict": True,
                    "resolution": "unresolved",
                    "severities_by_authority": {
                        s["authority"]: sorted({x["severity"] for x in sides if x["authority"] == s["authority"]})
                        for s in sides
                    },
                    "message": None,
                },
                "hints": {"kind": "conflict"},
            }
        )

    if conflict_raw:
        normalized_conflicts = normalize.normalize(conflict_raw)
        for crec in normalized_conflicts:
            code = crec["facts"].get("issue_code")
            for irec in issues_by_code.get(code, []):
                add_link(irec, "source_conflict", crec["id"], f"issue:{code}")
            crec["relationships"] = [
                {"type": "applies_to_issue", "id": r["id"], "label": f"issue:{code}"}
                for r in issues_by_code.get(code, [])
            ]
        unique.extend(normalized_conflicts)

    # strip internal fields
    for rec in unique:
        rec.pop("_record_key", None)
        rec.pop("_hints", None)

    stats["output_records"] = len(unique)
    stats["unique_issue_codes"] = len(issues_by_code)
    return unique, stats
