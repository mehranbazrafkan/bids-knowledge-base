"""Checks for the generated knowledge base (knowledge_base.jsonl)."""

from __future__ import annotations

import json
import os
import re

import pytest

from pipeline import config, normalize, parse, util
from pipeline import audit as audit_mod

ID_RE = re.compile(r"^kb_[a-z0-9]+_[0-9a-f]{6,16}$")


@pytest.fixture(scope="module")
def kb_records() -> list[dict]:
    assert os.path.exists(config.KB_PATH), (
        "knowledge_base.jsonl is missing - run `python -m pipeline.run --force`"
    )
    records, errors = util.read_jsonl(config.KB_PATH)
    assert not errors, errors[:5]
    assert records
    return records


def test_file_exists_and_is_jsonl(kb_records):
    with open(config.KB_PATH, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            assert isinstance(obj, dict), f"line {lineno} is not a JSON object"


def test_template_fields_present(kb_records):
    for rec in kb_records:
        for field in normalize.REQUIRED_FIELDS:
            assert field in rec, f"{rec.get('id')} missing template field {field}"


def test_ids_unique_and_well_formed(kb_records):
    ids = [r["id"] for r in kb_records]
    assert len(set(ids)) == len(ids)
    for rid in ids:
        assert ID_RE.match(rid), f"unexpected id format: {rid}"


def test_knowledge_types_and_authorities(kb_records):
    types = {r["knowledge_type"] for r in kb_records}
    assert types <= set(config.KNOWLEDGE_TYPES), types - set(config.KNOWLEDGE_TYPES)
    authorities = {r["authority"] for r in kb_records}
    assert authorities <= set(config.AUTHORITIES), authorities - set(config.AUTHORITIES)


def test_every_configured_source_is_represented(kb_records):
    covered = {(r.get("source") or {}).get("source_id") for r in kb_records}
    assert set(config.SOURCE_IDS) <= covered, set(config.SOURCE_IDS) - covered


def test_issue_records_keep_exact_codes(kb_records):
    issues = [r for r in kb_records if r["knowledge_type"] == "ValidationIssue"]
    assert issues
    for rec in issues:
        code = (rec.get("facts") or {}).get("issue_code")
        assert code == rec["title"], rec["id"]
        assert code in rec["retrieval_text"], rec["id"]
        assert code in json.dumps(rec["raw_content"]), rec["id"]


def test_enrichment_never_quotes_invented_text(kb_records):
    def record_text(rec: dict) -> str:
        out = []

        def walk(v):
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)

        walk(rec)
        return re.sub(r"\s+", " ", " ".join(out)).strip()

    for rec in kb_records:
        guidance = str((rec.get("ai_enrichment") or {}).get("resolution_guidance") or "")
        text = record_text(rec)
        for quote in re.findall(r'"([^"]{25,400})"', guidance):
            assert re.sub(r"\s+", " ", quote).strip() in text, (
                f"{rec['id']} quotes text absent from the record: {quote[:80]}"
            )


def test_covers_every_discovered_issue_code(kb_records):
    truth, _ = audit_mod._source_issue_truth()
    in_kb = {
        (r.get("facts") or {}).get("issue_code") or r["title"]
        for r in kb_records
        if r["knowledge_type"] == "ValidationIssue"
    }
    missing = sorted(set(truth) - in_kb)
    assert not missing, missing


def test_source_issue_messages_match_verbatim(kb_records):
    truth, _ = audit_mod._source_issue_truth()
    by_code = {}
    for rec in kb_records:
        if rec["knowledge_type"] != "ValidationIssue":
            continue
        code = (rec.get("facts") or {}).get("issue_code") or rec["title"]
        by_code.setdefault(code, []).append(
            re.sub(r"\s+", " ", str((rec.get("facts") or {}).get("message") or "")).strip()
        )
    for code, t in truth.items():
        if code not in by_code:
            continue
        for msg in t["messages"]:
            if msg:
                assert msg in by_code[code], f"{code}: source message lost: {msg[:80]}"


def test_version_scoped_records_are_not_mixed(kb_records):
    dev_sources = {"bids-specification-repo", "bids-schema", "bids-examples"}
    for rec in kb_records:
        bv = str(rec.get("bids_version") or "")
        sv = str(rec.get("schema_version") or "")
        src = (rec.get("source") or {}).get("source_id")
        if "dev" in bv or "dev" in sv:
            assert src in dev_sources, f"{rec['id']} claims dev version from {src}"
        if rec.get("authority") == "published_documentation":
            assert "dev" not in bv, rec["id"]


def test_full_audit_passes():
    report = audit_mod.run_all()
    assert report["ok"], {
        "failures": report["failures"],
        "warnings": report["warnings"],
    }
