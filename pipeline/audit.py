"""Stage 9: validation and auditing.

Verifies the two deliverables (``knowledge_base.jsonl`` and
``finetuning_dataset.jsonl``) against the pipeline's own requirements and
against the *sources themselves* (the sources are re-parsed here, so a
drift between source content and stored records is reported instead of
being papered over).

Output: ``artifacts/validation_report.json`` with an ``ok`` flag that is
true only when no check failed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from typing import Any

from . import config, finetune, normalize, parse, util

KB_REQUIRED_FIELDS = [
    "id", "knowledge_type", "title", "summary", "retrieval_text", "scope",
    "bids_version", "schema_version", "source", "raw_content", "ai_enrichment",
]
ID_RE = re.compile(r"^kb_[a-z0-9]+_[0-9a-f]{6,16}$")
FT_KEYS = {"prompt", "completion"}
CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
EXAMPLE_TYPES = list(finetune.ISSUE_EXAMPLE_TYPES)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


class Audit:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: Any = "", status: str | None = None) -> bool:
        st = status or ("pass" if ok else "fail")
        self.checks.append({"name": name, "status": st, "detail": detail})
        return st != "fail"

    def warn(self, name: str, detail: Any = "") -> None:
        self.checks.append({"name": name, "status": "warn", "detail": detail})

    @property
    def failed(self) -> list[str]:
        return [c["name"] for c in self.checks if c["status"] == "fail"]

    @property
    def warned(self) -> list[str]:
        return [c["name"] for c in self.checks if c["status"] == "warn"]


# ---------------------------------------------------------------------------
# Source truth (re-parsed independently of the extraction stage)
# ---------------------------------------------------------------------------

def _source_issue_truth() -> tuple[dict[str, dict], dict]:
    """code -> {messages, levels, selectors, checks, paths} from the sources."""
    truth: dict[str, dict] = defaultdict(
        lambda: {"messages": set(), "levels": set(), "selectors": set(),
                  "checks": set(), "paths": set(), "origins": set()}
    )

    spec = parse.parse_spec_schema()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            issue = node.get("issue")
            if isinstance(issue, dict) and issue.get("code"):
                entry = truth[issue["code"]]
                if issue.get("message"):
                    entry["messages"].add(_norm(issue["message"]))
                if issue.get("level"):
                    entry["levels"].add(_norm(issue["level"]).lower())
                entry["paths"].add("src/schema/" + path)
                entry["origins"].add("bids-specification-repo")
            for key in ("selectors", "checks"):
                vals = node.get(key)
                if isinstance(vals, list) and node.get("issue"):
                    code = (node.get("issue") or {}).get("code")
                    if code:
                        for v in vals:
                            if isinstance(v, str):
                                truth[code][key].add(_norm(v))
            for v in node.values():
                walk(v, path)
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    for rel, data in (spec.get("rules") or {}).items():
        walk(data, rel)

    for item in parse.parse_validator_issue_list():
        entry = truth[item["code"]]
        if item.get("reason"):
            entry["messages"].add(_norm(item["reason"]))
        if item.get("severity"):
            entry["levels"].add(_norm(item["severity"]).lower())
        entry["paths"].add("src/issues/list.ts")
        entry["origins"].add("bids-validator")

    for code, locs in parse.parse_validator_code_sites().items():
        # Only non-test raise sites count as discovered issues: codes that appear
        # exclusively in *.test.ts (e.g. fixture codes like TOTALLY_FAKE_CODE_12345)
        # are test data, not validation issues users can encounter.
        if any(not loc.get("test") for loc in locs):
            entry = truth[code]
            entry["origins"].add("bids-validator")
    return dict(truth), spec


def _docs_page_urls() -> set[str]:
    try:
        acq = util.read_json(os.path.join(config.ARTIFACTS_DIR, "acquisition_report.json"))
    except (OSError, ValueError):
        return set()
    return {p["url"] for p in acq.get("docs", {}).get("pages", []) if p.get("url")}


# ---------------------------------------------------------------------------
# Knowledge-base checks
# ---------------------------------------------------------------------------

def audit_kb(a: Audit) -> tuple[list[dict], dict]:
    path = config.KB_PATH
    info: dict[str, Any] = {"path": path, "exists": os.path.exists(path)}
    records: list[dict] = []
    if not a.add("kb.file_exists", os.path.exists(path), path):
        return records, info

    records, errors = util.read_jsonl(path)
    a.add("kb.lines_parse", not errors, {"records": len(records), "errors": errors[:10]})
    info["records"] = len(records)
    if not records:
        a.add("kb.non_empty", False, "no records")
        return records, info
    a.add("kb.non_empty", True, len(records))

    missing = Counter()
    for r in records:
        for f in KB_REQUIRED_FIELDS:
            if f not in r:
                missing[f] += 1
    a.add("kb.template_fields", not missing, dict(missing))

    ids = [r.get("id") for r in records]
    dup = [i for i, c in Counter(ids).items() if c > 1]
    a.add("kb.ids_unique", not dup, {"duplicate_ids": dup[:20], "count": len(dup)})
    bad_ids = [i for i in ids if not isinstance(i, str) or not ID_RE.match(i)]
    a.add("kb.id_format", not bad_ids, {"bad": bad_ids[:10], "count": len(bad_ids)})

    bad_types = sorted({r.get("knowledge_type") for r in records} - set(config.KNOWLEDGE_TYPES))
    a.add("kb.knowledge_types", not bad_types, bad_types)

    bad_auth = sorted({r.get("authority") for r in records} - set(config.AUTHORITIES))
    a.add("kb.authorities", not bad_auth, bad_auth)

    bad_src = sorted(
        {
            (r.get("source") or {}).get("source_id")
            for r in records
            if (r.get("source") or {}).get("source_id") not in config.SOURCE_IDS
        }
    )
    a.add("kb.source_registry", not bad_src, bad_src)
    no_prov = [
        r.get("id") for r in records
        if not any((r.get("source") or {}).get(k) for k in ("revision", "url", "path", "file"))
    ]
    a.add("kb.provenance_present", not no_prov, {"records_without_provenance": no_prov[:10]})

    # --- issue records ----------------------------------------------------
    issues = [r for r in records if r.get("knowledge_type") == "ValidationIssue"]
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in issues:
        code = (r.get("facts") or {}).get("issue_code") or r.get("title")
        by_code[code].append(r)
    info["issue_records"] = len(issues)
    info["unique_issue_codes"] = len(by_code)

    code_title_mismatch = [
        r["id"] for r in issues
        if (r.get("facts") or {}).get("issue_code")
        and r["title"] != (r.get("facts") or {}).get("issue_code")
    ]
    a.add("kb.issue_title_matches_code", not code_title_mismatch, code_title_mismatch[:10])

    no_code_in_text = [
        r["id"] for r in issues
        if r["title"] not in r.get("retrieval_text", "")
    ]
    a.add("kb.issue_code_in_retrieval_text", not no_code_in_text,
          {"count": len(no_code_in_text), "ids": no_code_in_text[:10]})

    # --- source truth comparison -----------------------------------------
    truth, spec = _source_issue_truth()
    info["discovered_issue_codes"] = len(truth)
    missing_codes = sorted(set(truth) - set(by_code))
    a.add("kb.covers_all_discovered_issue_codes", not missing_codes,
          {"discovered": len(truth), "in_kb": len(by_code), "missing": missing_codes})

    msg_mismatch, sel_mismatch, sev_mismatch = [], [], []
    for code, t in truth.items():
        if code not in by_code:
            continue
        kb_msgs = {_norm((r.get("facts") or {}).get("message")) for r in by_code[code]}
        for msg in t["messages"]:
            if msg and msg not in kb_msgs:
                msg_mismatch.append({"code": code, "source_message": msg[:120],
                                     "kb_messages": sorted(m for m in kb_msgs if m)[:3]})
        kb_sels = {
            _norm(v)
            for r in by_code[code]
            for key in ("selectors", "checks")
            for v in ((r.get("facts") or {}).get(key) or [])
        }
        for sel in t["selectors"] | t["checks"]:
            if sel and sel not in kb_sels:
                sel_mismatch.append({"code": code, "source": sel[:120]})
        kb_sev = {
            _norm((r.get("facts") or {}).get("severity")).lower()
            for r in by_code[code]
        }
        if t["levels"] and not (t["levels"] & kb_sev):
            sev_mismatch.append({"code": code, "source_levels": sorted(t["levels"]),
                                 "kb_levels": sorted(s for s in kb_sev if s)})
    a.add("kb.issue_messages_exact", not msg_mismatch,
          {"mismatches": len(msg_mismatch), "sample": msg_mismatch[:5]})
    a.add("kb.selectors_and_checks_preserved", not sel_mismatch,
          {"missing": len(sel_mismatch), "sample": sel_mismatch[:5]})
    a.add("kb.issue_severities_match_sources", not sev_mismatch,
          {"mismatches": len(sev_mismatch), "sample": sev_mismatch[:5]})

    # KB may contain additional codes, but each must trace to a real source file.
    extra = sorted(set(by_code) - set(truth))
    bad_paths = []
    for code in extra:
        for rec in by_code[code]:
            src = rec.get("source") or {}
            repo = src.get("repository") or ""
            local = repo.rstrip("/").split("/")[-1]
            p = src.get("path")
            if local and p:
                full = os.path.join(config.SOURCES_DIR, local, p)
                if not os.path.exists(full):
                    bad_paths.append({"code": code, "path": f"{local}/{p}"})
            elif src.get("url") and src["url"] not in _docs_page_urls():
                bad_paths.append({"code": code, "url": src["url"]})
    a.add("kb.extra_codes_trace_to_sources", not bad_paths,
          {"extra_codes": len(extra), "unresolvable": bad_paths[:8]})
    info["kb_only_issue_codes"] = extra

    # --- version separation ----------------------------------------------
    # A record may carry a development version only when it comes from a source
    # that actually is the development branch (or from a dataset that declares
    # its own BIDSVersion), never from the published 'stable' documentation.
    dev_allowed_sources = {"bids-specification-repo", "bids-schema", "bids-examples"}
    version_errors = []
    for r in records:
        bv = str(r.get("bids_version") or "")
        sv = str(r.get("schema_version") or "")
        src_id = (r.get("source") or {}).get("source_id")
        if ("dev" in bv or "dev" in sv) and src_id not in dev_allowed_sources:
            version_errors.append({"id": r["id"], "bids_version": bv,
                                   "schema_version": sv, "source": src_id})
        if r.get("authority") == "published_documentation" and "dev" in bv:
            version_errors.append({"id": r["id"], "problem": "docs record claims dev version"})
    a.add("kb.version_specific_records_not_mixed", not version_errors,
          {"violations": version_errors[:10], "count": len(version_errors)})
    dev_records = sum(
        1 for r in records
        if "dev" in str(r.get("bids_version") or "")
        or "dev" in str(r.get("schema_version") or "")
    )
    stable_records = sum(1 for r in records if r.get("bids_version") == "1.11.2")
    info["version_scoped_records"] = {"development": dev_records, "stable_1_11_2": stable_records}

    # --- conflicts ---------------------------------------------------------
    conflicts = [r for r in records if r.get("knowledge_type") == "Conflict"]
    unresolved = [
        r for r in conflicts
        if "unresolved" in json.dumps(r.get("raw_content") or {}).lower()
    ]
    a.add("kb.conflicts_marked_unresolved", len(unresolved) == len(conflicts),
          {"conflicts": len(conflicts), "marked_unresolved": len(unresolved)})

    # --- duplicates --------------------------------------------------------
    # Two records are duplicates only when they agree on type, title, source
    # locator (path/section/line) and rule/section discriminator: the same code
    # or heading legitimately appears twice in one file for different rules or
    # sections.
    def _dup_key(r: dict) -> tuple:
        src = r.get("source") or {}
        f = r.get("facts") or {}
        return (
            r.get("knowledge_type"),
            _norm(r.get("title")),
            src.get("path") or src.get("url"),
            src.get("section"),
            src.get("line_start"),
            str(
                f.get("rule_id")
                or f.get("object_key")
                or f.get("section_anchor")
                or f.get("fixture_name")
                or ""
            ),
        )

    seen = Counter(_dup_key(r) for r in records)
    dups = [list(k) + [c] for k, c in seen.items() if c > 1]
    identical_rt = [
        {"text": t[:100], "count": c}
        for t, c in Counter(_norm(r.get("retrieval_text")) for r in records).items()
        if c > 1
    ]
    a.add("kb.no_duplicate_records", not dups, {"duplicates": dups[:10], "count": len(dups)})
    a.add("kb.retrieval_texts_distinct", not identical_rt,
          {"duplicates": identical_rt[:5], "count": len(identical_rt)})

    # --- enrichment sanity (no invented quotes) ---------------------------
    def _record_text(rec: dict) -> str:
        """All string values of a record, unescaped (json.dumps would mangle
        backslash sequences such as the literal ``\\n`` inside messages)."""
        out: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)

        walk(rec)
        return _norm(" \n ".join(out))

    bad_quotes, missing_fields = [], []
    for r in records:
        ai = r.get("ai_enrichment") or {}
        for f in normalize.ENRICH_FIELDS:
            if f not in ai:
                missing_fields.append({"id": r["id"], "field": f})
                break
        record_text = _record_text(r)
        text = str(ai.get("resolution_guidance") or "")
        for quote in re.findall(r'"([^"]{25,400})"', text):
            if _norm(quote) not in record_text:
                bad_quotes.append({"id": r["id"], "quote": quote[:80]})
    a.add("kb.enrichment_fields_complete", not missing_fields,
          {"missing": missing_fields[:10], "count": len(missing_fields)})
    a.add("kb.resolution_guidance_quotes_sources", not bad_quotes,
          {"invented_quotes": bad_quotes[:5], "count": len(bad_quotes)})

    empty_retrieval = [r["id"] for r in records if not str(r.get("retrieval_text") or "").strip()]
    a.add("kb.retrieval_text_non_empty", not empty_retrieval,
          {"count": len(empty_retrieval)})

    # --- source coverage ---------------------------------------------------
    src_counts = Counter((r.get("source") or {}).get("source_id") for r in records)
    missing_sources = sorted(set(config.SOURCE_IDS) - set(src_counts))
    a.add("kb.all_sources_covered", not missing_sources,
          dict(sorted(src_counts.items())))
    info["records_by_source"] = dict(sorted(src_counts.items()))
    info["records_by_knowledge_type"] = dict(
        sorted(Counter(r.get("knowledge_type") for r in records).items())
    )

    # --- search smoke tests ------------------------------------------------
    smoke = {}
    for query in ("ACCEL_CHANNEL_COUNT", "Inheritance", "participants.tsv", "NOT_INCLUDED"):
        hits = sum(1 for r in records if query in r.get("retrieval_text", ""))
        smoke[query] = hits
    a.add("kb.search_smoke_tests", all(v > 0 for v in smoke.values()), smoke)
    info["search_smoke"] = smoke

    # --- authority separation of version records ---------------------------
    version_records = [r for r in records if r.get("knowledge_type") == "Version"]
    info["version_records"] = len(version_records)
    return records, info


# ---------------------------------------------------------------------------
# Fine-tuning dataset checks
# ---------------------------------------------------------------------------

def audit_ft(a: Audit, kb_records: list[dict]) -> tuple[list[dict], dict]:
    path = config.FT_PATH
    info: dict[str, Any] = {"path": path, "exists": os.path.exists(path)}
    if not a.add("ft.file_exists", os.path.exists(path), path):
        return [], info
    lines, errors = util.read_jsonl(path)
    a.add("ft.lines_parse", not errors, {"examples": len(lines), "errors": errors[:10]})
    info["examples"] = len(lines)
    if not lines:
        a.add("ft.non_empty", False, "no examples")
        return [], info
    a.add("ft.non_empty", True, len(lines))

    bad_keys = [i for i, r in enumerate(lines, 1) if set(r.keys()) != FT_KEYS]
    a.add("ft.only_prompt_completion_keys", not bad_keys,
          {"violations": bad_keys[:10], "count": len(bad_keys)})

    bad_values = [
        i for i, r in enumerate(lines, 1)
        if not str(r.get("prompt") or "").strip()
        or not str(r.get("completion") or "").strip()
        or r.get("prompt") == r.get("completion")
    ]
    a.add("ft.prompt_completion_non_empty", not bad_values,
          {"violations": bad_values[:10], "count": len(bad_values)})

    prompts = [str(r.get("prompt")) for r in lines]
    dup_prompts = [p for p, c in Counter(prompts).items() if c > 1]
    a.add("ft.prompts_unique", not dup_prompts,
          {"duplicates": dup_prompts[:5], "count": len(dup_prompts)})

    # --- manifest ----------------------------------------------------------
    manifest: list[dict] = []
    man_path = config.FT_MANIFEST_PATH
    if a.add("ft.manifest_exists", os.path.exists(man_path), man_path):
        manifest, man_errors = util.read_jsonl(man_path)
        a.add("ft.manifest_parse", not man_errors, man_errors[:10])
    a.add("ft.manifest_aligned", len(manifest) == len(lines),
          {"manifest": len(manifest), "examples": len(lines)})
    man_ids = [m.get("example_id") for m in manifest]
    a.add("ft.manifest_ids_unique", len(set(man_ids)) == len(man_ids),
          {"unique": len(set(man_ids)), "total": len(man_ids)})
    bad_types = sorted({m.get("example_type") for m in manifest} - set(EXAMPLE_TYPES))
    a.add("ft.manifest_example_types_known", not bad_types, bad_types)

    # family -> exactly one split (no train/eval leakage)
    family_split: dict[str, set[str]] = defaultdict(set)
    for m in manifest:
        family_split[m.get("family")].add(m.get("split"))
    leaked = {f: sorted(s) for f, s in family_split.items() if len(s) > 1}
    a.add("ft.family_split_grouping", not leaked, {"leaking_families": list(leaked)[:10]})
    splits = Counter(m.get("split") for m in manifest)
    a.add("ft.both_splits_populated", set(splits) >= {"train", "eval"}, dict(splits))
    info["splits"] = dict(splits)
    info["families"] = len(family_split)

    # every example marked synthetic says so in the prompt
    unflagged = []
    for idx, m in enumerate(manifest):
        if idx >= len(lines):
            break
        if m.get("synthetic") and "synthetic" not in str(lines[idx].get("prompt", "")).lower():
            unflagged.append(m.get("example_id"))
    a.add("ft.synthetic_examples_flagged", not unflagged,
          {"unflagged": unflagged[:10], "count": len(unflagged)})
    info["synthetic_examples"] = sum(1 for m in manifest if m.get("synthetic"))

    # --- issue-code coverage ------------------------------------------------
    kb_codes = sorted(
        {
            (r.get("facts") or {}).get("issue_code") or r.get("title")
            for r in kb_records
            if r.get("knowledge_type") == "ValidationIssue"
        }
    )
    prompt_blob = "\n".join(prompts)
    without = [c for c in kb_codes if not re.search(rf"\b{re.escape(c)}\b", prompt_blob)]
    a.add("ft.every_issue_code_has_example", not without,
          {"issue_codes": len(kb_codes), "without_examples": without})

    by_type = Counter(m.get("example_type") for m in manifest)
    missing_types = [t for t in EXAMPLE_TYPES if by_type.get(t, 0) == 0]
    a.add("ft.all_issue_example_types_present", not missing_types,
          {t: by_type.get(t, 0) for t in EXAMPLE_TYPES})
    info["by_example_type"] = dict(sorted(by_type.items()))
    info["issue_examples"] = sum(1 for m in manifest if m.get("issue_code"))
    info["supplement_examples"] = sum(1 for m in manifest if not m.get("issue_code"))

    # --- no invented issue codes -------------------------------------------
    kb_blob = open(config.KB_PATH, "r", encoding="utf-8").read()
    ft_blob = open(path, "r", encoding="utf-8").read()
    unknown = sorted(
        {
            tok
            for tok in CODE_TOKEN_RE.findall(ft_blob)
            if tok not in kb_blob
        }
    )
    a.add("ft.no_invented_issue_codes", not unknown,
          {"tokens_absent_from_kb": unknown[:20], "count": len(unknown)})

    # --- completions are grounded in the KB --------------------------------
    # Token-coverage test: content words used in a completion must come from
    # words that actually occur in the knowledge base.  This catches invented
    # field names, invented codes, and fabricated messages without rescanning
    # the whole knowledge base once per example.
    kb_norm = _norm(kb_blob)
    token_re = re.compile(r"[a-z][a-z0-9_'-]{2,}")
    kb_tokens = set(token_re.findall(kb_norm.lower()))
    weak, coverages = [], []
    for i, r in enumerate(lines):
        comp = _norm(str(r.get("completion") or "")).lower()
        toks = set(token_re.findall(comp))
        if not toks:
            continue
        cov = sum(1 for t in toks if t in kb_tokens) / len(toks)
        coverages.append(cov)
        if cov < 0.6:
            weak.append({"line": i + 1, "coverage": round(cov, 2),
                         "start": comp[:70]})
    mean_cov = round(sum(coverages) / len(coverages), 4) if coverages else 0.0
    allowed = max(5, int(0.02 * len(lines)))
    a.add("ft.completions_trace_to_kb", len(weak) <= allowed,
          {"mean_token_coverage": mean_cov, "low_coverage_examples": len(weak),
           "allowed_max": allowed, "sample": weak[:5]})

    # --- determinism --------------------------------------------------------
    try:
        with tempfile.TemporaryDirectory(dir=config.ARTIFACTS_DIR) as tmp:
            ft2 = os.path.join(tmp, "ft.jsonl")
            man2 = os.path.join(tmp, "manifest.jsonl")
            finetune.generate(config.KB_PATH, ft2, man2)
            same = (
                util.sha256_text(open(path, encoding="utf-8").read())
                == util.sha256_text(open(ft2, encoding="utf-8").read())
                and util.sha256_text(open(man_path, encoding="utf-8").read())
                == util.sha256_text(open(man2, encoding="utf-8").read())
            )
        a.add("ft.deterministic_regeneration", same,
              "regenerated dataset is byte-identical")
    except Exception as exc:  # noqa: BLE001
        a.add("ft.deterministic_regeneration", False, f"{type(exc).__name__}: {exc}")

    info["records_by_source_in_examples"] = dict(
        sorted(Counter(sid for m in manifest for sid in (m.get("source_ids") or [])).items())
    )
    return lines, info


# ---------------------------------------------------------------------------
# Coverage / gaps
# ---------------------------------------------------------------------------

def _coverage(kb_records: list[dict], ft_lines: list[dict], manifest: list[dict]) -> dict:
    by_source = {}
    src_counts = Counter((r.get("source") or {}).get("source_id") for r in kb_records)
    ex_src_counts = Counter(sid for m in manifest for sid in (m.get("source_ids") or []))
    for sid in config.SOURCE_IDS:
        by_source[sid] = {
            "kb_records": src_counts.get(sid, 0),
            "ft_examples": ex_src_counts.get(sid, 0),
            "role": config.SOURCES[sid]["role"],
            "authority": config.SOURCES[sid]["authority"],
        }
    kb_codes = sorted(
        {
            (r.get("facts") or {}).get("issue_code") or r.get("title")
            for r in kb_records
            if r.get("knowledge_type") == "ValidationIssue"
        }
    )
    per_code = {
        c: sum(1 for m in manifest if m.get("issue_code") == c) for c in kb_codes
    }
    return {
        "by_source": by_source,
        "by_knowledge_type": dict(
            sorted(Counter(r.get("knowledge_type") for r in kb_records).items())
        ),
        "issue_codes": {
            "total": len(kb_codes),
            "with_ft_examples": sum(1 for c in kb_codes if per_code[c] > 0),
            "codes_without_examples": sorted(c for c, n in per_code.items() if n == 0),
            "min_examples_per_code": min(per_code.values()) if per_code else 0,
            "max_examples_per_code": max(per_code.values()) if per_code else 0,
            "mean_examples_per_code": round(sum(per_code.values()) / len(per_code), 2)
            if per_code else 0,
            "codes_with_single_example": sorted(c for c, n in per_code.items() if n == 1),
        },
        "ft_examples_total": len(ft_lines),
        "ft_examples_by_split": dict(Counter(m.get("split") for m in manifest)),
        "ft_examples_by_type": dict(
            sorted(Counter(m.get("example_type") for m in manifest).items())
        ),
        "ft_examples_by_family_group": dict(
            sorted(Counter(str(m.get("family", "")).split(":")[0] for m in manifest).items())
        ),
    }


def _gaps(a: Audit, kb_records: list[dict], coverage: dict) -> list[dict]:
    gaps: list[dict] = []
    for sid, info in coverage["by_source"].items():
        if info["kb_records"] == 0:
            gaps.append({"kind": "source_without_records", "source": sid})
    for t, n in coverage["ft_examples_by_type"].items():
        if n == 0:
            gaps.append({"kind": "example_type_unused", "type": t})
    if coverage["issue_codes"]["codes_without_examples"]:
        gaps.append({
            "kind": "issue_codes_without_examples",
            "codes": coverage["issue_codes"]["codes_without_examples"],
        })
    empty_retrieval = [
        r["id"] for r in kb_records if not str(r.get("retrieval_text") or "").strip()
    ]
    if empty_retrieval:
        gaps.append({"kind": "records_without_retrieval_text", "ids": empty_retrieval[:20]})
    # documentation pages that produced no record
    try:
        acq = util.read_json(os.path.join(config.ARTIFACTS_DIR, "acquisition_report.json"))
        pages = [p["url"] for p in acq.get("docs", {}).get("pages", [])]
        covered = {
            (r.get("source") or {}).get("url")
            for r in kb_records
            if (r.get("source") or {}).get("url")
        }
        uncovered = [
            u for u in pages
            if u not in covered and "changelog" not in u.lower() and not u.endswith("sitemap.xml")
        ]
        if uncovered:
            gaps.append({"kind": "docs_pages_without_records", "pages": uncovered[:20],
                         "count": len(uncovered)})
    except (OSError, ValueError):
        pass
    return gaps


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_all() -> dict:
    a = Audit()
    kb_records, kb_info = audit_kb(a)
    ft_lines, ft_info = audit_ft(a, kb_records)

    manifest: list[dict] = []
    if os.path.exists(config.FT_MANIFEST_PATH):
        manifest, _ = util.read_jsonl(config.FT_MANIFEST_PATH)

    coverage = _coverage(kb_records, ft_lines, manifest)
    gaps = _gaps(a, kb_records, coverage)

    report = {
        "ok": not a.failed,
        "generated_at": util.utc_now_iso(),
        "checks_total": len(a.checks),
        "checks_passed": sum(1 for c in a.checks if c["status"] == "pass"),
        "checks_failed": len(a.failed),
        "checks_warned": len(a.warned),
        "failures": a.failed,
        "warnings": a.warned,
        "kb": kb_info,
        "ft": ft_info,
        "coverage": coverage,
        "gaps": gaps,
        "checks": a.checks,
        "regeneration": {
            "command": "python -m pipeline.run --force",
            "staged_command": "python -m pipeline.run --stages 4,5,6,7,8,9",
            "note": (
                "Stage 9 (this audit) must finish with ok=true; --force clears the "
                "stage state and rebuilds all artifacts and both deliverables from "
                "the pinned source revisions recorded in artifacts/inventory.json."
            ),
        },
    }
    util.write_json(config.VALIDATION_REPORT_PATH, report)
    return report


if __name__ == "__main__":  # pragma: no cover
    rep = run_all()
    print(json.dumps({k: rep[k] for k in ("ok", "checks_total", "failures", "warnings")}, indent=2))
