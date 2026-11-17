"""Stage 8: fine-tuning dataset generation.

Produces ``finetuning_dataset.jsonl`` where every line has exactly two
top-level keys - ``prompt`` and ``completion`` - plus a side-car manifest
(``artifacts/ft_manifest.jsonl``) that keeps the training metadata (example
type, family, split, synthetic flag, source record ids) out of the training
file itself.

Guarantees implemented here:

* every issue code discovered in the knowledge base gets at least one example;
* up to ten example types per issue code (see ``ISSUE_EXAMPLE_TYPES``);
* extra rule/concept/example/version supplements for non-issue records;
* completions are built only from facts already present in the knowledge base
  (exact messages, selectors, checks, required fields, source paths) - no code,
  severity, or fix is invented;
* anything constructed by the pipeline is explicitly marked ``synthetic`` in the
  prompt text;
* every example belongs to a family (issue code, rule id, or knowledge family)
  and a family maps to exactly one split, so near-duplicate records cannot leak
  between train and evaluation;
* output is fully deterministic for a given knowledge base.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Callable

from . import config, util

# ---------------------------------------------------------------------------
# Example taxonomy
# ---------------------------------------------------------------------------

ISSUE_EXAMPLE_TYPES = [
    "explain",
    "conditions",
    "investigate",
    "resolution",
    "context_json_interpretation",
    "rule_relationship",
    "code_comparison",
    "version_differences",
    "corrected_example",
    "insufficient_info_question",
]

SUPPLEMENT_TYPES = {
    "FilenameRule": ["explain", "conditions"],
    "MetadataRule": ["explain", "conditions", "corrected_example"],
    "TabularRule": ["explain", "conditions", "corrected_example"],
    "ValidationRule": ["explain", "conditions"],
    "Relationship": ["explain", "conditions"],
    "Concept": ["explain"],
    "Troubleshooting": ["explain"],
    "Example": ["explain"],
    "Version": ["explain"],
}

MAX_COMPLETION = 2600
MAX_PROMPT = 2600
SYNTH_MARKER = "synthetic"

AUTHORITY_RANK = {
    "normative_specification": 0,
    "published_documentation": 1,
    "validator_implementation": 2,
    "schema_archive": 3,
    "dataset_example": 4,
    "generated": 5,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _facts(rec: dict) -> dict:
    return rec.get("facts") or {}


def _ai(rec: dict) -> dict:
    return rec.get("ai_enrichment") or {}


def _severity(rec: dict) -> str | None:
    sev = _facts(rec).get("severity")
    if isinstance(sev, dict):
        sev = sev.get("level") or sev.get("value")
    return str(sev) if sev else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _list_of(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _distinct(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        n = _norm(v)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _clip(text: str, limit: int = MAX_COMPLETION) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _split_for(family: str) -> str:
    """Deterministic family-level split (no example-level randomness)."""
    digest = int(hashlib.sha1(family.encode("utf-8")).hexdigest()[:8], 16)
    return "eval" if digest % 10 == 0 else "train"


def _source_ref(rec: dict) -> str:
    src = rec.get("source") or {}
    bits = []
    if src.get("repository"):
        bits.append(str(src["repository"]))
    if src.get("path") or src.get("url"):
        bits.append(str(src.get("path") or src.get("url")))
    if src.get("section"):
        bits.append(f"#{src['section']}")
    if src.get("line_start"):
        bits.append(
            f"lines {src['line_start']}"
            + (f"-{src['line_end']}" if src.get("line_end") else "")
        )
    if src.get("revision"):
        bits.append(f"revision {str(src['revision'])[:12]}")
    return "; ".join(bits)


# ---------------------------------------------------------------------------
# Knowledge-base indexing
# ---------------------------------------------------------------------------

def _load_kb(kb_path: str) -> list[dict]:
    records, errors = util.read_jsonl(kb_path)
    if errors:
        raise RuntimeError(f"knowledge base has unreadable lines: {errors[:5]}")
    if not records:
        raise RuntimeError("knowledge base is empty")
    return records


def build_issue_bundles(records: list[dict]) -> dict[str, dict]:
    """Group every record that documents a given issue code."""
    bundles: dict[str, dict] = defaultdict(
        lambda: {"records": [], "version_records": []}
    )
    for rec in records:
        ktype = rec.get("knowledge_type")
        if ktype == "ValidationIssue":
            code = _facts(rec).get("issue_code") or rec.get("title")
            if code:
                bundles[code]["records"].append(rec)
        elif ktype == "Version" and _facts(rec).get("issue_code"):
            bundles[_facts(rec)["issue_code"]]["version_records"].append(rec)
    for code in list(bundles):
        group = bundles[code]
        group["records"].sort(
            key=lambda r: (AUTHORITY_RANK.get(r.get("authority"), 9), str(r.get("id")))
        )
        group["primary"] = group["records"][0] if group["records"] else None
        group["code"] = code
        group["family"] = f"issue:{code}"
    return dict(sorted(bundles.items()))


def _code_mentions(records: list[dict]) -> set[str]:
    """Issue codes that appear verbatim in example/fixture records."""
    mentioned: set[str] = set()
    for rec in records:
        if rec.get("knowledge_type") not in ("Example", "Troubleshooting"):
            continue
        blob = json.dumps(
            {
                "t": rec.get("title"),
                "s": rec.get("summary"),
                "f": _facts(rec),
                "r": rec.get("raw_content"),
            },
            ensure_ascii=False,
        )
        for m in re.findall(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b", blob):
            mentioned.add(m)
    return mentioned


def _distinct_messages(bundle: dict) -> list[tuple[str, str]]:
    """(source label, exact message) pairs recorded for an issue code."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rec in bundle["records"]:
        f = _facts(rec)
        msg = f.get("message") or f.get("implementation_reason")
        if not msg:
            continue
        key = _norm(msg)
        if key in seen:
            continue
        seen.add(key)
        out.append((rec.get("authority") or rec["source"].get("source_id", "source"), str(msg).strip()))
    return out


# ---------------------------------------------------------------------------
# Issue example builders
# ---------------------------------------------------------------------------

def _ex_explain(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    if primary is None:
        return None
    sev = next(
        (s for s in (_severity(r) for r in bundle["records"]) if s), None
    )
    parts = [
        f"{code} is a BIDS validation issue"
        + (f" reported at severity '{sev}'." if sev else " (the configured sources do not declare a severity for it).")
    ]
    for label, msg in _distinct_messages(bundle):
        parts.append(f'Exact source message ({label}): "{_norm(msg)}"')
    if primary.get("summary"):
        parts.append(f"Summary: {_norm(primary['summary'])}")
    interp = _ai(primary).get("interpretation")
    if interp:
        parts.append(f"Meaning: {_norm(interp)}")
    parts.append(f"Recorded in: {_source_ref(primary)}")
    return {
        "example_type": "explain",
        "prompt": f"What does the BIDS validation issue {code} mean?",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _ex_conditions(code: str, bundle: dict, ctx: dict) -> dict | None:
    selectors = _distinct(
        s for r in bundle["records"] for s in _list_of(_facts(r).get("selectors"))
    )
    checks = _distinct(
        c for r in bundle["records"] for c in _list_of(_facts(r).get("checks"))
    )
    if not selectors and not checks:
        return None
    parts = []
    if selectors:
        parts.append("Selectors (all must hold before the issue is evaluated):")
        parts.extend(f"- {s}" for s in selectors)
    if checks:
        parts.append("Assertions checked (a failure raises the issue):")
        parts.extend(f"- {c}" for c in checks)
    rule_ids = _distinct(
        _facts(r).get("rule_id") for r in bundle["records"] if _facts(r).get("rule_id")
    )
    if rule_ids:
        parts.append("Schema rule(s): " + ", ".join(rule_ids))
    paths = _distinct(
        (r.get("source") or {}).get("path") for r in bundle["records"] if (r.get("source") or {}).get("path")
    )
    if paths:
        parts.append("Defined in: " + "; ".join(paths))
    return {
        "example_type": "conditions",
        "prompt": (
            f"Under what conditions is {code} raised? Give the exact selectors "
            "and the assertions that are checked."
        ),
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _ex_investigate(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    f = _facts(primary)
    suffixes = _distinct(f.get("related_suffixes"))
    datatypes = _distinct(f.get("related_datatypes"))
    fields = _distinct(f.get("related_fields"))
    paths = _distinct(f.get("related_paths"))
    raise_sites = _distinct(f.get("raise_site_files"))
    selectors = _distinct(f.get("selectors"))
    checks = _distinct(f.get("checks"))
    if not any((suffixes, datatypes, fields, paths, raise_sites, selectors, checks)):
        return None
    steps = ["Locate the reported file or metadata context; the sources tie this issue to:"]
    if paths:
        steps.append("- target path(s): " + ", ".join(paths))
    if datatypes and suffixes:
        steps.append(
            f"- synthetic example path pattern: sub-01/{datatypes[0]}/"
            f"sub-01_<entities>_{suffixes[0]}.<ext>"
        )
    if fields:
        steps.append("- metadata field(s): " + ", ".join(fields))
    if raise_sites:
        steps.append(
            "- the validator raises it in: "
            + ", ".join(raise_sites[:6])
            + " (implementation source; behavior may change independently of the specification)"
        )
    if selectors:
        steps.append("- confirm the context matches the selector(s): " + "; ".join(selectors))
    if checks:
        steps.append("- verify the recorded assertion(s): " + "; ".join(checks))
    steps.append(
        f"Compare the findings with {_source_ref(primary)} and re-run validation "
        "after each change to confirm the effect."
    )
    return {
        "example_type": "investigate",
        "prompt": f"I received a {code} finding in my dataset. How should I investigate it?",
        "completion": _clip("\n".join(steps)),
        "synthetic": "synthetic example" in " ".join(steps),
    }


def _ex_resolution(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    guidance = _ai(primary).get("resolution_guidance")
    if not guidance:
        return None
    parts = [f"Resolution guidance for {code} (taken from the configured sources):"]
    parts.append(str(guidance))
    rule_ids = _distinct(
        _facts(r).get("rule_id") for r in bundle["records"] if _facts(r).get("rule_id")
    )
    if rule_ids:
        parts.append("Verify against schema rule(s): " + ", ".join(rule_ids) + ".")
    return {
        "example_type": "resolution",
        "prompt": f"How do I resolve the BIDS validation issue {code}?",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _ex_context_json(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    f = _facts(primary)
    msg = next((m for _, m in _distinct_messages(bundle)), None)
    if not msg:
        return None
    sev = next(
        (s for s in (_severity(r) for r in bundle["records"]) if s), None
    )
    issue_ctx: dict[str, Any] = {"code": code}
    if sev:
        issue_ctx["severity"] = sev
    issue_ctx["issueMessage"] = _norm(msg)
    rule_id = next(
        (_facts(r).get("rule_id") for r in bundle["records"] if _facts(r).get("rule_id")),
        None,
    )
    if rule_id:
        issue_ctx["rule"] = rule_id
    paths = _distinct(f.get("related_paths"))
    issue_ctx["location"] = paths[0] if paths else "<synthetic placeholder path>"
    issue_ctx["line"] = 1
    issue_ctx["character"] = 1
    payload = json.dumps(issue_ctx, indent=2, ensure_ascii=False)
    prompt = (
        f"Interpret this validator issue context. The file path, line, and character "
        f"are synthetic placeholders:\n```json\n{payload}\n```\n"
        f"What does this output mean, and which parts come from the specification?"
    )
    parts = [
        f'The object describes issue {code}' + (f" at severity '{sev}'." if sev else "."),
        f'Field "issueMessage" carries the exact source message: "{_norm(msg)}".',
    ]
    if rule_id:
        parts.append(f'Field "rule" names the schema rule that defines the issue: {rule_id}.')
    origin = primary.get("source", {}).get("source_id")
    parts.append(
        f"The code definition is recorded by {origin} at {_source_ref(primary)}."
    )
    parts.append(
        'Fields "location", "line", and "character" are synthetic placeholders inserted '
        "for this example; real values are reported by the validator at runtime."
    )
    if f.get("defined_in_spec_schema") is False:
        parts.append(
            "This code is raised by validator implementation code rather than a dedicated "
            "specification schema check."
        )
    return {
        "example_type": "context_json_interpretation",
        "prompt": _clip(prompt, MAX_PROMPT),
        "completion": _clip("\n".join(parts)),
        "synthetic": True,
    }


def _ex_rule_relationship(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    f = _facts(primary)
    rule_id = f.get("rule_id")
    raise_sites = _distinct(f.get("raise_site_files"))
    if not rule_id and not raise_sites:
        return None
    parts = []
    if rule_id:
        parts.append(f"The schema rule {rule_id} defines {code} through its issue block.")
        rule_def = (_facts(primary) or {}).get("rule_definition") or (
            (primary.get("raw_content") or {}).get("rule_definition") or {}
        )
        issue_block = rule_def.get("issue") or {}
        if issue_block.get("message"):
            parts.append(f'Issue message stored in the rule: "{_norm(issue_block["message"])}".')
        if issue_block.get("level"):
            parts.append(f"Level stored in the rule: {issue_block['level']}.")
        for sel in _distinct(rule_def.get("selectors")):
            parts.append(f"Rule selector: {sel}")
        for chk in _distinct(rule_def.get("checks")):
            parts.append(f"Rule check: {chk}")
        parts.append(
            "When the context matches the selectors and a check fails, the issue "
            f"{code} is raised."
        )
        parts.append(f"Rule source: {_source_ref(primary)}.")
    if raise_sites:
        parts.append(
            "The validator implementation raises this code in: " + ", ".join(raise_sites[:6]) + "."
        )
    return {
        "example_type": "rule_relationship",
        "prompt": (
            f"How does the validation issue {code} relate to "
            f"{('schema rule ' + rule_id) if rule_id else 'the validator implementation files'}?"
        ),
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _pick_sibling(code: str, bundle: dict, ctx: dict) -> str | None:
    bundles: dict[str, dict] = ctx["bundles"]
    primary = bundle.get("primary")
    if primary is None:
        return None
    path = (primary.get("source") or {}).get("path")
    same_file = sorted(
        c
        for c, b in bundles.items()
        if c != code
        and b.get("primary") is not None
        and (b["primary"].get("source") or {}).get("path") == path
        and path
    )
    if same_file:
        return same_file[0]
    others = sorted(c for c in bundles if c != code)
    if not others:
        return None
    ranked = sorted(
        others, key=lambda c: (-_common_prefix_len(code, c), c)
    )
    best = ranked[0]
    if _common_prefix_len(code, best) >= 4:
        return best
    return best if len(others) < 40 else None


def _ex_code_comparison(code: str, bundle: dict, ctx: dict) -> dict | None:
    other = _pick_sibling(code, bundle, ctx)
    if not other:
        return None
    ob = ctx["bundles"][other]
    if ob.get("primary") is None:
        return None
    parts = []
    for label, b in ((code, bundle), (other, ob)):
        rec = b["primary"]
        sev = next((s for s in (_severity(r) for r in b["records"]) if s), None)
        msg = next((m for _, m in _distinct_messages(b)), None)
        parts.append(
            f"{label}: "
            + (f'severity {sev}; exact message "{_norm(msg)}"' if msg else f"severity {sev or 'not declared'}")
            + f"; defined in {_source_ref(rec)}."
        )
        sels = _distinct(s for r in b["records"] for s in _list_of(_facts(r).get("selectors")))
        if sels:
            parts.append(f"{label} selectors: " + "; ".join(sels))
    path_a = (bundle["primary"].get("source") or {}).get("path")
    path_b = (ob["primary"].get("source") or {}).get("path")
    if path_a and path_a == path_b:
        parts.append(
            f"Relationship: both codes are defined in the same source file ({path_a}); "
            "the configured sources do not document a further relationship between them."
        )
    else:
        parts.append(
            "Relationship: the configured sources define these codes in separate places "
            f"({path_a or 'unknown'} vs {path_b or 'unknown'}) and do not document a direct "
            "relationship between them; treat any similarity of names as coincidence unless "
            "a shared selector or rule is recorded."
        )
    return {
        "example_type": "code_comparison",
        "prompt": (
            f"Compare the BIDS validation issues {code} and {other}: what do the "
            "configured sources record for each, and are they related?"
        ),
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _ex_version_differences(code: str, bundle: dict, ctx: dict) -> dict | None:
    version_records = sorted(
        bundle["version_records"], key=lambda r: str(r.get("id"))
    )
    per_record_versions = _distinct(
        r.get("bids_version") or r.get("schema_version")
        for r in bundle["records"]
        if r.get("bids_version") or r.get("schema_version")
    )
    if not version_records and len(per_record_versions) < 2:
        return None
    parts = [f"Version-specific records for {code} in the knowledge base:"]
    for rec in version_records:
        f = _facts(rec)
        parts.append(f"- Record: {rec.get('title')} (source: {_source_ref(rec)}).")
        for variant in f.get("variants") or []:
            snaps = ", ".join(variant.get("schema_snapshots") or [])
            parts.append(
                f'  * level {variant.get("level") or "not declared"}; message '
                f'"{_norm(variant.get("message"))}" (schema snapshots: {snaps or "n/a"}).'
            )
        if f.get("severity_differs_across_versions"):
            parts.append(
                "  * Note: the recorded severity differs between schema snapshots for this code."
            )
        else:
            parts.append("  * Note: the recorded severity does not differ between the listed snapshots.")
    if len(per_record_versions) > 1:
        parts.append(
            "Records for this code also carry different version labels: "
            + ", ".join(per_record_versions)
            + "; consult the version-specific record before applying a rule from one "
            "version to another."
        )
    return {
        "example_type": "version_differences",
        "prompt": f"How does the definition of {code} differ across BIDS/schema versions?",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _extract_sidecar_fields(checks: list[str]) -> list[str]:
    fields: list[str] = []
    for chk in checks:
        for m in re.finditer(r"\bsidecar\.([A-Za-z][A-Za-z0-9_]*)", chk):
            if m.group(1) not in fields:
                fields.append(m.group(1))
        for m in re.finditer(r"\bassociations\.([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)", chk):
            name = f"{m.group(1)}.{m.group(2)}"
            if name not in fields:
                fields.append(name)
    return fields


def _ex_corrected_example(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    f = _facts(primary)
    checks = _distinct(f.get("checks"))
    if not checks:
        return None
    fields = _extract_sidecar_fields(checks)
    parts = [
        f"[synthetic example] The requirement below is quoted verbatim from the sources; "
        f"the values are placeholders, not recommendations from the specification."
    ]
    parts.append("Assertion(s) that must hold so that " + code + " is not raised:")
    parts.extend(f"- {c}" for c in checks)
    if fields:
        snippet = {name: "<value satisfying the recorded assertion>" for name in fields}
        parts.append(
            "Synthetic sidecar fragment that satisfies the assertion structurally:\n```json\n"
            + json.dumps(snippet, indent=2, ensure_ascii=False)
            + "\n```"
        )
        parts.append(
            "The concrete value must be computed from the dataset exactly as the assertion "
            "states; the placeholder above is synthetic and must be replaced with the "
            "dataset-specific value."
        )
    else:
        parts.append(
            "The corrected context must satisfy the assertion(s) verbatim; the sources do "
            "not document a literal example for this code."
        )
    return {
        "example_type": "corrected_example",
        "prompt": (
            f"Show a corrected, compliant example that avoids the validation issue {code}."
        ),
        "completion": _clip("\n".join(parts)),
        "synthetic": True,
    }


def _ex_insufficient_info(code: str, bundle: dict, ctx: dict) -> dict | None:
    primary = bundle["primary"]
    guidance = str(_ai(primary).get("resolution_guidance") or "")
    legacy = any(
        _facts(r).get("legacy_issue_number") is not None for r in bundle["records"]
    )
    # Candidate questions, in order; the first one whose gap is real is used.
    candidates: list[tuple[str, str]] = []
    if not legacy:
        candidates.append(
            (
                f"What legacy numeric validator issue number corresponds to {code} in "
                "older bids-validator releases?",
                f"Not documented: none of the configured sources records a legacy numeric "
                f"issue number for {code}. The sources record the symbolic code itself "
                f"(defined in {(primary.get('source') or {}).get('path') or 'the recorded source'}); "
                "older numeric identifiers are not part of the configured sources.",
            )
        )
    if code not in ctx["code_mentions"]:
        candidates.append(
            (
                f"Which dataset in bids-examples reproduces {code}?",
                f"Not documented: no bids-examples dataset in the configured sources is "
                f"linked to {code}. The knowledge base records the issue definition "
                f"({_source_ref(primary)}) but no example dataset association; absence of a "
                "recorded example does not mean the issue cannot occur.",
            )
        )
    if "do not document a specific remediation" in guidance or not guidance:
        candidates.append(
            (
                f"Do the BIDS documentation sources provide a step-by-step fix for {code}?",
                f"Not documented: the configured sources do not provide a step-by-step fix "
                f"procedure for {code}. What they do record is: "
                f"{_clip(next((m for _, m in _distinct_messages(bundle)), ''), 400)} "
                "Use that exact wording to diagnose the finding; any repair procedure beyond "
                "it is unverified.",
            )
        )
    # Always-safe fallback: occurrence statistics are simply not part of the sources.
    candidates.append(
        (
            f"How often does {code} occur in practice, and what is its false-positive rate?",
            f"Not documented: the configured sources contain no occurrence statistics, "
            f"false-positive rate, or prevalence data for {code}; they only record its "
            f"definition, severity, and conditions (see {_source_ref(primary)}).",
        )
    )
    prompt, completion = candidates[0]
    return {
        "example_type": "insufficient_info_question",
        "prompt": prompt,
        "completion": _clip(completion),
        "synthetic": False,
    }


#: The ten example types, paired with their builders (order is the taxonomy).
ISSUE_BUILDERS: list[tuple[str, Callable[[str, dict, dict], dict | None]]] = [
    ("explain", _ex_explain),
    ("conditions", _ex_conditions),
    ("investigate", _ex_investigate),
    ("resolution", _ex_resolution),
    ("context_json_interpretation", _ex_context_json),
    ("rule_relationship", _ex_rule_relationship),
    ("code_comparison", _ex_code_comparison),
    ("version_differences", _ex_version_differences),
    ("corrected_example", _ex_corrected_example),
    ("insufficient_info_question", _ex_insufficient_info),
]
assert [name for name, _ in ISSUE_BUILDERS] == ISSUE_EXAMPLE_TYPES


# ---------------------------------------------------------------------------
# Supplement builders (non-issue records)
# ---------------------------------------------------------------------------

def _rule_explain(rec: dict) -> dict | None:
    f = _facts(rec)
    parts = [f"Rule: {f.get('rule_id') or rec.get('title')}."]
    if rec.get("summary"):
        parts.append(f"Summary: {_norm(rec['summary'])}")
    for label, key in (
        ("Requirement level", "requirement_level"),
        ("Path", "path"),
        ("File stem", "stem"),
        ("Allowed suffixes", "suffixes"),
        ("Allowed extensions", "extensions"),
        ("Allowed datatypes", "datatypes"),
        ("Required fields", "required_fields"),
        ("Recommended fields", "recommended_fields"),
        ("Required columns", "required_columns"),
        ("Recommended columns", "recommended_columns"),
        ("Target file", "related_paths"),
        ("Relationship type", "relationship_type"),
    ):
        vals = _list_of(f.get(key))
        if vals:
            parts.append(f"{label}: " + ", ".join(vals[:40]))
    ents = f.get("entities")
    if isinstance(ents, dict) and ents:
        parts.append(
            "Entities (requirement level): "
            + ", ".join(f"{k}={v}" for k, v in list(ents.items())[:40])
        )
    parts.append(f"Source: {_source_ref(rec)}")
    return {
        "example_type": "explain",
        "prompt": f"What does the BIDS rule {f.get('rule_id') or rec.get('title')} require?",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _rule_conditions(rec: dict) -> dict | None:
    f = _facts(rec)
    selectors = _distinct(f.get("selectors"))
    checks = _distinct(f.get("checks"))
    if not selectors and not checks:
        return None
    parts = []
    rid = f.get("rule_id") or rec.get("title")
    if selectors:
        parts.append(f"{rid} applies when all of the following hold:")
        parts.extend(f"- {s}" for s in selectors)
    if checks:
        parts.append("Assertions checked:")
        parts.extend(f"- {c}" for c in checks)
    parts.append(f"Source: {_source_ref(rec)}")
    return {
        "example_type": "conditions",
        "prompt": f"When does the BIDS rule {rid} apply? Quote its selectors and checks.",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _rule_corrected(rec: dict) -> dict | None:
    f = _facts(rec)
    required = _list_of(f.get("required_fields")) or _list_of(f.get("required_columns"))
    if not required:
        return None
    key_name = "field" if f.get("required_fields") else "column"
    rid = f.get("rule_id") or rec.get("title")
    if f.get("required_fields"):
        payload = {name: "<dataset-specific value>" for name in required}
        fmt = "JSON sidecar fragment"
    else:
        payload = {name: "<dataset-specific value>" for name in required}
        fmt = "TSV header line"
    if f.get("required_fields"):
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
        block = f"```json\n{rendered}\n```"
    else:
        rendered = "\t".join(required)
        block = f"```\n{rendered}\n```"
    parts = [
        f"[synthetic example] Minimal structure for rule {rid}. The {key_name} list is "
        "quoted exactly from the rule; the values are placeholders.",
        f"Required {key_name}s (verbatim): " + ", ".join(required),
        f"{fmt}:\n{block}",
        f"Source: {_source_ref(rec)}",
    ]
    return {
        "example_type": "corrected_example",
        "prompt": (
            f"Show a minimal compliant example that satisfies BIDS rule {rid} "
            f"(required {key_name}s only)."
        ),
        "completion": _clip("\n".join(parts)),
        "synthetic": True,
    }


def _concept_explain(rec: dict) -> dict | None:
    f = _facts(rec)
    parts = []
    if rec.get("summary"):
        parts.append(_norm(rec["summary"]))
    text = str(
        (rec.get("raw_content") or {}).get("text")
        or (rec.get("hints") or {}).get("text")
        or ""
    ).strip()
    if text:
        parts.append(_clip(_norm(text), 1200))
    for label, key in (
        ("Value type", "value_type"),
        ("Format", "format"),
        ("Entity label", "entity_label"),
        ("Description", "description"),
    ):
        if f.get(key):
            parts.append(f"{label}: {f[key]}")
    if f.get("ordered_items"):
        parts.append("Order: " + ", ".join(_list_of(f["ordered_items"])[:40]))
    if f.get("enum"):
        parts.append("Allowed values: " + ", ".join(_list_of(f["enum"])[:40]))
    parts.append(f"Source: {_source_ref(rec)}")
    title = _norm(rec.get("title"))
    if not title:
        return None
    prompt = f"What is '{title}' in BIDS?"
    if not parts:
        return None
    return {
        "example_type": "explain",
        "prompt": _clip(prompt, MAX_PROMPT),
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _example_explain(rec: dict) -> dict | None:
    f = _facts(rec)
    name = f.get("dataset_name") or f.get("fixture_name") or rec.get("title")
    if not name:
        return None
    parts = []
    if rec.get("summary"):
        parts.append(_norm(rec["summary"]))
    for label, key in (
        ("Dataset", "dataset_name"),
        ("Validator test fixture", "fixture_name"),
        ("DatasetType", "dataset_type"),
        ("BIDSVersion in dataset_description", "bids_version_in_dataset"),
        ("Subjects", "subject_count"),
        ("Sessions", "session_count"),
    ):
        if f.get(key) is not None:
            parts.append(f"{label}: {f[key]}")
    if f.get("datatypes"):
        dts = f["datatypes"]
        if isinstance(dts, dict):
            parts.append("Datatypes: " + ", ".join(f"{k} ({v} files)" for k, v in list(dts.items())[:12]))
        else:
            parts.append("Datatypes: " + ", ".join(_list_of(dts)))
    if f.get("suffixes"):
        parts.append("Suffixes: " + ", ".join(_list_of(f["suffixes"])[:40]))
    if f.get("participants_columns"):
        parts.append("participants.tsv columns: " + ", ".join(_list_of(f["participants_columns"])[:40]))
    if f.get("derivatives"):
        parts.append("Derivative datasets inside: " + ", ".join(_list_of(f["derivatives"])))
    if f.get("skip_validation_marker"):
        parts.append(
            "Carries a .SKIP_VALIDATION marker, so the repository's own validation runs "
            "exclude it."
        )
    if f.get("validation_status_verified_by_this_pipeline") is False:
        parts.append(
            "Validation status was not re-verified by this pipeline; do not treat this "
            "example as proof of validity."
        )
    parts.append(f"Source: {_source_ref(rec)}")
    return {
        "example_type": "explain",
        "prompt": f"Describe the structure of the BIDS example '{name}'.",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


def _version_explain(rec: dict) -> dict | None:
    f = _facts(rec)
    parts = [f"Record: {rec.get('title')}"]
    for key in (
        "release_version", "release_date", "bids_version", "schema_version",
        "snapshot", "validator_release", "latest_released", "content_stage",
        "released_bids_versions", "commit", "issue_count",
    ):
        if f.get(key) is not None:
            parts.append(f"{key}: {f[key]}")
    if f.get("issue_codes"):
        parts.append("Issue codes in this snapshot: " + ", ".join(_list_of(f["issue_codes"])[:60]))
    if rec.get("summary"):
        parts.append(f"Summary: {_norm(rec['summary'])}")
    parts.append(f"Source: {_source_ref(rec)}")
    return {
        "example_type": "explain",
        "prompt": f"What does the version record '{_norm(rec.get('title'))}' document?",
        "completion": _clip("\n".join(parts)),
        "synthetic": False,
    }


SUPPLEMENT_BUILDERS: dict[str, dict[str, Callable[[dict], dict | None]]] = {
    "FilenameRule": {"explain": _rule_explain, "conditions": _rule_conditions},
    "MetadataRule": {
        "explain": _rule_explain,
        "conditions": _rule_conditions,
        "corrected_example": _rule_corrected,
    },
    "TabularRule": {
        "explain": _rule_explain,
        "conditions": _rule_conditions,
        "corrected_example": _rule_corrected,
    },
    "ValidationRule": {"explain": _rule_explain, "conditions": _rule_conditions},
    "Relationship": {"explain": _rule_explain, "conditions": _rule_conditions},
    "Concept": {"explain": _concept_explain},
    "Troubleshooting": {"explain": _concept_explain},
    "Example": {"explain": _example_explain},
    "Version": {"explain": _version_explain},
}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(
    kb_path: str | None = None,
    ft_path: str | None = None,
    ft_manifest_path: str | None = None,
) -> dict:
    kb_path = kb_path or config.KB_PATH
    ft_path = ft_path or config.FT_PATH
    ft_manifest_path = ft_manifest_path or config.FT_MANIFEST_PATH

    records = _load_kb(kb_path)
    bundles = build_issue_bundles(records)
    code_mentions = _code_mentions(records)

    examples: list[dict] = []       # {"prompt","completion"} + meta
    builder_errors: list[dict] = []
    ctx = {"bundles": bundles, "code_mentions": code_mentions}

    # --- issue bundles: up to ten example types per issue code -------------
    for code, bundle in bundles.items():
        family = bundle["family"]
        for _type_name, builder in ISSUE_BUILDERS:
            try:
                ex = builder(code, bundle, ctx)
            except Exception as exc:  # noqa: BLE001 - one builder must not kill the run
                builder_errors.append(
                    {"stage": "issue", "type": _type_name, "key": code,
                     "error": f"{type(exc).__name__}: {exc}"}
                )
                ex = None
            if not ex or not ex.get("prompt") or not ex.get("completion"):
                continue
            ex["example_type"] = _type_name
            ex["family"] = family
            ex["issue_code"] = code
            ex["kb_record_ids"] = [r["id"] for r in bundle["records"]]
            ex["source_ids"] = sorted(
                {
                    (r.get("source") or {}).get("source_id")
                    for r in bundle["records"]
                    if (r.get("source") or {}).get("source_id")
                }
            )
            ex["knowledge_type"] = "ValidationIssue"
            examples.append(ex)

    # --- supplements: one or more examples per non-issue record ------------
    for rec in records:
        ktype = rec.get("knowledge_type")
        builders = SUPPLEMENT_BUILDERS.get(ktype)
        if not builders:
            continue
        family = str(rec.get("knowledge_family") or f"{ktype}:{rec.get('id')}")
        for ex_type in SUPPLEMENT_TYPES.get(ktype, []):
            builder = builders.get(ex_type)
            if builder is None:
                continue
            try:
                ex = builder(rec)
            except Exception as exc:  # noqa: BLE001
                builder_errors.append(
                    {"stage": "supplement", "type": ex_type,
                     "key": str(rec.get("id")), "error": f"{type(exc).__name__}: {exc}"}
                )
                ex = None
            if not ex or not ex.get("prompt") or not ex.get("completion"):
                continue
            ex["example_type"] = ex_type
            ex["family"] = family
            ex["issue_code"] = None
            ex["kb_record_ids"] = [rec["id"]]
            ex["source_ids"] = [
                (rec.get("source") or {}).get("source_id")
            ] if (rec.get("source") or {}).get("source_id") else []
            ex["knowledge_type"] = ktype
            examples.append(ex)

    # --- deterministic ordering, split assignment, de-duplication ----------
    type_rank = {t: i for i, t in enumerate(ISSUE_EXAMPLE_TYPES)}
    examples.sort(
        key=lambda e: (
            0 if e["issue_code"] else 1,
            str(e.get("issue_code") or ""),
            str(e["family"]),
            type_rank.get(e["example_type"], 99),
            str(e["knowledge_type"]),
            str(e["kb_record_ids"]),
            str(e["prompt"]),
        )
    )

    ft_lines: list[dict] = []
    manifest: list[dict] = []
    seen_prompts: set[str] = set()
    duplicates_dropped = 0
    for i, ex in enumerate(examples):
        prompt = _clip(str(ex["prompt"]).strip(), MAX_PROMPT)
        completion = str(ex["completion"]).strip()
        if not prompt or not completion:
            continue
        if prompt in seen_prompts:
            duplicates_dropped += 1
            continue
        seen_prompts.add(prompt)
        if ex.get("synthetic") and "synthetic" not in prompt.lower():
            # Never let a constructed example go unflagged.
            prompt = f"[synthetic example] {prompt}"
        split = _split_for(ex["family"])
        example_id = util.stable_id(
            "ft",
            ex["family"],
            ex["example_type"],
            hashlib.sha1(prompt.encode("utf-8")).hexdigest(),
            length=12,
        )
        ft_lines.append({"prompt": prompt, "completion": completion})
        manifest.append(
            {
                "example_id": example_id,
                "example_type": ex["example_type"],
                "family": ex["family"],
                "family_group": str(ex["family"]).split(":")[0],
                "split": split,
                "synthetic": bool(ex.get("synthetic")),
                "issue_code": ex.get("issue_code"),
                "knowledge_type": ex.get("knowledge_type"),
                "kb_record_ids": ex.get("kb_record_ids") or [],
                "source_ids": ex.get("source_ids") or [],
            }
        )

    n = util.write_jsonl(ft_path, ft_lines)
    util.write_jsonl(ft_manifest_path, manifest)

    # --- statistics --------------------------------------------------------
    by_type = Counter(m["example_type"] for m in manifest)
    by_group = Counter(m["family_group"] for m in manifest)
    by_split = Counter(m["split"] for m in manifest)
    by_ktype = Counter(m["knowledge_type"] for m in manifest)
    by_source = Counter()
    for m in manifest:
        for sid in m["source_ids"]:
            by_source[sid] += 1
    family_counts = Counter(m["family"] for m in manifest)
    issue_codes_with_examples = sorted(
        {m["issue_code"] for m in manifest if m["issue_code"]}
    )
    kb_issue_codes = sorted(bundles.keys())
    synthetic_n = sum(1 for m in manifest if m["synthetic"])

    issue_by_type = Counter(
        m["example_type"] for m in manifest if m["issue_code"]
    )
    stats = {
        "path": ft_path,
        "manifest_path": ft_manifest_path,
        "examples_written": n,
        "manifest_entries": len(manifest),
        "duplicates_dropped": duplicates_dropped,
        "by_example_type": dict(sorted(by_type.items())),
        "issue_example_types": {
            t: issue_by_type.get(t, 0) for t in ISSUE_EXAMPLE_TYPES
        },
        "by_family_group": dict(sorted(by_group.items())),
        "by_knowledge_type": dict(sorted(by_ktype.items())),
        "by_split": dict(sorted(by_split.items())),
        "by_source_id": dict(sorted(by_source.items())),
        "families": len(family_counts),
        "top_families": [
            {"family": fam, "examples": cnt}
            for fam, cnt in family_counts.most_common(20)
        ],
        "unique_issue_codes_in_kb": len(kb_issue_codes),
        "unique_issue_codes_with_examples": len(issue_codes_with_examples),
        "issue_codes_without_examples": sorted(
            set(kb_issue_codes) - set(issue_codes_with_examples)
        ),
        "issue_examples": sum(1 for m in manifest if m["issue_code"]),
        "supplement_examples": sum(1 for m in manifest if not m["issue_code"]),
        "synthetic_examples": synthetic_n,
        "builder_errors": builder_errors,
        "sha256": {
            "finetuning_dataset": util.sha256_text(
                open(ft_path, "r", encoding="utf-8").read()
            ),
            "manifest": util.sha256_text(
                open(ft_manifest_path, "r", encoding="utf-8").read()
            ),
            "knowledge_base": util.sha256_text(
                open(kb_path, "r", encoding="utf-8").read()
            ),
        },
    }
    util.write_json(
        os.path.join(config.ARTIFACTS_DIR, "ft_stats.json"), stats
    )
    return stats
