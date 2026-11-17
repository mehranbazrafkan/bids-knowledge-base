"""Stage 5: normalization.

Converts raw extracted records into the final knowledge-base record schema:

* deterministic IDs,
* consistent scope/source/raw_content/facts layout,
* a detailed self-contained ``retrieval_text``,
* deterministic, template-based ``ai_enrichment`` built ONLY from facts
  already present in the record (so enrichment cannot contradict sources),
  with unavailable guidance stated explicitly rather than invented.
"""

from __future__ import annotations

import re
from typing import Any

from . import config, util

MAX_RETRIEVAL = 6000
MAX_ENRICH = 4000

AUTHORITY_LABELS = {
    "normative_specification": "normative BIDS specification (development branch source)",
    "published_documentation": "published BIDS specification documentation (readthedocs 'stable')",
    "validator_implementation": "bids-validator implementation behavior (not itself a normative requirement)",
    "dataset_example": "bids-examples dataset repository (illustrative, not normative)",
    "schema_archive": "bids-schema archived/versioned schema snapshot",
    "generated": "pipeline-generated synthesis of the cited source facts",
}

REQUIRED_FIELDS = [
    "id", "knowledge_type", "title", "summary", "retrieval_text", "scope",
    "bids_version", "schema_version", "source", "raw_content", "ai_enrichment",
]

ENRICH_FIELDS = [
    "description", "interpretation", "why_it_matters", "example_scenarios",
    "common_causes", "resolution_guidance", "additional_notes", "confidence",
    "sources",
]

_SOURCE_FIELDS = [
    "repository", "file", "path", "url", "section", "line_start", "line_end",
    "revision",
]


# ---------------------------------------------------------------------------
# Retrieval text builders
# ---------------------------------------------------------------------------

def _source_ref(rec: dict) -> str:
    s = rec.get("source") or {}
    bits = []
    if s.get("url"):
        bits.append(s["url"])
    if s.get("path"):
        bits.append(str(s["path"]))
    if s.get("file") and s.get("file") != s.get("path"):
        bits.append(str(s["file"]))
    if s.get("line_start"):
        bits.append(f"line {s['line_start']}" + (f"-{s['line_end']}" if s.get("line_end") else ""))
    if s.get("section"):
        bits.append(f"section #{s['section']}")
    if s.get("revision"):
        bits.append(f"revision {str(s['revision'])[:12]}")
    return "; ".join(bits)


def _versions_line(rec: dict) -> str:
    bits = []
    if rec.get("bids_version"):
        bits.append(f"BIDS version {rec['bids_version']}")
    if rec.get("schema_version"):
        bits.append(f"schema version {rec['schema_version']}")
    if not bits:
        return "version: not attached to a specific BIDS/schema release (implementation source)"
    return "version: " + ", ".join(bits)


def build_retrieval_text(rec: dict) -> str:
    ktype = rec["knowledge_type"]
    facts = rec.get("facts") or {}
    parts: list[str] = [f"{rec['title']}"]

    if ktype == "ValidationIssue":
        code = facts.get("issue_code") or rec["title"]
        parts.append(f"Issue code: {code}")
        sev = facts.get("severity")
        parts.append(f"Severity/level: {sev if sev else 'not declared by this source (severity unknown)'}")
        if facts.get("legacy_issue_number") is not None:
            parts.append(f"Legacy validator issue number: {facts['legacy_issue_number']}")
        if facts.get("message"):
            parts.append(f"Exact message: {facts['message']}")
        if facts.get("implementation_reason") and facts.get("implementation_reason") != facts.get("message"):
            parts.append(f"Validator reason: {facts['implementation_reason']}")
        if facts.get("selectors"):
            parts.append("Applies when all selectors hold: " + " | ".join(facts["selectors"]))
        if facts.get("checks"):
            parts.append("Assertions checked (failure raises the issue): " + " | ".join(facts["checks"]))
        if facts.get("rule_id"):
            parts.append(f"Schema rule: {facts['rule_id']}")
        if facts.get("related_suffixes"):
            parts.append("Related suffixes: " + ", ".join(facts["related_suffixes"]))
        if facts.get("related_datatypes"):
            parts.append("Related datatypes: " + ", ".join(facts["related_datatypes"]))
        if facts.get("related_fields"):
            parts.append("Related metadata fields: " + ", ".join(facts["related_fields"]))
        if facts.get("raise_site_files"):
            parts.append("Raised by implementation files: " + ", ".join(facts["raise_site_files"][:8]))
        if facts.get("defined_in_spec_schema") is False:
            parts.append("Defined only in the validator implementation (no dedicated specification schema check).")
        if facts.get("defined_in_spec_schema") is True:
            parts.append("Also defined in the specification schema.")
        if facts.get("severity_unknown"):
            parts.append("Note: severity is not declared in the validator issue catalogue; it is supplied at the raise site or by schema rules.")
        parts.append(_versions_line(rec))

    elif ktype == "FilenameRule":
        parts.append(f"Rule ID: {facts.get('rule_id')}")
        if facts.get("requirement_level"):
            parts.append(f"Requirement level: {facts['requirement_level']}")
        if facts.get("path"):
            parts.append(f"Path: {facts['path']}")
        if facts.get("stem"):
            parts.append(f"File stem: {facts['stem']}")
        if facts.get("suffixes"):
            parts.append("Allowed suffixes: " + ", ".join(facts["suffixes"]))
        if facts.get("extensions"):
            parts.append("Allowed extensions: " + ", ".join(facts["extensions"]))
        if facts.get("datatypes"):
            parts.append("Allowed datatype directories: " + ", ".join(facts["datatypes"]))
        ents = facts.get("entities")
        if isinstance(ents, dict) and ents:
            parts.append(
                "Entities (requirement levels): "
                + ", ".join(f"{k}={v}" for k, v in list(ents.items())[:30])
            )
        if facts.get("selectors"):
            parts.append("Selectors: " + " | ".join(facts["selectors"]))
        parts.append(_versions_line(rec))

    elif ktype == "MetadataRule":
        parts.append(f"Rule ID: {facts.get('rule_id')}")
        if facts.get("selectors"):
            parts.append("Applies when all selectors hold: " + " | ".join(facts["selectors"]))
        if facts.get("required_fields"):
            parts.append("Required fields: " + ", ".join(facts["required_fields"]))
        if facts.get("recommended_fields"):
            parts.append("Recommended fields: " + ", ".join(facts["recommended_fields"]))
        levels = facts.get("field_levels") or {}
        optional = sorted(k for k, v in levels.items() if v == "optional")
        if optional:
            parts.append("Optional fields: " + ", ".join(optional[:80]) + (" …" if len(optional) > 80 else ""))
        addenda = {
            k: v.get("level_addendum") for k, v in (facts.get("fields") or {}).items()
            if isinstance(v, dict) and v.get("level_addendum")
        }
        if addenda:
            parts.append(
                "Conditional/annotated levels: "
                + "; ".join(f"{k}: {v}" for k, v in list(addenda.items())[:20])
            )
        if facts.get("related_paths"):
            parts.append("Target file(s): " + ", ".join(facts["related_paths"]))
        if facts.get("related_suffixes"):
            parts.append("Related suffixes: " + ", ".join(facts["related_suffixes"]))
        parts.append(_versions_line(rec))

    elif ktype == "TabularRule":
        parts.append(f"Rule ID: {facts.get('rule_id')}")
        if facts.get("selectors"):
            parts.append("Applies when all selectors hold: " + " | ".join(facts["selectors"]))
        if facts.get("required_columns"):
            parts.append("Required columns: " + ", ".join(facts["required_columns"]))
        if facts.get("recommended_columns"):
            parts.append("Recommended columns: " + ", ".join(facts["recommended_columns"]))
        levels = facts.get("column_levels") or {}
        optional = sorted(k for k, v in levels.items() if v == "optional")
        if optional:
            parts.append("Optional columns: " + ", ".join(optional))
        if facts.get("additional_columns") is not None:
            parts.append(f"Additional columns policy: {facts['additional_columns']}")
        if facts.get("initial_columns"):
            parts.append("Initial column order: " + ", ".join(facts["initial_columns"]))
        if facts.get("index_columns"):
            parts.append("Index columns (values must be unique): " + ", ".join(facts["index_columns"]))
        parts.append(_versions_line(rec))

    elif ktype == "Relationship":
        parts.append(f"Relationship type: {facts.get('relationship_type')}")
        if facts.get("selectors"):
            parts.append("Applies when: " + " | ".join(facts["selectors"]))
        if facts.get("target") is not None:
            parts.append(f"Target file pattern: {facts['target']}")
        if facts.get("inheritable") is not None:
            parts.append(f"May be inherited from a higher directory level: {facts['inheritable']}")
        parts.append(_versions_line(rec))

    elif ktype == "Version":
        for k in ("release_version", "release_date", "bids_version", "schema_version",
                  "snapshot", "validator_release", "latest_released", "content_stage"):
            if facts.get(k):
                parts.append(f"{k}: {facts[k]}")
        if facts.get("version_specific"):
            parts.append("This record documents version-specific behavior; see raw_content.variants.")
        parts.append(_source_ref(rec))

    elif ktype == "Example":
        if facts.get("dataset_name"):
            parts.append(f"Example dataset: {facts['dataset_name']}")
            if facts.get("datatypes"):
                parts.append("Datatypes: " + ", ".join(f"{k} ({v} files)" for k, v in list(facts["datatypes"].items())[:12]))
            if facts.get("suffixes"):
                parts.append("Suffixes: " + ", ".join(list(facts["suffixes"])[:30]))
            if facts.get("participants_columns"):
                parts.append("participants.tsv columns: " + ", ".join(facts["participants_columns"]))
            if facts.get("dataset_type"):
                parts.append(f"DatasetType: {facts['dataset_type']}")
            if facts.get("bids_version_in_dataset"):
                parts.append(f"dataset_description BIDSVersion: {facts['bids_version_in_dataset']}")
            if facts.get("derivatives"):
                parts.append("Derivative datasets inside: " + ", ".join(facts["derivatives"]))
            if facts.get("skip_validation_marker"):
                parts.append("Carries .SKIP_VALIDATION marker (excluded from repo validation runs).")
            ents = facts.get("entities_by_suffix") or {}
            if ents:
                parts.append(
                    "Entity usage by suffix: "
                    + "; ".join(f"{k}: [{', '.join(v)}]" for k, v in list(ents.items())[:25])
                )
            for note in (rec.get("scope") or {}).get("limitations") or []:
                parts.append("Limitation: " + note)
        elif facts.get("fixture_name"):
            parts.append(f"Validator test fixture: {facts['fixture_name']}")
            if facts.get("file_sample"):
                parts.append("Files: " + ", ".join(facts["file_sample"][:25]))
        else:
            parts.append(rec.get("summary", ""))
        parts.append(_versions_line(rec))

    else:  # Concept / ValidationRule / Conflict / other
        parts.append(f"Concept kind: {facts.get('concept_kind') or (rec.get('scope') or {}).get('knowledge_domain')}")
        if facts.get("entity_label"):
            parts.append(f"File-name label: {facts['entity_label']}")
        if facts.get("json_key"):
            parts.append(f"JSON key: {facts['json_key']}")
        if facts.get("value_type"):
            parts.append(f"Value type: {facts['value_type']}")
        if facts.get("format"):
            parts.append(f"Format: {facts['format']}")
        if facts.get("ordered_items"):
            parts.append("Order: " + ", ".join(facts["ordered_items"][:40]))
        if facts.get("datatypes"):
            parts.append("Datatypes: " + ", ".join(str(d) for d in facts["datatypes"]))
        if facts.get("selectors"):
            parts.append("Selectors: " + " | ".join(facts["selectors"]))
        if facts.get("checks"):
            parts.append("Checks: " + " | ".join(facts["checks"]))
        if facts.get("issue_fields"):
            parts.append(
                "Issue object fields: "
                + ", ".join(f"{f['field']} ({f['type']})" for f in facts["issue_fields"])
            )
        text = (rec.get("hints") or {}).get("text") or ""
        if text:
            parts.append("Content: " + util.truncate(text, 2500))
        if rec.get("summary"):
            parts.append("Summary: " + rec["summary"])
        parts.append(_versions_line(rec))

    parts.append("Source: " + _source_ref(rec))
    parts.append("Authority: " + AUTHORITY_LABELS.get(rec.get("authority", ""), rec.get("authority", "")))
    out = "\n".join(p for p in parts if p)
    if len(out) > MAX_RETRIEVAL:
        out = out[:MAX_RETRIEVAL].rstrip() + "\n…[retrieval text truncated; full content in raw_content]"
    return out


# ---------------------------------------------------------------------------
# Deterministic AI-style enrichment built from record facts
# ---------------------------------------------------------------------------

_CAUSE_RULES = [
    (r"\btypo", "Typo or misspelling in a file name, entity label, or key."),
    (r"\bmissing\b|not found|is required|absent", "A required file, entity, column, or metadata field is absent."),
    (r"deprecat|instead", "Use of a deprecated value, suffix, or feature where the sources specify a replacement."),
    (r"duplicate|with and without", "Duplicate files that differ only in extension (e.g. with and without .gz)."),
    (r"corrupt|unreadable|unable to parse|mislab", "Corrupt, truncated, or mislabeled binary/file content."),
    (r"encoding|utf-8|utf8", "File saved in an encoding other than UTF-8."),
    (r"inherit|higher level|override", "Metadata inheritance: a value is defined at a different directory level than expected."),
    (r"order|reorder", "Entity or column order deviates from the specification's defined order."),
    (r"additional|extra columns|extra files", "Extra columns or files that the rule does not cover."),
    (r"\bempty\b|too small", "File is empty or too small to contain valid content."),
    (r"symlink|symbolic link", "Symbolic link handling (broken link, cycle, or target outside the dataset)."),
    (r"case", "Case-sensitivity collision between file names."),
    (r"not part of BIDS|not included|naming scheme", "File name does not match any BIDS filename rule (often a typo, an unsupported file, or misplaced derived/source data)."),
]

_DIRECTIVE_RE = re.compile(
    r"\b(use|should|must|please|make sure|ensure|include|remove|rename|replace|check|expand|consult|add|specify|provide|verify|update|place|move)\b",
    re.I,
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def _resolution_from_sources(rec: dict) -> str:
    """Extract evidence-based remediation sentences from source messages.

    When the sources offer no directive, says so explicitly and points the
    reader at the recorded rule/conditions instead of inventing a fix.
    """
    facts = rec.get("facts") or {}
    text = " ".join(
        str(x) for x in (facts.get("message"), facts.get("implementation_reason"), rec.get("summary"))
        if x
    )
    directives = [s for s in _sentences(text) if _DIRECTIVE_RE.search(s) and len(s) >= 25]
    code = facts.get("issue_code") or rec.get("title")
    if directives:
        quoted = " ".join(f'"{d}"' for d in directives[:3])
        extra = ""
        if facts.get("rule_id"):
            extra = (
                f" Confirm the behavior against the specification rule {facts['rule_id']} "
                f"({(rec.get('source') or {}).get('path')}) before editing files, and re-run a BIDS validator "
                "afterwards to confirm the effect of any change."
            )
        return f"Guidance supported by the sources: {quoted}{extra}"
    rule_hint = facts.get("rule_id") or (rec.get("source") or {}).get("path") or "the recorded source"
    return (
        f"The configured sources do not document a specific remediation for {code}. "
        f"Based on the recorded conditions, start by inspecting the file(s) the issue points at and "
        f"comparing them against {rule_hint} at the recorded version. Do not suppress the issue "
        "(ignore/downgrade configuration) without understanding its cause, and re-run validation "
        "after any change. Treat any fix hypothesis as unverified until validation confirms it."
    )


def _why_it_matters(rec: dict) -> str:
    if rec["knowledge_type"] == "ValidationIssue":
        sev = (rec.get("facts") or {}).get("severity")
        if sev == "error":
            return (
                "Classified as an error. Per the validator documentation (docs/user_guide/issues.md), "
                "errors are violations of the BIDS specification (or cases where the validator cannot read "
                "the dataset) and must be resolved for a dataset to be considered valid."
            )
        if sev == "warning":
            return (
                "Classified as a warning. Per the validator documentation, warnings are emitted when "
                "recommendations are not followed or when a common problem may be present; they need user "
                "attention to verify the dataset's validity, and tooling may allow warnings to be ignored "
                "or downgraded via configuration."
            )
        return (
            "Severity is not declared by this source, so its impact cannot be stated reliably; determine the "
            "severity from the validator configuration/schema version in use before acting on it."
        )
    ktype = rec["knowledge_type"]
    if ktype == "FilenameRule":
        return "Filename and directory rules determine which file layouts are accepted; violating them typically produces validation errors (e.g. NOT_INCLUDED or filename-related codes)."
    if ktype == "MetadataRule":
        return "Metadata rules define required/recommended sidecar fields; missing required fields raise JSON_KEY_REQUIRED/SIDECAR_KEY_REQUIRED-style errors, missing recommended fields raise warnings."
    if ktype == "TabularRule":
        return "Tabular rules define required columns, ordering, and uniqueness constraints for TSV files; violations raise TSV_* issues."
    if ktype == "Relationship":
        return "Cross-file associations drive inheritance and sidecar/TSV pairing; misunderstanding them leads to misdiagnosed 'missing file' findings."
    if ktype == "Version":
        return "Version information prevents mixing rules from incompatible BIDS/schema releases when interpreting validation output."
    if ktype == "Example":
        return "Examples ground explanations of structure, but must not be treated as proof of universal validity."
    return "Provides retrievable background knowledge needed to interpret BIDS validation output and specification text."


def _example_scenarios(rec: dict) -> list[str]:
    facts = rec.get("facts") or {}
    if rec["knowledge_type"] == "ValidationIssue":
        scen: list[str] = []
        sfx = facts.get("related_suffixes") or []
        dts = facts.get("related_datatypes") or []
        paths = facts.get("related_paths") or []
        fields = facts.get("related_fields") or []
        if paths:
            scen.append(f'Illustrative (synthetic) scenario: a dataset contains {paths[0]}, which matches the issue selectors while an assertion fails.')
        if dts and sfx:
            scen.append(
                f"Illustrative (synthetic) scenario: a file such as "
                f"sub-01/{dts[0]}/sub-01_task-rest_{sfx[0]}.nii.gz satisfies the selectors "
                f"({', '.join((facts.get('selectors') or [])[:3]) or 'see rule'}) but fails an assertion, triggering {facts.get('issue_code')}."
            )
        elif sfx:
            scen.append(f"Illustrative (synthetic) scenario: a file ending in suffix '{sfx[0]}' triggers the issue when its assertion fails.")
        elif fields:
            scen.append(f"Illustrative (synthetic) scenario: a JSON sidecar references '{fields[0]}' in a context where the recorded condition does not hold.")
        if not scen:
            scen.append("Illustrative (synthetic) scenario: the issue fires for any file/context satisfying the recorded selectors while the recorded assertions fail; no concrete example dataset is documented in the sources.")
        return scen[:2]
    if rec["knowledge_type"] == "FilenameRule":
        sfx = facts.get("suffixes") or []
        dts = facts.get("datatypes") or []
        if sfx and dts:
            return [f"Illustrative (synthetic) example: sub-01/{dts[0]}/sub-01_{'_'.join(list((facts.get('entities') or {}).keys())[:2]) + '_' if facts.get('entities') else ''}{sfx[0]}.nii.gz matches this rule's suffix/datatype constraints."]
        return []
    if rec["knowledge_type"] == "Example":
        return [f"The recorded example '{facts.get('dataset_name') or facts.get('fixture_name')}' demonstrates this content in a repository file tree (see source path)."]
    if rec["knowledge_type"] == "MetadataRule" and facts.get("required_fields"):
        return [f"Illustrative (synthetic) scenario: a sidecar matching the selectors omits required field(s) {', '.join(facts['required_fields'][:4])}, producing a missing-key error."]
    if rec["knowledge_type"] == "TabularRule" and facts.get("required_columns"):
        return [f"Illustrative (synthetic) scenario: a TSV matching the selectors lacks required column(s) {', '.join(facts['required_columns'][:4])}."]
    return []


def _common_causes(rec: dict) -> list[str]:
    facts = rec.get("facts") or {}
    text = " ".join(
        str(x) for x in (facts.get("message"), facts.get("implementation_reason"), rec.get("summary"))
        if x
    ).lower()
    causes = [cause for pat, cause in _CAUSE_RULES if re.search(pat, text)]
    if causes:
        return causes[:4]
    if rec["knowledge_type"] == "ValidationIssue":
        return [
            "The configured sources do not analyze causes for this issue; causes must be inferred from the "
            "reported location and the recorded selector/assertion conditions."
        ]
    return []


def _description(rec: dict) -> str:
    facts = rec.get("facts") or {}
    ktype = rec["knowledge_type"]
    if ktype == "ValidationIssue":
        code = facts.get("issue_code") or rec["title"]
        sev = facts.get("severity")
        base = f"{code} is a validation issue reported by BIDS tooling"
        if sev:
            base += f" at severity '{sev}'"
        base += "."
        if facts.get("message"):
            base += f' Source message: "{facts["message"]}"'
        return util.truncate(base, MAX_ENRICH)
    return util.truncate(rec.get("summary") or "", MAX_ENRICH)


def _interpretation(rec: dict) -> str:
    facts = rec.get("facts") or {}
    ktype = rec["knowledge_type"]
    if ktype == "ValidationIssue":
        bits = []
        if facts.get("message"):
            bits.append(f"It means: {facts['message']}")
        if facts.get("selectors"):
            bits.append(
                "The issue is only evaluated for contexts where every selector holds: "
                + "; ".join(facts["selectors"])
            )
        if facts.get("checks"):
            bits.append(
                "The failing assertion is one of: "
                + "; ".join(facts["checks"])
                + ". All listed checks must pass for the context to be clean."
            )
        elif facts.get("implementation_reason"):
            bits.append("Validator implementation description: " + facts["implementation_reason"])
        if facts.get("rule_id"):
            bits.append(f"Underlying schema rule: {facts['rule_id']}.")
        if facts.get("defined_in_spec_schema") is False:
            bits.append(
                "This code is raised by validator implementation code rather than a dedicated specification "
                "schema check, so its text may change independently of the specification."
            )
        if facts.get("defined_in_spec_schema") is True:
            bits.append("This code is backed by a specification schema check (normative side) as well.")
        if facts.get("legacy_issue_number") is not None:
            bits.append(f"Legacy numeric validator issue number: {facts['legacy_issue_number']}.")
        return util.truncate(" ".join(bits), MAX_ENRICH)
    if ktype == "FilenameRule":
        ents = facts.get("entities") or {}
        bits = [rec.get("summary") or ""]
        if isinstance(ents, dict) and ents:
            req = [k for k, v in ents.items() if v == "required"]
            if req:
                bits.append("Entities required by this rule: " + ", ".join(req) + ".")
        return util.truncate(" ".join(bits), MAX_ENRICH)
    if ktype in ("MetadataRule", "TabularRule"):
        key = "required_fields" if ktype == "MetadataRule" else "required_columns"
        rec_key = "recommended_fields" if ktype == "MetadataRule" else "recommended_columns"
        bits = [rec.get("summary") or ""]
        if facts.get(key):
            bits.append(f"Required: {', '.join(facts[key])}.")
        if facts.get(rec_key):
            bits.append(f"Recommended: {', '.join(facts[rec_key])}.")
        return util.truncate(" ".join(bits), MAX_ENRICH)
    return util.truncate(rec.get("summary") or "", MAX_ENRICH)


def build_enrichment(rec: dict) -> dict:
    src = rec.get("source") or {}
    src_list = []
    for cand in (src.get("url"), src.get("repository"), src.get("path"), src.get("file")):
        if cand and cand not in src_list:
            src_list.append(str(cand))
    rev = src.get("revision")
    notes = []
    if rev:
        notes.append(f"Source revision: {rev}.")
    if rec.get("bids_version"):
        notes.append(f"Applies to BIDS {rec['bids_version']}.")
    if rec.get("schema_version"):
        notes.append(f"Schema {rec['schema_version']}.")
    if (rec.get("facts") or {}).get("severity_unknown"):
        notes.append("Severity unknown in this source; check the schema/validator version in use.")
    if rec.get("authority") == "validator_implementation":
        notes.append(
            "This describes implementation behavior of the validator; implementation details are not "
            "automatically normative BIDS requirements."
        )
    if rec.get("authority") == "dataset_example":
        notes.append("Example content is illustrative; presence in an examples repository does not prove validity.")
    confidence = "high"
    facts = rec.get("facts") or {}
    if rec["knowledge_type"] == "ValidationIssue" and not facts.get("message"):
        confidence = "medium"
    if rec["knowledge_type"] == "ValidationIssue" and facts.get("severity") is None:
        confidence = "medium"
    return {
        "description": _description(rec),
        "interpretation": _interpretation(rec),
        "why_it_matters": _why_it_matters(rec),
        "example_scenarios": _example_scenarios(rec),
        "common_causes": _common_causes(rec),
        "resolution_guidance": _resolution_from_sources(rec) if rec["knowledge_type"] in (
            "ValidationIssue", "FilenameRule", "MetadataRule", "TabularRule", "Troubleshooting", "Conflict"
        ) else (
            "Not applicable: this record is descriptive rather than a reported problem. "
            "Consult the linked sources before drawing remediation conclusions."
        ),
        "additional_notes": " ".join(notes),
        "confidence": confidence,
        "sources": src_list,
    }


# ---------------------------------------------------------------------------
# Main normalization
# ---------------------------------------------------------------------------

def _normalize_source(src: dict | None) -> dict:
    out = {k: None for k in _SOURCE_FIELDS}
    src = dict(src or {})
    for k in _SOURCE_FIELDS:
        if k in src:
            out[k] = src.pop(k)
    # keep any extra provenance keys (retrieved_at, source_id, ...)
    for k, v in src.items():
        out[k] = v
    if out["repository"] is None and out["url"] is None and out["path"] is None and out["file"] is None:
        out["path"] = "unknown"
    return out


def _record_id(ktype: str, record_key: str) -> str:
    return f"kb_{util.slugify(ktype, 18)}_{util.short_hash(record_key, 10)}"


def normalize(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rec in records:
        r = dict(rec)
        record_key = r.pop("record_key")
        hints = r.pop("hints", {}) or {}
        facts = r.pop("facts", {}) or {}
        ktype = r.get("knowledge_type", "Concept")
        if ktype not in config.KNOWLEDGE_TYPES:
            ktype = "Concept"
        final = {
            "id": _record_id(ktype, record_key),
            "_record_key": record_key,
            "knowledge_type": ktype,
            "title": util.normalize_ws(str(r.get("title") or ""))[:300] or record_key,
            "summary": util.normalize_ws(str(r.get("summary") or ""))[:1000],
            "retrieval_text": "",
            "scope": r.get("scope") or {},
            "bids_version": r.get("bids_version"),
            "schema_version": r.get("schema_version"),
            "source": _normalize_source(r.get("source")),
            "raw_content": r.get("raw_content") or {},
            "ai_enrichment": {},
            "facts": facts,
            "authority": r.get("authority") or "generated",
            "knowledge_family": r.get("knowledge_family") or f"misc:{record_key}",
            "relationships": [],
        }
        final["scope"].setdefault("authority", final["authority"])
        final["retrieval_text"] = build_retrieval_text(final)
        final["ai_enrichment"] = build_enrichment(final)
        final["_hints"] = hints
        out.append(final)
    return out
