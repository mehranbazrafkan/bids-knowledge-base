"""Stage 4: semantic extraction.

Turns the structured parses from stage 3 into raw knowledge records.
Raw records are internal (they carry a ``record_key`` used later for
deterministic IDs); stage 5 (normalize) converts them to the final schema.

Every record preserves the original structured definition in
``raw_content`` and the exact source location in ``source``.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import config, parse, util

MAX_TEXT = 12_000          # cap for stored document/section text
MAX_RETRIEVAL = 6_000      # cap for retrieval_text
MAX_RAW_ITEMS = 200        # cap for lists/maps inside raw_content

LEVEL_WORDS = ("error", "warning", "hint", "ignore")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _src(source_id: str, revisions: dict, **kw) -> dict:
    rev = (revisions.get(source_id) or {}).get("commit")
    base = {
        "repository": config.SOURCES[source_id]["url"] if config.SOURCES[source_id]["kind"] == "git" else None,
        "file": None,
        "path": None,
        "url": None,
        "section": None,
        "line_start": None,
        "line_end": None,
        "revision": rev,
        "retrieved_at": util.utc_now_iso(),
        "source_id": source_id,
    }
    base.update(kw)
    return base


def _bounded(d: dict, limit: int = MAX_RAW_ITEMS) -> dict:
    if len(d) <= limit:
        return d
    keys = sorted(d)[:limit]
    out = {k: d[k] for k in keys}
    out["_truncated"] = f"showing {limit} of {len(d)} entries"
    return out


def _bounded_list(items: list, limit: int = MAX_RAW_ITEMS) -> list:
    if len(items) <= limit:
        return items
    return items[:limit] + [f"... {len(items) - limit} more items omitted"]


def _first_sentence(text: str, limit: int = 260) -> str:
    t = util.normalize_ws(text or "")
    if not t:
        return ""
    m = re.search(r"(?<=[.!?])\s", t)
    if m and m.start() > 40:
        t = t[: m.start() + 1]
    return util.truncate(t, limit)


def _selector_tokens(selectors: list[str] | None) -> dict:
    """Extract concrete BIDS tokens referenced by selector expressions."""
    out: dict = {"suffixes": [], "datatypes": [], "extensions": [], "fields": [], "paths": [], "entities": []}
    for sel in selectors or []:
        if not isinstance(sel, str):
            continue
        for m in re.finditer(r"suffix\s*==\s*['\"]([^'\"]+)['\"]", sel):
            out["suffixes"].append(m.group(1))
        for m in re.finditer(r"datatype\s*==\s*['\"]([^'\"]+)['\"]", sel):
            out["datatypes"].append(m.group(1))
        for m in re.finditer(r"extension\s*==\s*['\"]([^'\"]+)['\"]", sel):
            out["extensions"].append(m.group(1))
        for m in re.finditer(r"(?:sidecar|json|context)\.([A-Za-z_][A-Za-z0-9_]*)", sel):
            out["fields"].append(m.group(1))
        for m in re.finditer(r"path\s*==\s*['\"]([^'\"]+)['\"]", sel):
            out["paths"].append(m.group(1))
        for m in re.finditer(r"['\"]([a-z]+)-<(?:label|index)>['\"]", sel):
            out["entities"].append(m.group(1))
        if '"task" in entities' in sel or "'task' in entities" in sel:
            out["entities"].append("task")
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


def _normalize_fields(fields: Any) -> dict:
    """Normalize sidecar/tabular requirement maps to {name: {level, ...}}."""
    out: dict = {}
    if not isinstance(fields, dict):
        return out
    for name, spec in fields.items():
        if isinstance(spec, str):
            out[name] = {"level": spec}
        elif isinstance(spec, dict):
            entry = dict(spec)
            entry.setdefault("level", "optional" if "level" not in entry else entry["level"])
            out[name] = entry
        else:
            out[name] = {"level": str(spec)}
    return out


def _rule_id(rel: str, key: str) -> str:
    stem = rel.replace(".yaml", "").replace(".yml", "")
    return "rules." + stem.replace("/", ".") + "." + key


def _split_check_dir(rel: str) -> str:
    top = rel.split("/")[0]
    if "/" not in rel:
        return top.replace(".yaml", "").replace(".yml", "")
    return top


# ===========================================================================
# A. Specification schema rules (source: bids-specification-repo)
# ===========================================================================

def extract_spec_schema(parsed: dict, revisions: dict) -> list[dict]:
    records: list[dict] = []
    bids_version = parsed.get("bids_version")
    schema_version = parsed.get("schema_version")

    for rel in sorted(parsed["rules"]):
        data = parsed["rules"][rel]
        if isinstance(data, list):
            records.extend(_extract_ordered_list_rule(rel, data, parsed, revisions, bids_version, schema_version))
            continue
        if not isinstance(data, dict):
            continue
        kind = _classify_rule_file(rel)
        for key in sorted(data):
            rule = data[key]
            if not isinstance(rule, dict):
                continue
            resolved = parse.resolve_rule(rule, parsed)
            line = parsed["rule_lines"].get((rel, key))
            legacy = parsed["legacy_numbers"].get((rel, key))
            if kind == "ValidationIssue" or _has_issue(resolved):
                rec = _issue_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line, legacy)
                if rec:
                    records.append(rec)
                # A check rule may exist without its own issue block? then generic:
                if kind == "ValidationIssue" and not _has_issue(resolved) and rec is None:
                    records.append(
                        _validation_rule_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line)
                    )
                continue
            if kind == "FilenameRule":
                records.append(_filename_rule_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line))
            elif kind == "MetadataRule":
                records.append(_metadata_rule_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line))
                # custom per-field issues inside sidecar rules (e.g. NO_AUTHORS)
                for fr in _field_issue_records(rel, key, resolved, parsed, revisions, bids_version, schema_version, line):
                    records.append(fr)
            elif kind == "TabularRule":
                records.append(_tabular_rule_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line))
            else:
                records.append(_validation_rule_record(rel, key, rule, resolved, parsed, revisions, bids_version, schema_version, line))
    return records


def _classify_rule_file(rel: str) -> str:
    if rel.startswith("files/") or rel.startswith("directories."):
        return "FilenameRule"
    if rel.startswith("sidecars/") or rel.startswith("json/"):
        return "MetadataRule"
    if rel.startswith("tabular_data/"):
        return "TabularRule"
    if rel.startswith("checks/") or rel.startswith("errors."):
        return "ValidationIssue"
    return "ValidationRule"


def _has_issue(rule: dict) -> bool:
    if not isinstance(rule, dict):
        return False
    if isinstance(rule.get("issue"), dict) and rule["issue"].get("code"):
        return True
    if rule.get("code") and rule.get("message"):
        return True
    return False


def _issue_from_rule(rule: dict) -> dict | None:
    if isinstance(rule.get("issue"), dict) and rule["issue"].get("code"):
        return dict(rule["issue"])
    if rule.get("code") and rule.get("message"):
        return {"code": rule["code"], "message": rule["message"], "level": rule.get("level")}
    return None


def _issue_record(rel, key, raw_rule, rule, parsed, revisions, bids_version, schema_version, line, legacy=None) -> dict | None:
    issue = _issue_from_rule(rule)
    if not issue:
        return None
    code = str(issue["code"])
    level = str(issue.get("level") or "").lower() or None
    message = util.normalize_ws(str(issue.get("message") or ""))
    selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, (str, int, float))]
    checks = [str(c) for c in rule.get("checks") or [] if isinstance(c, (str, int, float))]
    tokens = _selector_tokens(selectors)
    ref_note = rule.get("_unresolved_ref")
    rule_id = _rule_id(rel, key)
    hint = "hint" if rel.startswith("checks/hints") else None
    return {
        "record_key": f"spec_issue|{rel}|{key}|{code}|{schema_version}",
        "knowledge_type": "ValidationIssue",
        "knowledge_family": f"issue:{code}",
        "authority": "normative_specification",
        "title": code,
        "summary": message or f"Schema check '{key}' raises {code}.",
        "scope": {
            "knowledge_domain": "validation_issue",
            "rule_group": _split_check_dir(rel),
            "source_file": f"src/schema/rules/{rel}",
            "rule_name": key,
            "check_kind": hint or "schema_check",
        },
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": _src(
            "bids-specification-repo",
            revisions,
            file=f"src/schema/rules/{rel}",
            path=f"src/schema/rules/{rel}",
            line_start=(line or (None, None))[0],
            line_end=(line or (None, None))[1],
        ),
        "raw_content": {
            "rule_definition": rule,
            "original_rule_definition": raw_rule,
            "issue_definition": issue,
            "file": f"src/schema/rules/{rel}",
            "rule_name": key,
            "unresolved_ref": ref_note,
        },
        "facts": {
            "issue_code": code,
            "severity": level,
            "message": message,
            "selectors": selectors,
            "checks": checks,
            "rule_id": rule_id,
            "legacy_issue_number": legacy,
            "related_suffixes": tokens["suffixes"],
            "related_datatypes": tokens["datatypes"],
            "related_fields": tokens["fields"],
            "related_paths": tokens["paths"],
        },
        "hints": {"kind": "issue"},
    }


def _validation_rule_record(rel, key, raw_rule, rule, parsed, revisions, bids_version, schema_version, line) -> dict:
    selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, str)]
    checks = [str(c) for c in rule.get("checks") or [] if isinstance(c, str)]
    return {
        "record_key": f"spec_rule|{rel}|{key}|{schema_version}",
        "knowledge_type": "ValidationRule",
        "knowledge_family": f"rule:{_rule_id(rel, key)}",
        "authority": "normative_specification",
        "title": f"Validation rule {key}",
        "summary": f"Schema rule '{key}' in rules/{rel} "
        + (f"guarded by {len(selectors)} selector(s)." if selectors else "."),
        "scope": {"knowledge_domain": "validation_rule", "rule_group": _split_check_dir(rel), "rule_name": key},
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": _src(
            "bids-specification-repo",
            revisions,
            file=f"src/schema/rules/{rel}",
            path=f"src/schema/rules/{rel}",
            line_start=(line or (None, None))[0],
            line_end=(line or (None, None))[1],
        ),
        "raw_content": {"rule_definition": rule, "file": f"src/schema/rules/{rel}", "rule_name": key},
        "facts": {"selectors": selectors, "checks": checks, "rule_id": _rule_id(rel, key)},
        "hints": {"kind": "rule"},
    }


def _filename_rule_record(rel, key, raw_rule, rule, parsed, revisions, bids_version, schema_version, line) -> dict:
    entities = rule.get("entities")
    if isinstance(entities, dict):
        ref = entities.get("$ref")
        merged = {}
        if ref:
            target = parse.resolve_ref(str(ref), parsed)
            if isinstance(target, dict):
                merged.update(target)
        merged.update({k: v for k, v in entities.items() if k != "$ref"})
        entities = merged
        entities_ref = ref
    else:
        entities_ref = None
    selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, str)]
    suffixes = [str(s) for s in rule.get("suffixes") or [] if isinstance(s, (str, int))]
    extensions = [str(e) for e in rule.get("extensions") or [] if isinstance(e, str)]
    datatypes = [str(d) for d in rule.get("datatypes") or [] if isinstance(d, str)]
    path = rule.get("path")
    stem = rule.get("stem")
    level = str(rule.get("level")) if rule.get("level") else None
    rule_id = _rule_id(rel, key)
    is_directory = rel.startswith("directories")
    title = f"Directory layout rule {key}" if is_directory else f"Filename rule {key}"
    summary_bits = []
    if suffixes:
        summary_bits.append("suffixes: " + ", ".join(suffixes))
    if datatypes:
        summary_bits.append("datatypes: " + ", ".join(datatypes))
    if extensions:
        summary_bits.append("extensions: " + ", ".join(extensions))
    if level:
        summary_bits.append(f"level: {level}")
    if path:
        summary_bits.append(f"path: {path}")
    return {
        "record_key": f"filename_rule|{rel}|{key}|{schema_version}",
        "knowledge_type": "FilenameRule",
        "knowledge_family": f"rule:{rule_id}",
        "authority": "normative_specification",
        "title": title,
        "summary": f"{rule_id}: " + ("; ".join(summary_bits) if summary_bits else "directory layout definition"),
        "scope": {"knowledge_domain": "filename_rule", "rule_group": _split_check_dir(rel), "rule_name": key},
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": _src(
            "bids-specification-repo",
            revisions,
            file=f"src/schema/rules/{rel}",
            path=f"src/schema/rules/{rel}",
            line_start=(line or (None, None))[0],
            line_end=(line or (None, None))[1],
        ),
        "raw_content": {
            "rule_definition": rule,
            "file": f"src/schema/rules/{rel}",
            "rule_name": key,
            "entities_ref": entities_ref,
        },
        "facts": {
            "rule_id": rule_id,
            "suffixes": suffixes,
            "extensions": extensions,
            "datatypes": datatypes,
            "entities": entities,
            "selectors": selectors,
            "requirement_level": level,
            "path": path,
            "stem": stem,
        },
        "hints": {"kind": "filename_rule", "is_directory": is_directory},
    }


def _metadata_rule_record(rel, key, raw_rule, rule, parsed, revisions, bids_version, schema_version, line) -> dict:
    fields = _normalize_fields(rule.get("fields"))
    selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, str)]
    tokens = _selector_tokens(selectors)
    levels = {k: v.get("level") for k, v in fields.items()}
    required = sorted(k for k, v in levels.items() if v == "required")
    recommended = sorted(k for k, v in levels.items() if v == "recommended")
    rule_id = _rule_id(rel, key)
    return {
        "record_key": f"metadata_rule|{rel}|{key}|{schema_version}",
        "knowledge_type": "MetadataRule",
        "knowledge_family": f"rule:{rule_id}",
        "authority": "normative_specification",
        "title": f"Metadata rule {key}",
        "summary": f"{rule_id} requires {len(required)} and recommends {len(recommended)} metadata field(s)"
        + (f" for {', '.join(tokens['suffixes'][:4])}" if tokens["suffixes"] else "")
        + (f" in {', '.join(tokens['paths'][:2])}" if tokens["paths"] else "")
        + ".",
        "scope": {
            "knowledge_domain": "metadata_rule",
            "rule_group": _split_check_dir(rel),
            "rule_name": key,
            "sidecar_fields": sorted(fields),
        },
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": _src(
            "bids-specification-repo",
            revisions,
            file=f"src/schema/rules/{rel}",
            path=f"src/schema/rules/{rel}",
            line_start=(line or (None, None))[0],
            line_end=(line or (None, None))[1],
        ),
        "raw_content": {
            "rule_definition": rule,
            "file": f"src/schema/rules/{rel}",
            "rule_name": key,
        },
        "facts": {
            "rule_id": rule_id,
            "selectors": selectors,
            "fields": _bounded(fields, 400),
            "field_levels": levels,
            "required_fields": required,
            "recommended_fields": recommended,
            "related_suffixes": tokens["suffixes"],
            "related_datatypes": tokens["datatypes"],
            "related_paths": tokens["paths"],
            "related_fields_in_selectors": tokens["fields"],
        },
        "hints": {"kind": "metadata_rule"},
    }


def _field_issue_records(rel, key, rule, parsed, revisions, bids_version, schema_version, line) -> list[dict]:
    """Custom issues attached to individual fields of a metadata rule."""
    out = []
    fields = rule.get("fields")
    if not isinstance(fields, dict):
        return out
    for fname, fspec in fields.items():
        if not isinstance(fspec, dict) or not isinstance(fspec.get("issue"), dict):
            continue
        issue = fspec["issue"]
        if not issue.get("code"):
            continue
        code = str(issue["code"])
        selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, str)]
        message = util.normalize_ws(str(issue.get("message") or ""))
        out.append(
            {
                "record_key": f"spec_field_issue|{rel}|{key}|{fname}|{code}|{schema_version}",
                "knowledge_type": "ValidationIssue",
                "knowledge_family": f"issue:{code}",
                "authority": "normative_specification",
                "title": code,
                "summary": message or f"Field '{fname}' in rule {key} raises {code}.",
                "scope": {
                    "knowledge_domain": "validation_issue",
                    "rule_group": _split_check_dir(rel),
                    "source_file": f"src/schema/rules/{rel}",
                    "rule_name": key,
                    "metadata_field": fname,
                },
                "bids_version": bids_version,
                "schema_version": schema_version,
                "source": _src(
                    "bids-specification-repo",
                    revisions,
                    file=f"src/schema/rules/{rel}",
                    path=f"src/schema/rules/{rel}",
                    line_start=(line or (None, None))[0],
                    line_end=(line or (None, None))[1],
                ),
                "raw_content": {
                    "rule_definition": rule,
                    "field": fname,
                    "field_spec": fspec,
                    "issue_definition": issue,
                    "file": f"src/schema/rules/{rel}",
                    "rule_name": key,
                },
                "facts": {
                    "issue_code": code,
                    "severity": str(issue.get("level") or fspec.get("level") or "error").lower(),
                    "message": message,
                    "selectors": selectors,
                    "checks": [str(c) for c in rule.get("checks") or [] if isinstance(c, str)],
                    "rule_id": _rule_id(rel, key),
                    "related_fields": [fname],
                    "related_suffixes": _selector_tokens(selectors)["suffixes"],
                    "related_paths": _selector_tokens(selectors)["paths"],
                    "legacy_issue_number": None,
                },
                "hints": {"kind": "issue", "field_level": True},
            }
        )
    return out


def _tabular_rule_record(rel, key, raw_rule, rule, parsed, revisions, bids_version, schema_version, line) -> dict:
    columns = _normalize_fields(rule.get("columns"))
    selectors = [str(s) for s in rule.get("selectors") or [] if isinstance(s, str)]
    tokens = _selector_tokens(selectors)
    required = sorted(k for k, v in columns.items() if v.get("level") == "required")
    recommended = sorted(k for k, v in columns.items() if v.get("level") == "recommended")
    rule_id = _rule_id(rel, key)
    return {
        "record_key": f"tabular_rule|{rel}|{key}|{schema_version}",
        "knowledge_type": "TabularRule",
        "knowledge_family": f"rule:{rule_id}",
        "authority": "normative_specification",
        "title": f"Tabular data rule {key}",
        "summary": f"{rule_id}: TSV rule with {len(required)} required and {len(recommended)} recommended column(s)"
        + (f" (suffix: {', '.join(tokens['suffixes'])})" if tokens["suffixes"] else "")
        + ".",
        "scope": {"knowledge_domain": "tabular_rule", "rule_group": _split_check_dir(rel), "rule_name": key},
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": _src(
            "bids-specification-repo",
            revisions,
            file=f"src/schema/rules/{rel}",
            path=f"src/schema/rules/{rel}",
            line_start=(line or (None, None))[0],
            line_end=(line or (None, None))[1],
        ),
        "raw_content": {
            "rule_definition": rule,
            "file": f"src/schema/rules/{rel}",
            "rule_name": key,
        },
        "facts": {
            "rule_id": rule_id,
            "selectors": selectors,
            "columns": _bounded(columns, 400),
            "column_levels": {k: v.get("level") for k, v in columns.items()},
            "required_columns": required,
            "recommended_columns": recommended,
            "additional_columns": rule.get("additional_columns"),
            "initial_columns": rule.get("initial_columns"),
            "index_columns": rule.get("index_columns"),
            "related_suffixes": tokens["suffixes"],
            "related_paths": tokens["paths"],
        },
        "hints": {"kind": "tabular_rule"},
    }


def _extract_ordered_list_rule(rel, data, parsed, revisions, bids_version, schema_version) -> list[dict]:
    """Ordered lists in rules/ (entity order, common principles order, ...)."""
    stem = rel.replace(".yaml", "").replace(".yml", "")
    if stem == "entities":
        title = "Entity order in BIDS filenames"
        summary = (
            "BIDS file names are built from hyphenated 'entities' in a defined order: "
            + ", ".join(str(x) for x in data[:12])
            + (" …" if len(data) > 12 else "")
        )
        kind_desc = "The specification defines the order in which entity key-value pairs must appear within filenames."
    elif stem == "common_principles":
        title = "Order of the BIDS common principles"
        summary = "Presentation order of common principles: " + ", ".join(str(x) for x in data)
        kind_desc = "Ordering rule for the common principles section of the specification."
    elif stem == "metaentities":
        title = "Meta-entities"
        summary = "Meta-entities/placeholders: " + ", ".join(str(x) for x in data)
        kind_desc = "Meta-entities used in derivative filename templates."
    else:
        title = f"Ordered list rule {stem}"
        summary = f"Ordered list definition in rules/{rel}."
        kind_desc = "Ordered list rule."
    return [
        {
            "record_key": f"ordered_rule|{rel}|{schema_version}",
            "knowledge_type": "Concept",
            "knowledge_family": f"concept:{stem}",
            "authority": "normative_specification",
            "title": title,
            "summary": summary,
            "scope": {"knowledge_domain": "ordering_rule", "source_file": f"src/schema/rules/{rel}"},
            "bids_version": bids_version,
            "schema_version": schema_version,
            "source": _src(
                "bids-specification-repo",
                revisions,
                file=f"src/schema/rules/{rel}",
                path=f"src/schema/rules/{rel}",
            ),
            "raw_content": {"ordered_list": data, "description": kind_desc, "file": f"src/schema/rules/{rel}"},
            "facts": {"ordered_items": [str(x) for x in data]},
            "hints": {"kind": "ordered_list"},
        }
    ]


# --- rules/modalities.yaml and rules/directories.yaml handled separately ---

def extract_modality_rules(parsed: dict, revisions: dict) -> list[dict]:
    records = []
    bids_version = parsed.get("bids_version")
    schema_version = parsed.get("schema_version")
    data = parsed["rules"].get("modalities.yaml")
    if isinstance(data, dict):
        for modality, spec in sorted(data.items()):
            dts = [str(d) for d in (spec or {}).get("datatypes", [])] if isinstance(spec, dict) else []
            records.append(
                {
                    "record_key": f"modality_rule|{modality}|{schema_version}",
                    "knowledge_type": "Concept",
                    "knowledge_family": f"concept:modality:{modality}",
                    "authority": "normative_specification",
                    "title": f"Modality {modality}",
                    "summary": f"Modality '{modality}' groups datatypes: {', '.join(dts)}.",
                    "scope": {"knowledge_domain": "modality", "source_file": "src/schema/rules/modalities.yaml"},
                    "bids_version": bids_version,
                    "schema_version": schema_version,
                    "source": _src(
                        "bids-specification-repo",
                        revisions,
                        file="src/schema/rules/modalities.yaml",
                        path="src/schema/rules/modalities.yaml",
                    ),
                    "raw_content": {"definition": spec, "file": "src/schema/rules/modalities.yaml"},
                    "facts": {"modality": modality, "datatypes": dts, "rule_id": f"rules.modalities.{modality}"},
                    "hints": {"kind": "modality"},
                }
            )
    return records


# ===========================================================================
# B. Schema objects -> Concept records
# ===========================================================================

CONCEPT_KINDS = {
    "metadata.yaml": "metadata_field",
    "columns.yaml": "tabular_column",
    "entities.yaml": "entity",
    "suffixes.yaml": "suffix",
    "datatypes.yaml": "datatype",
    "extensions.yaml": "file_extension",
    "formats.yaml": "file_format",
    "modalities.yaml": "modality",
    "metaentities.yaml": "metaentity",
    "common_principles.yaml": "common_principle",
    "files.yaml": "dataset_file",
    "enums.yaml": "enum_value",
}


def extract_objects(parsed: dict, revisions: dict) -> list[dict]:
    records: list[dict] = []
    bids_version = parsed.get("bids_version")
    schema_version = parsed.get("schema_version")
    for fname in sorted(parsed["objects"]):
        data = parsed["objects"][fname]
        kind = CONCEPT_KINDS.get(fname, "schema_object")
        if not isinstance(data, dict):
            continue
        for key in sorted(data):
            obj = data[key]
            if not isinstance(obj, dict):
                obj = {"value": obj}
            display = obj.get("display_name") or key
            desc = util.normalize_ws(str(obj.get("description") or ""))
            facts: dict = {"concept_kind": kind, "object_key": key, "display_name": display}
            # type-specific facts
            if kind == "metadata_field":
                facts["value_type"] = obj.get("type")
                facts["format"] = obj.get("format")
                facts["json_key"] = obj.get("name") or key
                for c in ("minimum", "maximum", "minItems", "maxItems", "enum", "units", "pattern"):
                    if c in obj:
                        facts[c] = obj[c]
            elif kind == "entity":
                facts["entity_label"] = obj.get("name") or key
                facts["value_type"] = obj.get("type")
                facts["format"] = obj.get("format")
            elif kind == "tabular_column":
                facts["value_type"] = obj.get("type")
                facts["format"] = obj.get("format")
                facts["column_name"] = obj.get("name") or key
            elif kind == "dataset_file":
                facts["file_type"] = obj.get("file_type")
                facts["path_hint"] = obj.get("path") or obj.get("stem")
            records.append(
                {
                    "record_key": f"object|{fname}|{key}|{schema_version}",
                    "knowledge_type": "Concept",
                    "knowledge_family": f"concept:{kind}:{key}",
                    "authority": "normative_specification",
                    "title": f"{kind.replace('_', ' ')}: {key}",
                    "summary": _first_sentence(desc) or f"{kind} '{key}' defined in src/schema/objects/{fname}.",
                    "scope": {"knowledge_domain": kind, "source_file": f"src/schema/objects/{fname}"},
                    "bids_version": bids_version,
                    "schema_version": schema_version,
                    "source": _src(
                        "bids-specification-repo",
                        revisions,
                        file=f"src/schema/objects/{fname}",
                        path=f"src/schema/objects/{fname}",
                    ),
                    "raw_content": {"definition": obj, "file": f"src/schema/objects/{fname}"},
                    "facts": facts,
                    "hints": {"kind": "object_concept", "description": desc},
                }
            )
    return records


# ===========================================================================
# C. Schema meta/ -> context, associations, versions, templates
# ===========================================================================

def extract_meta(parsed: dict, revisions: dict) -> list[dict]:
    records: list[dict] = []
    bids_version = parsed.get("bids_version")
    schema_version = parsed.get("schema_version")
    meta = parsed.get("meta", {})

    # --- context namespaces (validation context object) ---
    ctx = meta.get("context.yaml")
    if isinstance(ctx, dict):
        props = ctx.get("properties") or {}
        if isinstance(props, dict):
            for name, spec in sorted(props.items()):
                if not isinstance(spec, dict):
                    continue
                subprops = {}
                inner = spec.get("properties")
                if isinstance(inner, dict):
                    for sname, sspec in sorted(inner.items()):
                        if isinstance(sspec, dict):
                            subprops[sname] = {
                                "description": sspec.get("description"),
                                "type": sspec.get("type"),
                                "required": sspec.get("required"),
                            }
                records.append(
                    {
                        "record_key": f"context|{name}",
                        "knowledge_type": "Concept",
                        "knowledge_family": f"concept:context:{name}",
                        "authority": "normative_specification",
                        "title": f"Validation context namespace: {name}",
                        "summary": util.truncate(str(spec.get("description") or f"context.{name}"), 240),
                        "scope": {"knowledge_domain": "validation_context"},
                        "bids_version": bids_version,
                        "schema_version": schema_version,
                        "source": _src(
                            "bids-specification-repo",
                            revisions,
                            file="src/schema/meta/context.yaml",
                            path="src/schema/meta/context.yaml",
                        ),
                        "raw_content": {"schema_fragment": spec, "subproperties": _bounded(subprops)},
                        "facts": {
                            "context_namespace": name,
                            "description": spec.get("description"),
                            "value_type": spec.get("type"),
                            "required": spec.get("required"),
                            "subproperties": subprops,
                        },
                        "hints": {"kind": "context"},
                    }
                )

    # --- cross-file associations ---
    assoc = meta.get("associations.yaml")
    if isinstance(assoc, dict):
        for name, spec in sorted(assoc.items()):
            if not isinstance(spec, dict):
                continue
            selectors = [str(s) for s in spec.get("selectors") or [] if isinstance(s, str)]
            records.append(
                {
                    "record_key": f"association|{name}",
                    "knowledge_type": "Relationship",
                    "knowledge_family": f"relationship:association:{name}",
                    "authority": "normative_specification",
                    "title": f"File association: {name}",
                    "summary": f"Association '{name}' links a file matching "
                    + ", ".join(selectors[:3])
                    + f" to {spec.get('target')}.",
                    "scope": {"knowledge_domain": "cross_file_association"},
                    "bids_version": bids_version,
                    "schema_version": schema_version,
                    "source": _src(
                        "bids-specification-repo",
                        revisions,
                        file="src/schema/meta/associations.yaml",
                        path="src/schema/meta/associations.yaml",
                    ),
                    "raw_content": {"definition": spec, "file": "src/schema/meta/associations.yaml"},
                    "facts": {
                        "relationship_type": "file_association",
                        "association_name": name,
                        "selectors": selectors,
                        "target": spec.get("target"),
                        "inheritable": spec.get("inherit"),
                    },
                    "hints": {"kind": "association"},
                }
            )

    # --- released BIDS versions ---
    versions = meta.get("versions.yaml")
    if isinstance(versions, list):
        records.append(
            {
                "record_key": "released_bids_versions",
                "knowledge_type": "Version",
                "knowledge_family": "version:released_bids",
                "authority": "normative_specification",
                "title": "Released BIDS versions",
                "summary": f"{len(versions)} released BIDS versions as of the next schema release; latest: {versions[0]}.",
                "scope": {"knowledge_domain": "version_history"},
                "bids_version": None,
                "schema_version": schema_version,
                "source": _src(
                    "bids-specification-repo",
                    revisions,
                    file="src/schema/meta/versions.yaml",
                    path="src/schema/meta/versions.yaml",
                ),
                "raw_content": {"released_versions": [str(v) for v in versions]},
                "facts": {"released_bids_versions": [str(v) for v in versions], "latest_released": str(versions[0]) if versions else None},
                "hints": {"kind": "version_list"},
            }
        )

    # --- templates ---
    templates = meta.get("templates.yaml")
    if isinstance(templates, dict):
        records.append(
            {
                "record_key": "schema_templates",
                "knowledge_type": "Concept",
                "knowledge_family": "concept:schema_templates",
                "authority": "normative_specification",
                "title": "Schema entity templates ($ref sets)",
                "summary": "Reusable entity requirement sets referenced by filename rules via $ref "
                "(e.g. meta.templates.raw.base.entities = subject required, session optional).",
                "scope": {"knowledge_domain": "schema_mechanism"},
                "bids_version": bids_version,
                "schema_version": schema_version,
                "source": _src(
                    "bids-specification-repo",
                    revisions,
                    file="src/schema/meta/templates.yaml",
                    path="src/schema/meta/templates.yaml",
                ),
                "raw_content": {"templates": templates},
                "facts": {"template_names": sorted(templates.keys()) if isinstance(templates, dict) else []},
                "hints": {"kind": "templates"},
            }
        )

    # --- BIDS_VERSION / SCHEMA_VERSION ---
    records.append(
        {
            "record_key": f"dev_versions|{bids_version}|{schema_version}",
            "knowledge_type": "Version",
            "knowledge_family": "version:development_spec",
            "authority": "normative_specification",
            "title": f"Development specification version {bids_version} (schema {schema_version})",
            "summary": f"The cloned specification repository reports BIDS {bids_version} and schema {schema_version} "
            "on its default branch (development content, not a released specification).",
            "scope": {"knowledge_domain": "version_history", "content_stage": "development"},
            "bids_version": bids_version,
            "schema_version": schema_version,
            "source": _src(
                "bids-specification-repo",
                revisions,
                file="src/schema/BIDS_VERSION",
                path="src/schema/BIDS_VERSION",
            ),
            "raw_content": {
                "bids_version": bids_version,
                "schema_version": schema_version,
                "files": ["src/schema/BIDS_VERSION", "src/schema/SCHEMA_VERSION", "CITATION.cff"],
            },
            "facts": {"bids_version": bids_version, "schema_version": schema_version, "content_stage": "development"},
            "hints": {"kind": "version_dev"},
        }
    )
    return records


# ===========================================================================
# D. Changelog -> Version records
# ===========================================================================

_CHANGE_HEAD = re.compile(r"^##\s+\[?v?([0-9]+\.[0-9]+\.[0-9]+[^\]\s]*)\]?\s*(?:[-–—(]\s*([^)\n]+))?")


def extract_changelog(revisions: dict) -> list[dict]:
    path = os.path.join(config.SOURCES_DIR, "bids-specification", "src", "CHANGES.md")
    if not os.path.exists(path):
        return []
    text, _, _ = util.read_text(path)
    lines = text.splitlines()
    records = []
    entries = []
    cur = None
    for i, line in enumerate(lines, 1):
        m = _CHANGE_HEAD.match(line)
        if m:
            if cur:
                entries.append(cur)
            cur = {"version": m.group(1), "date": (m.group(2) or "").strip(), "line": i, "lines": []}
        elif cur is not None:
            cur["lines"].append(line)
    if cur:
        entries.append(cur)
    for e in entries:
        body = util.normalize_ws("\n".join(e["lines"]))
        records.append(
            {
                "record_key": f"changelog|{e['version']}",
                "knowledge_type": "Version",
                "knowledge_family": f"version:bids:{e['version']}",
                "authority": "normative_specification",
                "title": f"BIDS specification {e['version']}",
                "summary": (f"Release {e['version']}" + (f" ({e['date']})" if e["date"] else "") + "."),
                "scope": {"knowledge_domain": "version_history", "content_stage": "release"},
                "bids_version": e["version"],
                "schema_version": None,
                "source": _src(
                    "bids-specification-repo",
                    revisions,
                    file="src/CHANGES.md",
                    path="src/CHANGES.md",
                    line_start=e["line"],
                    line_end=e["line"],
                ),
                "raw_content": {"changelog_excerpt": body[:4000]},
                "facts": {"release_version": e["version"], "release_date": e["date"] or None},
                "hints": {"kind": "release"},
            }
        )
    return records


# ===========================================================================
# E. Published documentation pages (source A)
# ===========================================================================

TROUBLE_HEADINGS = re.compile(
    r"(troubleshoot|pitfall|faq|frequently asked|common problems|debug)", re.I
)


def extract_docs(pages_report: dict, revisions: dict) -> tuple[list[dict], list[dict]]:
    """Parse every acquired documentation page into section records.

    Returns (records, page_index) where page_index maps
    (docs_path, heading_slug) -> record, for mirroring by stage 4b.
    """
    records: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    failures = []
    for page in pages_report.get("pages", []):
        url = page["url"]
        if page.get("status") != 200 and page.get("provenance") != "cache":
            failures.append(page)
            continue
        html_path, _meta = acquire_cache_paths(url)
        if not html_path or not os.path.exists(html_path):
            failures.append({"url": url, "error": "cache file missing"})
            continue
        payload = open(html_path, "rb").read()
        try:
            parsed_page = parse.parse_docs_page(payload, url)
        except Exception as exc:  # noqa: BLE001 - record and continue
            failures.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rel_md = parse.docs_page_to_md_path(url) or url
        if rel_md.endswith("src/CHANGES.md"):
            # Changelog sections become Version records from the repository
            # source instead; the page itself is still inventoried.
            continue
        for sec in parsed_page["sections"]:
            rec = _docs_section_record(url, rel_md, parsed_page, sec, revisions)
            records.append(rec)
            key = (rel_md, util.slugify(sec.get("heading") or "", 120))
            index.setdefault(key, rec)
    return records, {"records": records, "index": index, "failures": failures}


def acquire_cache_paths(url: str):
    from . import acquire

    html_path, _meta = acquire._cache_paths(url)
    return (html_path if os.path.exists(html_path) else None), html_path


def _docs_section_record(url, rel_md, page, sec, revisions) -> dict:
    heading = sec.get("heading") or page.get("title") or url
    anchor = sec.get("anchor") or ""
    text = sec.get("text") or ""
    truncated = len(text) > MAX_TEXT
    tables = sec.get("tables") or []
    ktype = "Troubleshooting" if TROUBLE_HEADINGS.search(heading) else "Concept"
    scope_domain = "specification_documentation"
    summary = _first_sentence(text) or f"Section '{heading}' of the published BIDS specification documentation."
    source_path = url.replace(config.SOURCES["bids-specification-docs"]["url"], "")
    return {
        "record_key": f"docs_section|{source_path}|{anchor}|{heading}",
        "knowledge_type": ktype,
        "knowledge_family": f"doc:{source_path.split('/')[-1].replace('.html', '')}",
        "authority": "published_documentation",
        "title": heading,
        "summary": summary,
        "scope": {"knowledge_domain": scope_domain, "page": page.get("title"), "page_path": source_path},
        "bids_version": page.get("bids_version"),
        "schema_version": None,
        "source": _src(
            "bids-specification-docs",
            revisions,
            url=url,
            path=source_path,
            section=anchor or None,
        ),
        "raw_content": {
            "page_title": page.get("title"),
            "page_url": url,
            "section_heading": heading,
            "text": text[:MAX_TEXT],
            "text_truncated": truncated,
            "tables": [t[:60] for t in tables[:25]],
            "code_blocks": [c[:4000] for c in (sec.get("code_blocks") or [])[:15]],
            "generator": page.get("generator"),
        },
        "facts": {
            "page_path": source_path,
            "section_anchor": anchor or None,
            "table_count": len(tables),
            "normative_text": bool(re.search(r"\bMUST\b|\bSHALL\b|\bMUST NOT\b", text)),
        },
        "hints": {"kind": "doc_section", "text": text[:MAX_TEXT]},
    }


# ===========================================================================
# F. Specification markdown (development source) with doc mirroring
# ===========================================================================

def extract_spec_markdown(revisions: dict, docs_index: dict) -> tuple[list[dict], dict]:
    """Extract dev-branch markdown sections, merging exact mirrors of the
    published documentation (same content, different version/authority)."""
    from difflib import SequenceMatcher

    root = os.path.join(config.SOURCES_DIR, "bids-specification")
    records: list[dict] = []
    stats = {"mirrored": 0, "dev_only": 0, "changed_in_dev": 0, "skipped": 0}
    parsed_spec = parse.parse_spec_schema(root)
    bids_version = parsed_spec.get("bids_version")
    schema_version = parsed_spec.get("schema_version")

    md_files = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "src")):
        dirnames[:] = sorted(d for d in dirnames if d not in ("schema", "images", "css", "js"))
        for fn in sorted(filenames):
            if fn.endswith(".md"):
                md_files.append(os.path.join(dirpath, fn))

    for full in sorted(md_files):
        rel = os.path.relpath(full, root).replace("\\", "/")
        if rel.endswith("src/CHANGES.md"):
            continue
        docs_url_rel = rel[:-3] + ".html" if rel.endswith(".md") else None
        docs_key_prefix = rel  # e.g. src/modality-agnostic-files/events.md
        try:
            sections = parse.parse_md_sections(full)
        except Exception:  # noqa: BLE001
            stats["skipped"] += 1
            continue
        for sec in sections:
            body = sec.get("text") or ""
            if not body and not sec.get("heading"):
                continue
            heading = sec.get("heading") or rel
            slug = util.slugify(heading, 120)
            mirror_rec = docs_index.get("index", {}).get((docs_key_prefix, slug))
            if mirror_rec is None:
                mirror_rec = _find_mirror(docs_index, docs_key_prefix, heading, body, SequenceMatcher)
            if mirror_rec is not None:
                # Same heading + near-identical body: the published docs already
                # carry this knowledge.  Record the dev mirror provenance there.
                if "dev_mirrors" not in mirror_rec["raw_content"]:
                    mirror_rec["raw_content"]["dev_mirrors"] = []
                mirror_rec["raw_content"]["dev_mirrors"].append(
                    {
                        "repository": config.SOURCES["bids-specification-repo"]["url"],
                        "path": rel,
                        "revision": revisions.get("bids-specification-repo", {}).get("commit"),
                        "line_start": sec.get("line_start"),
                        "line_end": sec.get("line_end"),
                        "bids_version": bids_version,
                        "schema_version": schema_version,
                    }
                )
                stats["mirrored"] += 1
                continue
            # Materially different dev content: emit a development record.
            rec = {
                "record_key": f"spec_md|{rel}|{sec.get('line_start')}|{slug}",
                "knowledge_type": "Concept",
                "knowledge_family": f"doc_dev:{rel.split('/')[-1].replace('.md', '')}",
                "authority": "normative_specification",
                "title": heading,
                "summary": _first_sentence(body) or f"Development source section '{heading}' of {rel}.",
                "scope": {
                    "knowledge_domain": "specification_documentation",
                    "content_stage": "development",
                    "source_file": rel,
                },
                "bids_version": bids_version,
                "schema_version": schema_version,
                "source": _src(
                    "bids-specification-repo",
                    revisions,
                    file=rel,
                    path=rel,
                    line_start=sec.get("line_start"),
                    line_end=sec.get("line_end"),
                    section=sec.get("anchor") or None,
                ),
                "raw_content": {
                    "text": body[:MAX_TEXT],
                    "text_truncated": len(body) > MAX_TEXT,
                    "heading": heading,
                    "file": rel,
                },
                "facts": {"content_stage": "development", "page_path": rel},
                "hints": {"kind": "dev_md_section", "text": body[:MAX_TEXT]},
            }
            records.append(rec)
            stats["dev_only"] += 1
    return records, stats


def _find_mirror(docs_index, docs_key_prefix, heading, body, SequenceMatcher):
    slug = util.slugify(heading, 120)
    rec = docs_index.get("index", {}).get((docs_key_prefix, slug))
    if rec is None:
        return None
    dev_text = util.normalize_ws(body)[:4000]
    doc_text = util.normalize_ws(rec.get("hints", {}).get("text", ""))[:4000]
    if not dev_text or not doc_text:
        return None
    ratio = SequenceMatcher(None, dev_text, doc_text).ratio()
    if ratio >= 0.97:
        rec.setdefault("_mirror_ratio", ratio)
        return rec
    return None


# ===========================================================================
# G. Validator source (implementation behavior)
# ===========================================================================

def extract_validator(parsed_issues, code_sites, issue_model, revisions, spec_issue_codes) -> list[dict]:
    records: list[dict] = []
    rev = revisions.get("bids-validator", {}).get("commit")

    for item in parsed_issues:
        code = item["code"]
        sites = code_sites.get(code, [])
        prod_sites = [s for s in sites if not s["test"]]
        test_sites = [s for s in sites if s["test"]]
        spec_defined = code in spec_issue_codes
        records.append(
            {
                "record_key": f"validator_issue|{code}",
                "knowledge_type": "ValidationIssue",
                "knowledge_family": f"issue:{code}",
                "authority": "validator_implementation",
                "title": code,
                "summary": item["reason"] or f"Validator issue code {code} (severity {item['severity']}).",
                "scope": {
                    "knowledge_domain": "validation_issue",
                    "rule_group": "validator_issue_catalogue",
                    "implementation_only": not spec_defined,
                    "source_file": "src/issues/list.ts",
                },
                "bids_version": None,
                "schema_version": None,
                "source": _src(
                    "bids-validator",
                    revisions,
                    file="src/issues/list.ts",
                    path="src/issues/list.ts",
                    line_start=item["line"],
                    line_end=item["line"],
                ),
                "raw_content": {
                    "issue_definition": {
                        "code": code,
                        "severity": item["severity"],
                        "reason": item["reason"],
                    },
                    "file": "src/issues/list.ts",
                    "raise_sites": prod_sites[:40],
                    "test_references": test_sites[:20],
                    "defined_in_spec_schema": spec_defined,
                },
                "facts": {
                    "issue_code": code,
                    "severity": (item["severity"] or "").lower() or None,
                    "message": util.normalize_ws(item["reason"]),
                    "implementation_reason": util.normalize_ws(item["reason"]),
                    "selectors": [],
                    "checks": [],
                    "rule_id": None,
                    "legacy_issue_number": None,
                    "raise_site_files": sorted({s["file"] for s in prod_sites})[:30],
                    "raise_site_count": len(prod_sites),
                    "defined_in_spec_schema": spec_defined,
                },
                "hints": {"kind": "issue", "implementation": True},
            }
        )

    # Issue codes raised in source but absent from the catalogue (implementation
    # detail; severity/message not centrally declared).
    for code, sites in sorted(code_sites.items()):
        if code in parsed_issue_codes_set(parsed_issues):
            continue
        prod = [s for s in sites if not s["test"]]
        if not prod:
            continue
        records.append(
            {
                "record_key": f"validator_raise_site|{code}",
                "knowledge_type": "ValidationIssue",
                "knowledge_family": f"issue:{code}",
                "authority": "validator_implementation",
                "title": code,
                "summary": f"Issue code {code} is raised by the validator implementation at "
                f"{prod[0]['file']}:{prod[0]['line']} but has no central definition in src/issues/list.ts "
                "(severity/message are supplied at the raise site or by schema rules).",
                "scope": {
                    "knowledge_domain": "validation_issue",
                    "rule_group": "validator_raise_sites",
                    "implementation_only": True,
                    "catalogue_entry": False,
                },
                "bids_version": None,
                "schema_version": None,
                "source": _src(
                    "bids-validator",
                    revisions,
                    file=prod[0]["file"],
                    path=prod[0]["file"],
                    line_start=prod[0]["line"],
                    line_end=prod[0]["line"],
                ),
                "raw_content": {
                    "issue_definition": {
                        "code": code,
                        "severity": None,
                        "message": None,
                        "origin": "raise_site_only",
                    },
                    "raise_sites": prod[:40],
                    "test_references": [s for s in sites if s["test"]][:20],
                    "catalogue_entry": False,
                },
                "facts": {
                    "issue_code": code,
                    "severity": None,
                    "message": None,
                    "selectors": [],
                    "checks": [],
                    "rule_id": None,
                    "legacy_issue_number": None,
                    "raise_site_files": sorted({s["file"] for s in prod})[:30],
                    "raise_site_count": len(prod),
                    "defined_in_spec_schema": code in spec_issue_codes,
                    "severity_unknown": True,
                },
                "hints": {"kind": "issue", "implementation": True, "no_catalogue": True},
            }
        )
    return records


def parsed_issue_codes_set(parsed_issues) -> set[str]:
    return {i["code"] for i in parsed_issues}


def extract_validator_docs(revisions: dict) -> list[dict]:
    """Markdown documentation inside the validator repository."""
    root = os.path.join(config.SOURCES_DIR, "bids-validator")
    targets = [
        ("docs/index.md", "Concept"),
        ("docs/user_guide/issues.md", "Concept"),
        ("docs/user_guide/faq.md", "Troubleshooting"),
        ("docs/user_guide/command-line.md", "Concept"),
        ("docs/user_guide/web.md", "Concept"),
        ("docs/validation-model/index.md", "Concept"),
        ("docs/validation-model/context.md", "Concept"),
        ("docs/validation-model/inheritance-principle.md", "Concept"),
        ("README.md", "Troubleshooting"),
    ]
    records: list[dict] = []
    for rel, default_kind in targets:
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(full):
            continue
        for sec in parse.parse_md_sections(full):
            body = sec.get("text") or ""
            heading = sec.get("heading") or rel
            if not body and sec.get("level") == 0:
                continue
            ktype = "Troubleshooting" if TROUBLE_HEADINGS.search(heading) else default_kind
            if rel.endswith("faq.md"):
                ktype = "Troubleshooting"
            records.append(
                {
                    "record_key": f"validator_doc|{rel}|{sec.get('line_start')}|{sec.get('anchor')}",
                    "knowledge_type": ktype,
                    "knowledge_family": f"validator_doc:{rel.split('/')[-1].replace('.md', '')}",
                    "authority": "validator_implementation",
                    "title": heading,
                    "summary": _first_sentence(body) or f"Validator documentation section '{heading}'.",
                    "scope": {
                        "knowledge_domain": "validator_documentation",
                        "source_file": rel,
                        "content_stage": "development",
                    },
                    "bids_version": None,
                    "schema_version": None,
                    "source": _src(
                        "bids-validator",
                        revisions,
                        file=rel,
                        path=rel,
                        line_start=sec.get("line_start"),
                        line_end=sec.get("line_end"),
                        section=sec.get("anchor") or None,
                    ),
                    "raw_content": {
                        "text": body[:MAX_TEXT],
                        "text_truncated": len(body) > MAX_TEXT,
                        "heading": heading,
                        "file": rel,
                    },
                    "facts": {},
                    "hints": {"kind": "validator_doc", "text": body[:MAX_TEXT], "issue_model": rel.endswith("issues.md")},
                }
            )
    return records


def extract_validator_version(revisions: dict) -> list[dict]:
    root = os.path.join(config.SOURCES_DIR, "bids-validator")
    out = []
    changelog = os.path.join(root, "CHANGELOG.md")
    version = None
    date = None
    if os.path.exists(changelog):
        text, _, _ = util.read_text(changelog)
        m = re.search(r"(?m)^#\s+\"?([0-9]+\.[0-9]+\.[0-9]+)\"?\s+[-–—]\s+([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
        if m:
            version, date = m.group(1), m.group(2)
    rev = revisions.get("bids-validator", {}).get("commit")
    out.append(
        {
            "record_key": f"validator_version|{version}|{rev}",
            "knowledge_type": "Version",
            "knowledge_family": "version:validator",
            "authority": "validator_implementation",
            "title": f"bids-validator release {version or 'unknown'}",
            "summary": f"Validator changelog head: {version or 'unknown'}"
            + (f" ({date})" if date else "")
            + f"; cloned revision {rev}.",
            "scope": {"knowledge_domain": "version_history", "content_stage": "implementation"},
            "bids_version": None,
            "schema_version": None,
            "source": _src("bids-validator", revisions, file="CHANGELOG.md", path="CHANGELOG.md"),
            "raw_content": {"latest_release": version, "release_date": date, "commit": rev},
            "facts": {"validator_release": version, "release_date": date, "commit": rev},
            "hints": {"kind": "version_validator"},
        }
    )
    # Schema selection behavior (implementation fact worth preserving)
    loadschema = os.path.join(root, "src", "setup", "loadSchema.ts")
    if os.path.exists(loadschema):
        text, _, _ = util.read_text(loadschema)
        out.append(
            {
                "record_key": "validator_schema_selection",
                "knowledge_type": "Concept",
                "knowledge_family": "concept:validator_schema_selection",
                "authority": "validator_implementation",
                "title": "How the validator selects its schema",
                "summary": "The validator uses the bundled @bids/schema package by default, or fetches "
                "https://bids-specification.readthedocs.io/en/<version>/schema.json when a version like "
                "vX.Y.Z, stable or latest is requested (or BIDS_SCHEMA env var is set).",
                "scope": {"knowledge_domain": "validator_behavior"},
                "bids_version": None,
                "schema_version": None,
                "source": _src(
                    "bids-validator",
                    revisions,
                    file="src/setup/loadSchema.ts",
                    path="src/setup/loadSchema.ts",
                    line_start=1,
                    line_end=43,
                ),
                "raw_content": {"file": "src/setup/loadSchema.ts"},
                "facts": {"schema_sources": ["bundled @bids/schema", "BIDS_SCHEMA env", "RTD schema.json by version"]},
                "hints": {"kind": "validator_schema"},
            }
        )
    # Issue model fields from docs (documented issue JSON structure)
    from . import parse as _p

    model = _p.parse_validator_issue_model(root)
    if model.get("issue_fields"):
        out.append(
            {
                "record_key": "validator_issue_model",
                "knowledge_type": "Concept",
                "knowledge_family": "concept:validator_issue_model",
                "authority": "validator_implementation",
                "title": "Validator issue object fields",
                "summary": "Every validator issue carries code, location and severity; optional fields are "
                + ", ".join(f["field"] for f in model["issue_fields"] if f["field"] not in ("code", "location", "severity"))
                + ".",
                "scope": {"knowledge_domain": "validator_behavior", "source_file": "docs/user_guide/issues.md"},
                "bids_version": None,
                "schema_version": None,
                "source": _src(
                    "bids-validator",
                    revisions,
                    file="docs/user_guide/issues.md",
                    path="docs/user_guide/issues.md",
                ),
                "raw_content": {"issue_fields": model["issue_fields"]},
                "facts": {"issue_fields": model["issue_fields"]},
                "hints": {"kind": "issue_model"},
            }
        )
    return out


def extract_validator_fixtures(revisions: dict) -> list[dict]:
    """Test fixture datasets that reproduce specific validation situations."""
    root = os.path.join(config.SOURCES_DIR, "bids-validator", "tests", "data")
    if not os.path.isdir(root):
        return []
    records = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        files = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = sorted(dirnames)
            for fn in sorted(filenames):
                files.append(os.path.relpath(os.path.join(dirpath, fn), d).replace("\\", "/"))
                if len(files) >= 60:
                    break
            if len(files) >= 60:
                break
        readme = ""
        for cand in ("README", "README.md", "readme.md"):
            p = os.path.join(d, cand)
            if os.path.exists(p):
                text, _, _ = util.read_text(p)
                readme = util.truncate(text, 500)
                break
        records.append(
            {
                "record_key": f"validator_fixture|{name}",
                "knowledge_type": "Example",
                "knowledge_family": f"example:validator_fixture:{name}",
                "authority": "validator_implementation",
                "title": f"Validator test fixture: {name}",
                "summary": readme or f"Test fixture dataset '{name}' used by the validator test suite "
                "(its presence indicates a tested validation scenario, not a valid dataset).",
                "scope": {
                    "knowledge_domain": "validator_test_fixture",
                    "fixture": name,
                    "note": "test fixture; may intentionally be invalid",
                },
                "bids_version": None,
                "schema_version": None,
                "source": _src(
                    "bids-validator",
                    revisions,
                    file=f"tests/data/{name}",
                    path=f"tests/data/{name}",
                ),
                "raw_content": {"readme": readme, "sample_files": files[:60]},
                "facts": {"fixture_name": name, "file_sample": files[:30]},
                "hints": {"kind": "fixture"},
            }
        )
    return records


# ===========================================================================
# H. bids-examples
# ===========================================================================

def extract_examples(scan: dict, revisions: dict) -> list[dict]:
    records: list[dict] = []
    listing_rows = {}
    if scan.get("listing"):
        for row in scan["listing"]["rows"]:
            listing_rows[row.get("name", "")] = row
    rev = revisions.get("bids-examples", {}).get("commit")

    for ds in scan["datasets"]:
        name = ds["name"]
        row = listing_rows.get(name, {})
        dd = ds.get("dataset_description") or {}
        description = row.get("description") or (dd.get("Name") if isinstance(dd, dict) else None) or ""
        validation_note = _validation_status_note(ds)
        facts = {
            "dataset_name": name,
            "datatypes": ds.get("datatypes", {}),
            "suffixes": ds.get("suffixes", {}),
            "subject_count": ds.get("subject_count"),
            "session_count": ds.get("session_count"),
            "entities_by_suffix": ds.get("entities_by_suffix", {}),
            "participants_columns": ds.get("participants_columns"),
            "bids_version_in_dataset": dd.get("BIDSVersion") if isinstance(dd, dict) else None,
            "dataset_type": dd.get("DatasetType") if isinstance(dd, dict) else None,
            "derivatives": [d.get("name") for d in ds.get("derivatives", [])],
            "skip_validation_marker": ds.get("skip_validation"),
            "validator_config": ds.get("validator_config"),
            "validation_status_verified_by_this_pipeline": False,
        }
        records.append(
            {
                "record_key": f"example_dataset|{name}",
                "knowledge_type": "Example",
                "knowledge_family": f"example:dataset:{name}",
                "authority": "dataset_example",
                "title": f"Example dataset: {name}",
                "summary": util.truncate(
                    description or f"BIDS example dataset '{name}' with datatypes: "
                    + ", ".join(sorted(ds.get("datatypes", {}))[:8]),
                    300,
                ),
                "scope": {
                    "knowledge_domain": "dataset_example",
                    "dataset": name,
                    "limitations": validation_note,
                },
                "bids_version": dd.get("BIDSVersion") if isinstance(dd, dict) else None,
                "schema_version": None,
                "source": _src(
                    "bids-examples",
                    revisions,
                    repository=config.SOURCES["bids-examples"]["url"],
                    file=f"{name}/",
                    path=name,
                ),
                "raw_content": {
                    "dataset_description": _bounded(dd, 60) if isinstance(dd, dict) else None,
                    "dataset_description_error": ds.get("dataset_description_error"),
                    "root_json_keys": dict(list(ds.get("root_json_keys", {}).items())[:25]),
                    "tsv_headers": dict(list(ds.get("tsv_headers", {}).items())[:25]),
                    "file_sample": ds.get("files", [])[:80],
                    "file_count_estimate": ds.get("file_count_estimate"),
                    "truncated_scan": ds.get("truncated"),
                    "derivative_datasets": [
                        {"name": d.get("name"), "dataset_description": d.get("dataset_description")}
                        for d in ds.get("derivatives", [])
                    ],
                    "listing_row": row or None,
                    "parsing_problems": ds.get("parsing_problems", [])[:10],
                },
                "facts": facts,
                "hints": {"kind": "example_dataset"},
            }
        )

    # Repository-level validation harness (honesty about validation status)
    harness = {
        "record_key": "examples_validation_harness",
        "knowledge_type": "Concept",
        "knowledge_family": "concept:examples_validation_harness",
        "authority": "dataset_example",
        "title": "Validation harness of the bids-examples repository",
        "summary": "bids-examples validates each dataset with the bids-validator via run_tests.sh, "
        "ignoring EMPTY_FILE by default (default-config.json) and skipping NIfTI headers except for "
        "'synthetic'; datasets with .SKIP_VALIDATION are not validated.",
        "scope": {
            "knowledge_domain": "dataset_example",
            "note": "presence in this repository is not proof of validity under every validator version",
        },
        "bids_version": None,
        "schema_version": None,
        "source": _src("bids-examples", revisions, file="run_tests.sh", path="run_tests.sh"),
        "raw_content": {
            "files": [f for f in scan.get("root_files", [])],
            "default_config": _read_json_file(
                os.path.join(config.SOURCES_DIR, "bids-examples", "default-config.json")
            ),
        },
        "facts": {
            "default_ignore_codes": ["EMPTY_FILE"],
            "nifti_headers_ignored_except": ["synthetic"],
            "skip_marker": ".SKIP_VALIDATION",
        },
        "hints": {"kind": "examples_harness"},
    }
    records.append(harness)
    return records


def _validation_status_note(ds: dict) -> list[str]:
    notes = [
        "Validation status not verified by this pipeline; presence in bids-examples does not "
        "guarantee validity under every validator/schema version."
    ]
    if ds.get("skip_validation"):
        notes.append("Dataset carries .SKIP_VALIDATION: excluded from the repository's validation runs.")
    if ds.get("validator_config"):
        notes.append(f"Custom validator config present: {ds['validator_config']}")
    if ds.get("truncated"):
        notes.append("File scan truncated at the per-dataset file cap.")
    return notes


def _read_json_file(path: str):
    if not os.path.exists(path):
        return None
    try:
        text, _, _ = util.read_text(path)
        return json_loads_safe(text)
    except OSError:
        return None


def json_loads_safe(text: str):
    import json

    try:
        return json.loads(text)
    except ValueError:
        return {"_parse_error": True}


# ===========================================================================
# I. bids-schema repository (historical schemas)
# ===========================================================================

def extract_schema_repo(schema_parsed: dict, revisions: dict) -> list[dict]:
    records: list[dict] = []
    variants: dict[str, dict] = {}  # code -> {message_key: {level,message,snapshots:[]}}

    for name in sorted(schema_parsed.get("versions", {})):
        entry = schema_parsed["versions"][name]
        issue_index = entry.get("issue_index") or {}
        records.append(
            {
                "record_key": f"schema_snapshot|{name}",
                "knowledge_type": "Version",
                "knowledge_family": f"version:schema_snapshot:{name}",
                "authority": "schema_archive",
                "title": f"BIDS schema snapshot {name}",
                "summary": f"Archived schema snapshot '{name}' with BIDS version "
                f"{entry.get('BIDS_VERSION', 'unknown')}, schema version {entry.get('SCHEMA_VERSION', 'unknown')}"
                + (f", last edit {entry['BIDS_VERSION_LASTEDIT']}" if entry.get("BIDS_VERSION_LASTEDIT") else "")
                + f"; {len(issue_index)} issue codes, {entry.get('rule_files', 0)} rule files.",
                "scope": {
                    "knowledge_domain": "version_history",
                    "content_stage": "archived_schema",
                    "snapshot": name,
                },
                "bids_version": entry.get("BIDS_VERSION"),
                "schema_version": entry.get("SCHEMA_VERSION"),
                "source": _src(
                    "bids-schema",
                    revisions,
                    file=f"versions/{name}/",
                    path=f"versions/{name}",
                ),
                "raw_content": {
                    "snapshot": name,
                    "bids_version": entry.get("BIDS_VERSION"),
                    "schema_version": entry.get("SCHEMA_VERSION"),
                    "bids_version_lastedit": entry.get("BIDS_VERSION_LASTEDIT"),
                    "issue_count": len(issue_index),
                    "rule_files": entry.get("rule_files"),
                    "files": entry.get("files"),
                },
                "facts": {
                    "snapshot": name,
                    "bids_version": entry.get("BIDS_VERSION"),
                    "schema_version": entry.get("SCHEMA_VERSION"),
                    "issue_codes": sorted(issue_index.keys())[:400],
                    "issue_count": len(issue_index),
                },
                "hints": {"kind": "schema_snapshot"},
            }
        )
        # collect per-code variants across snapshots
        for code, idef in issue_index.items():
            key = (idef.get("level"), idef.get("message"))
            variants.setdefault(code, {}).setdefault(key, {"level": idef.get("level"), "message": idef.get("message"), "snapshots": []})
            variants[code][key]["snapshots"].append(name)

    # version-specific issue behavior records
    for code in sorted(variants):
        vs = variants[code]
        if len(vs) == 1:
            continue  # identical across all snapshots: no version-specific behavior
        record_variants = []
        for (_, _), v in sorted(vs.items(), key=lambda kv: sorted(kv[1]["snapshots"])):
            record_variants.append(
                {
                    "level": v["level"],
                    "message": v["message"],
                    "schema_snapshots": sorted(v["snapshots"]),
                }
            )
        record_variants.sort(key=lambda v: v["schema_snapshots"][0])
        levels = {v["level"] for v in record_variants}
        records.append(
            {
                "record_key": f"issue_version_variants|{code}",
                "knowledge_type": "Version",
                "knowledge_family": f"issue:{code}",
                "authority": "schema_archive",
                "title": f"Version-specific definitions of {code}",
                "summary": f"Issue {code} is defined differently across archived schema snapshots "
                f"({len(record_variants)} variants; severity variants: {sorted(levels)}). "
                "Consult the snapshot matching the schema version under validation.",
                "scope": {
                    "knowledge_domain": "version_history",
                    "issue_code": code,
                    "content_stage": "archived_schema",
                },
                "bids_version": None,
                "schema_version": None,
                "source": _src("bids-schema", revisions, path="versions/"),
                "raw_content": {"variants": record_variants, "issue_code": code},
                "facts": {
                    "issue_code": code,
                    "version_specific": True,
                    "variants": record_variants,
                    "severity_differs_across_versions": len(levels) > 1,
                },
                "hints": {"kind": "issue_versions", "code": code},
            }
        )
    return records


def extract_schema_repo_concepts(revisions: dict) -> list[dict]:
    records = []
    root = os.path.join(config.SOURCES_DIR, "bids-schema")
    readme = os.path.join(root, "README.md")
    if os.path.exists(readme):
        text, _, _ = util.read_text(readme)
        records.append(
            {
                "record_key": "bids_schema_repo_readme",
                "knowledge_type": "Concept",
                "knowledge_family": "concept:bids_schema_repo",
                "authority": "schema_archive",
                "title": "Purpose of the bids-schema repository",
                "summary": util.truncate(
                    util.normalize_ws(text)[:400] or "README of the bids-schema repository.", 300
                ),
                "scope": {"knowledge_domain": "source_description"},
                "bids_version": None,
                "schema_version": None,
                "source": _src("bids-schema", revisions, file="README.md", path="README.md"),
                "raw_content": {"text": text[:6000]},
                "facts": {},
                "hints": {"kind": "repo_readme"},
            }
        )
    return records
