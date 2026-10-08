#!/usr/bin/env python3
"""
BIDS Knowledge Extraction & Knowledge-Base Construction
Transforms raw BIDS specification YAML/Markdown files into atomic, traceable knowledge records.

Outputs:
- knowledge.jsonl
- relationships.jsonl
- sources.jsonl
- processing_report.json
- README.md (summary)
"""

import json
import os
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

# ============================================================
# Configuration
# ============================================================

OUT_DIR = Path(__file__).parent

# The raw BIDS schema lives in a sibling directory of this script. It can be
# overridden so the extractor can be pointed at a different schema checkout.
SRC_DIR = Path(
    os.environ.get("BIDS_RULES_DIR", OUT_DIR.parent / "BIDS-Rules")
).resolve()

BIDS_VERSION = "1.11.2-dev"
SCHEMA_VERSION = "2.0.0-dev"

# Output files
OUTPUT_KNOWLEDGE = OUT_DIR / "knowledge.jsonl"
OUTPUT_RELATIONSHIPS = OUT_DIR / "relationships.jsonl"
OUTPUT_SOURCES = OUT_DIR / "sources.jsonl"
OUTPUT_REPORT = OUT_DIR / "processing_report.json"
OUTPUT_README = OUT_DIR / "KB_README.md"

# Counters
stats = {
    "files_processed": 0,
    "records_created": 0,
    "relationships_created": 0,
    "duplicates_found": 0,
    "conflicts_found": 0,
    "inferred_records": 0,
    "unclassified_sections": 0,
    "errors": 0,
    "warnings": 0,
}

# Registry for tracking sources and relationships
source_registry = defaultdict(list)
knowledge_records = []
relationship_records = []


def make_id(prefix: str, key: str, suffix: str = "", section: str = "") -> str:
    """Generate a stable unique identifier."""
    parts = [prefix, key.replace(" ", "_").lower()]
    if section:
        parts.append(section.replace(" ", "_").lower())
    if suffix:
        parts.append(suffix)
    return "_".join(parts)


# Every id handed out so far, so no two records can share one.
_used_ids = {}


def unique_id(candidate: str, hint: str = "") -> str:
    """Return ``candidate``, or a disambiguated variant if it is already taken.

    Ids collide for reasons that are invisible in the id itself: make_id() folds
    case, so the schema's MISCChannelCount and MiscChannelCount become one
    string, and the same rule name is defined in two different files. A duplicate
    id is not cosmetic. The retriever indexes records by id, so the second record
    silently replaces the first and one rule becomes unanswerable.
    """
    if candidate not in _used_ids:
        _used_ids[candidate] = 1
        return candidate

    if hint:
        hinted = f"{candidate}__{re.sub(r'[^a-z0-9]+', '_', hint.lower()).strip('_')}"
        if hinted not in _used_ids:
            _used_ids[hinted] = 1
            return hinted

    _used_ids[candidate] += 1
    numbered = f"{candidate}__{_used_ids[candidate]}"
    while numbered in _used_ids:
        _used_ids[candidate] += 1
        numbered = f"{candidate}__{_used_ids[candidate]}"
    _used_ids[numbered] = 1
    return numbered


def save_record(record):
    """Save a knowledge record, guaranteeing its id is unique."""
    source = record.get("source") or {}
    record["id"] = unique_id(
        record.get("id", ""),
        hint=str(source.get("section") or source.get("file") or ""),
    )
    knowledge_records.append(record)
    stats["records_created"] += 1


def save_relationship(rel):
    """Save a relationship record."""
    relationship_records.append(rel)
    stats["relationships_created"] += 1


def record_source(filepath: str, category: str, section: str = None, key: str = None):
    """Register source information."""
    source_registry[filepath].append({
        "category": category,
        "section": section or "",
        "key": key or "",
    })


# ============================================================
# YAML Loader (handles $ref references)
# ============================================================

def safe_load_yaml(filepath: str, content: str = None) -> dict:
    """Load a YAML file safely, handling $ref references."""
    if content is None:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    try:
        data = yaml.safe_load(content)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as e:
        print(f"Warning: YAML parsing error in {filepath}: {e}")
        return {}


def resolve_refs(data, ref_map=None):
    """Resolve $ref references in YAML if ref_map is provided."""
    if ref_map is None:
        ref_map = {}
    
    def _resolve(obj):
        if isinstance(obj, str) and obj.startswith("$ref:"):
            ref_path = obj.replace("$ref:", "").strip()
            return ref_map.get(ref_path, ref_path)
        elif isinstance(obj, dict):
            return {k: _resolve(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_resolve(i) for i in obj]
        return obj
    
    return _resolve(data)


# ============================================================
# Shared schema helpers
#
# The BIDS schema separates *definitions* (objects/) from *rules* (rules/).
# A field like RepetitionTime is DEFINED once in objects/metadata.yaml and then
# REFERENCED by many rule groups that each say, for one datatype/suffix
# combination, whether it is required, recommended or optional. Neither half is
# usable on its own: the definition never says where the field applies, and the
# rule never says what the field means. These helpers load the definitions once
# so both halves can be joined into a single answerable record.
# ============================================================

# Populated by load_schema_objects(); keyed by field/column name.
METADATA_OBJECTS = {}
COLUMN_OBJECTS = {}
ENUM_OBJECTS = {}

# Requirement levels used throughout the schema, ordered from strongest to
# weakest so the strongest level across rule groups can be reported.
LEVEL_ORDER = ["required", "recommended", "optional", "deprecated", "prohibited"]

# How each level should be phrased. The distinction matters: a validator reports
# a missing required field as an error and a missing recommended field as a
# warning, and users routinely treat the two as the same thing.
LEVEL_PHRASING = {
    "required": "REQUIRED (its absence is a validation error)",
    "recommended": "RECOMMENDED (its absence is a validation warning, not an error)",
    "optional": "OPTIONAL (it may be omitted without any validation message)",
    "deprecated": "DEPRECATED (still tolerated, but it should no longer be used)",
    "prohibited": "PROHIBITED (it must not be present)",
}


def load_schema_objects():
    """Load the object definitions that rules refer to by name."""
    global METADATA_OBJECTS, COLUMN_OBJECTS, ENUM_OBJECTS

    meta_path = SRC_DIR / "objects" / "metadata.yaml"
    if meta_path.exists():
        METADATA_OBJECTS = safe_load_yaml(str(meta_path))

    cols_path = SRC_DIR / "objects" / "columns.yaml"
    if cols_path.exists():
        COLUMN_OBJECTS = safe_load_yaml(str(cols_path))

    enums_path = SRC_DIR / "objects" / "enums.yaml"
    if enums_path.exists():
        ENUM_OBJECTS = safe_load_yaml(str(enums_path))


def clean_text(value, limit=None) -> str:
    """Flatten a schema description into a single line of readable prose.

    Schema descriptions are markdown with hard line wraps and SPEC_ROOT links
    that mean nothing outside the specification website.
    """
    text = " ".join(str(value or "").split())

    # Markdown links, keeping the link text and dropping the target. The target
    # may itself contain balanced parentheses (DICOM tag URLs end in "(0020,0110)"),
    # so a non-greedy "[^)]*" stops at the wrong bracket and leaves a stray ")".
    text = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)", r"\1", text)

    text = text.replace("`", "")
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut + "..."
    return text.strip()


def resolve_enum(value):
    """Resolve a schema ``$ref`` into the value it points at.

    Enumerations in rules are stored as references such as
    ``{"$ref": "objects.enums.EEG.value"}``. Left unresolved they are useless to
    a reader, who sees a pointer instead of the allowed value.
    """
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("objects.enums."):
            key = ref.split(".")[2] if len(ref.split(".")) > 2 else ""
            entry = ENUM_OBJECTS.get(key)
            if isinstance(entry, dict):
                return entry.get("value", key)
            return key
        for key in ("value", "name", "const"):
            if key in value:
                return value[key]
        return None
    return value


def describe_type(definition) -> str:
    """Describe the value a field or column accepts, in words."""
    if not isinstance(definition, dict):
        return ""

    alternatives = definition.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        parts = [describe_type(alt) for alt in alternatives]
        parts = [p for p in parts if p]
        if parts:
            return " or ".join(dict.fromkeys(parts))

    declared = definition.get("type", "")
    bits = []

    if declared == "array":
        items = definition.get("items")
        inner = describe_type(items) if isinstance(items, dict) else "value"
        bits.append(f"an array of {inner}" if inner else "an array")
        if "minItems" in definition:
            bits.append(f"minimum {definition['minItems']} item(s)")
        if "maxItems" in definition:
            bits.append(f"maximum {definition['maxItems']} item(s)")
    elif declared:
        bits.append(f"a {declared}")

    if "unit" in definition:
        bits.append(f"in {definition['unit']}")
    if "units" in definition:
        bits.append(f"in {definition['units']}")
    if "minimum" in definition:
        bits.append(f"minimum {definition['minimum']}")
    if "exclusiveMinimum" in definition:
        bits.append(f"greater than {definition['exclusiveMinimum']}")
    if "maximum" in definition:
        bits.append(f"maximum {definition['maximum']}")
    if "pattern" in definition:
        bits.append(f"matching the pattern {definition['pattern']}")
    if "format" in definition:
        bits.append(f"of format {definition['format']}")

    return ", ".join(bits)


def allowed_values_of(definition):
    """The list of values a field or column accepts, with refs resolved."""
    if not isinstance(definition, dict):
        return None

    collected = []
    for source in (definition.get("enum"), definition.get("values")):
        if isinstance(source, list):
            for item in source:
                resolved = resolve_enum(item)
                if resolved is not None and resolved not in collected:
                    collected.append(resolved)

    alternatives = definition.get("anyOf")
    if isinstance(alternatives, list):
        for alt in alternatives:
            for item in (allowed_values_of(alt) or []):
                if item not in collected:
                    collected.append(item)

    items = definition.get("items")
    if isinstance(items, dict):
        for item in (allowed_values_of(items) or []):
            if item not in collected:
                collected.append(item)

    return collected or None


# ------------------------------------------------------------
# Rendering schema expressions as prose
#
# Selectors and checks are written in the schema's own expression language.
# "suffix == 'T1w'" is readable enough, but "nifti_header.dim[0] == 3" and
# 'match(extension, "^\.nii(\.gz)?$")' are not, and they are exactly the part a
# user needs explained when a check fails.
# ------------------------------------------------------------

EXPRESSION_GLOSSARY = [
    (r'^match\(extension,\s*"\^\\\.nii\(\\\.gz\)\?\$"\)$',
     "the file is a NIfTI image (.nii or .nii.gz)"),
    (r'^datatype\s*==\s*[\'"](\w+)[\'"]$',
     r"the file is in the \1 datatype directory"),
    (r'^suffix\s*==\s*[\'"]([\w\-]+)[\'"]$',
     r"the file's suffix is \1"),
    (r'^extension\s*==\s*[\'"]([\w.\-]+)[\'"]$',
     r"the file extension is \1"),
    (r'^modality\s*==\s*[\'"](\w+)[\'"]$',
     r"the file belongs to the \1 modality"),
    (r'^nifti_header\s*!=\s*null$',
     "the NIfTI header could be read"),
    (r'^sidecar\s*!=\s*null$',
     "a JSON sidecar was found for the file"),
    (r'^"(\w+)"\s+in\s+sidecar$',
     r"the sidecar defines \1"),
    (r'^!\("(\w+)"\s+in\s+sidecar\)$',
     r"the sidecar does not define \1"),
    (r'^nifti_header\.dim\[0\]\s*==\s*(\d+)$',
     r"the image has exactly \1 dimensions"),
    (r'^nifti_header\.dim\[(\d+)\]\s*==\s*(\d+)$',
     r"dimension \1 of the image is exactly \2"),
    (r'^nifti_header\.dim\[(\d+)\]\s*>\s*(\d+)$',
     r"dimension \1 of the image is greater than \2"),
    (r'^type\s*==\s*[\'"](\w+)[\'"]$',
     r"the entry is a \1"),
]


def explain_expression(expression) -> str:
    """Render one schema expression in plain language, best effort.

    Anything not recognised is returned unchanged rather than dropped: a raw
    expression is still useful, an omission is not.
    """
    text = str(expression or "").strip()
    if not text:
        return ""

    for pattern, replacement in EXPRESSION_GLOSSARY:
        match = re.match(pattern, text)
        if match:
            return re.sub(pattern, replacement, text)

    readable = text
    readable = re.sub(r'\bintersects\(([^,]+),\s*([^)]+)\)', r"\1 shares a value with \2", readable)
    readable = re.sub(r'\bmatch\(([^,]+),\s*"([^"]+)"\)', r"\1 matches \2", readable)
    readable = re.sub(r'\bexists\(([^,]+),\s*"([^"]+)"\)', r"\1 exists relative to the \2", readable)
    readable = re.sub(r'\bcount\(([^)]+)\)', r"the number of \1", readable)
    readable = re.sub(r'\blength\(([^)]+)\)', r"the length of \1", readable)
    readable = readable.replace("&&", "and").replace("||", "or")
    return readable


def explain_expressions(expressions, joiner="; ") -> str:
    """Render a list of schema expressions as one readable clause."""
    if not expressions:
        return ""
    if isinstance(expressions, str):
        expressions = [expressions]
    parts = [explain_expression(e) for e in expressions]
    return joiner.join(p for p in parts if p)


def as_list(value):
    """Coerce a schema value that may be a scalar or a list into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def resolve_schema_ref(ref: str):
    """Resolve a dotted intra-schema reference such as
    ``rules.checks.deprecations.AnatomicalLandmarkCoordinateSystemDeprecation``.

    A check may inherit its whole ``issue`` block from a sibling this way. Left
    unresolved, the inheriting check has no code, no message and no severity, so
    it silently falls back to the default of "error" and becomes unfindable
    under the code it actually reports.
    """
    parts = [p for p in str(ref or "").split(".") if p]
    if len(parts) < 2:
        return None

    # The longest leading run of path components that names a real file wins,
    # because a schema key may itself contain no dots but a path may be nested.
    for split_at in range(len(parts) - 1, 0, -1):
        base = SRC_DIR.joinpath(*parts[:split_at])
        for candidate in (base.with_suffix(".yaml"), base.with_suffix(".yml")):
            if candidate.exists():
                data = safe_load_yaml(str(candidate))
                node = data
                for key in parts[split_at:]:
                    if not isinstance(node, dict) or key not in node:
                        node = None
                        break
                    node = node[key]
                if node is not None:
                    return node
    return None


def expand_schema_ref(entry):
    """Merge a ``$ref`` target into an entry, with local keys winning."""
    if not isinstance(entry, dict) or "$ref" not in entry:
        return entry

    target = resolve_schema_ref(entry["$ref"])
    if not isinstance(target, dict):
        return entry

    merged = dict(target)
    for key, value in entry.items():
        if key != "$ref":
            merged[key] = value
    return merged


def rel_source(path) -> str:
    """Path of a schema file relative to the schema root, POSIX-style.

    Always POSIX: the knowledge base is generated on one machine and read on
    another, and a backslash in a source path silently splits one file into two
    entries in the source inventory.
    """
    try:
        return Path(path).resolve().relative_to(SRC_DIR).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


# ============================================================
# Version Information (Section 23)
# ============================================================

def extract_version_info():
    """Extract BIDS and schema version information."""
    bids_ver_path = SRC_DIR / "BIDS_VERSION"
    schema_ver_path = SRC_DIR / "SCHEMA_VERSION"
    versions_path = SRC_DIR / "meta" / "versions.yaml"

    bids_version = BIDS_VERSION
    schema_version = SCHEMA_VERSION
    released_versions = []

    if bids_ver_path.exists():
        bids_version = bids_ver_path.read_text().strip()
    if schema_ver_path.exists():
        schema_version = schema_ver_path.read_text().strip()

    if versions_path.exists():
        data = safe_load_yaml(str(versions_path))
        if isinstance(data, list):
            released_versions = [str(v) for v in data]

    # Knowledge record for BIDS version
    save_record({
        "id": make_id("ver", "bids", bids_version),
        "knowledge_type": "Version",
        "title": f"BIDS Specification Version {bids_version}",
        "summary": f"Current BIDS specification version is {bids_version}.",
        "retrieval_text": f"The BIDS specification is at version {bids_version}. Released versions include: {', '.join(released_versions[:5])}...",
        "scope": {},
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": {
            "file": "BIDS_VERSION",
            "path": str(SRC_DIR / "BIDS_VERSION"),
            "section": "global",
            "key": "version"
        },
        "raw_content": {"version": bids_version}
    })

    # Knowledge record for schema version
    save_record({
        "id": make_id("ver", "schema", schema_version),
        "knowledge_type": "Version",
        "title": f"Schema Version {schema_version}",
        "summary": f"BIDS schema version is {schema_version}.",
        "retrieval_text": f"The BIDS schema (machine-readable YAML definitions) is at version {schema_version}.",
        "scope": {},
        "bids_version": bids_version,
        "schema_version": schema_version,
        "source": {
            "file": "SCHEMA_VERSION",
            "path": str(SRC_DIR / "SCHEMA_VERSION"),
            "section": "global",
            "key": "schema_version"
        },
        "raw_content": {"schema_version": schema_version}
    })

    # Released versions
    for i, ver in enumerate(released_versions):
        save_record({
            "id": make_id("ver", "released", ver, str(i)),
            "knowledge_type": "Version",
            "title": f"Released BIDS Version {ver}",
            "summary": f"BIDS version {ver} is a released (non-dev) version.",
            "retrieval_text": f"BIDS version {ver} is a released version of the specification, prior to {bids_version}.",
            "scope": {},
            "bids_version": ver,
            "schema_version": schema_version,
            "source": {
                "file": "meta/versions.yaml",
                "path": str(SRC_DIR / "meta" / "versions.yaml"),
                "section": "released_versions",
                "key": str(i)
            },
            "raw_content": {"version": ver, "released": True}
        })
        save_relationship({
            "source": "version_schema",
            "relation": "constrains",
            "target": f"ver_released_{ver.replace('.', '_')}",
            "source_reference": "meta/versions.yaml",
            "confidence": "explicit"
        })

    # Register sources
    record_source("BIDS_VERSION", "version_info")
    record_source("SCHEMA_VERSION", "version_info")
    record_source("meta/versions.yaml", "version_info")


# ============================================================
# Context & Association Extraction (Section 12)
# ============================================================

def extract_context():
    """Extract context namespaces and file associations."""
    context_path = SRC_DIR / "meta" / "context.yaml"
    associations_path = SRC_DIR / "meta" / "associations.yaml"

    if context_path.exists():
        content = context_path.read_text()
        record_source("meta/context.yaml", "context", "namespaces")
        
        # Extract namespace structure
        context_keys = ["schema", "dataset", "subject", "path", "size", "entities", 
                       "datatype", "suffix", "extension", "modality", "sidecar", "associations"]
        for key in context_keys:
            save_record({
                "id": make_id("ctx", "namespace", key),
                "knowledge_type": "Concept",
                "title": f"Context Namespace: {key}",
                "summary": f"The '{key}' namespace is available during validation for the current file being inspected.",
                "retrieval_text": f"The '{key}' context namespace provides access to {key} information during BIDS validation. "
                                f"This namespace is available when visiting all files in a dataset or subject.",
                "scope": {},
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "meta/context.yaml",
                    "path": str(context_path),
                    "section": "namespaces",
                    "key": key
                },
                "raw_content": {"context_key": key}
            })
        
        # Build relationships between context namespaces
        ns_rels = [
            ("dataset", "contains", "subjects"),
            ("dataset", "contains", "datatypes"),
            ("dataset", "contains", "modalities"),
            ("dataset", "contains", "dataset_description"),
            ("dataset", "contains", "tree"),
            ("subject", "contains", "sessions"),
            ("file_context", "provides", "entities"),
            ("file_context", "provides", "sidecar"),
            ("file_context", "provides", "associations"),
        ]
        for src, rel, tgt in ns_rels:
            save_relationship({
                "source": f"ctx_{src}",
                "relation": rel,
                "target": f"ctx_{tgt}",
                "source_reference": "meta/context.yaml",
                "confidence": "explicit"
            })

    if associations_path.exists():
        data = safe_load_yaml(str(associations_path))
        record_source("meta/associations.yaml", "association_rules")
        
        for assoc_name, assoc_data in data.items():
            if not isinstance(assoc_data, dict):
                continue
            
            selectors = assoc_data.get("selectors", [])
            target = assoc_data.get("target", {})
            inherit = assoc_data.get("inherit", False)
            
            ret_txt = f"The '{assoc_name}' association applies when file conditions match the selector rules. "
            if inherit:
                ret_txt += "Associated files may be inherited from shallower directory levels."
            else:
                ret_txt += "Associated files are expected in the same directory level."
            
            save_record({
                "id": make_id("assoc", assoc_name),
                "knowledge_type": "Association",
                "title": f"File Association: {assoc_name}",
                "summary": f"Defines when to search for an associated file matching '{target.get('suffix', 'unknown')}' "
                          f"with extension '{target.get('extension', 'any')}'.",
                "retrieval_text": ret_txt,
                "scope": {},
                "conditions": selectors,
                "requirements": target,
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "meta/associations.yaml",
                    "path": str(associations_path),
                    "section": "associations",
                    "key": assoc_name
                },
                "raw_content": assoc_data
            })
            
            save_relationship({
                "source": "context_associations",
                "relation": "defines",
                "target": f"assoc_{assoc_name}",
                "source_reference": "meta/associations.yaml",
                "confidence": "explicit"
            })


# ============================================================
# Expression Tests (Section 14)
# ============================================================

def extract_expression_tests():
    """Extract machine-readable expression tests for validation rules."""
    expr_path = SRC_DIR / "meta" / "expression_tests.yaml"
    
    if expr_path.exists():
        data = safe_load_yaml(str(expr_path))
        record_source("meta/expression_tests.yaml", "expression_tests")
        
        if isinstance(data, list):
            # Group by category (null fall-through, general, etc.)
            current_group = "general"
            for item in data:
                if not isinstance(item, dict) or "expression" not in item:
                    continue
                
                expr = item["expression"]
                result = item.get("result", None)
                
                # Detect null section
                if expr.startswith("null") or "_null" in expr.lower():
                    current_group = "null_fallthrough"
                elif any(op in expr for op in ["+", "-", "*", "/", "%"]):
                    current_group = "general_arithmetic"
                elif "match(" in expr or "substr(" in expr:
                    current_group = "general_string"
                elif "intersects(" in expr or "length(" in expr:
                    current_group = "general_array"
                elif "sorted(" in expr:
                    current_group = "general_sort"
                
                ret_txt = f"Expression '{expr}' evaluates to '{result}'."
                
                save_record({
                    "id": make_id("expr", "test", current_group, expr[:50].replace(" ", "_")),
                    "knowledge_type": "Expression",
                    "title": f"Expression Test: {expr[:60]}",
                    "summary": f"Validates that the expression '{expr}' produces expected result '{result}'.",
                    "retrieval_text": ret_txt,
                    "expression": expr,
                    "expected_result": result,
                    "scope": {"group": current_group},
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "meta/expression_tests.yaml",
                        "path": str(expr_path),
                        "section": current_group,
                        "key": expr
                    },
                    "raw_content": item
                })
                
                save_relationship({
                    "source": "test_expression",
                    "relation": "validates",
                    "target": "expr_engine",
                    "source_reference": "meta/expression_tests.yaml",
                    "confidence": "explicit"
                })


# ============================================================
# Templates (Section 14)
# ============================================================

def extract_templates():
    """Extract filename templates for raw, deriv, and atlas data."""
    templates_path = SRC_DIR / "meta" / "templates.yaml"
    
    if templates_path.exists():
        data = safe_load_yaml(str(templates_path))
        record_source("meta/templates.yaml", "templates")
        
        # Extract raw templates
        raw_templates = data.get("raw", {})
        for tmpl_name, tmpl_data in raw_templates.items():
            if not isinstance(tmpl_data, dict):
                continue
            
            entities = tmpl_data.get("entities", {})
            ref = tmpl_data.get("$ref", "")
            
            entity_list = []
            for ent_name, ent_req in entities.items():
                if ent_name != "$ref":
                    entity_list.append({
                        "name": ent_name,
                        "requirement": str(ent_req) if not isinstance(ent_req, str) else "required"
                    })
            
            ret_txt = f"The raw template '{tmpl_name}' defines filename entity requirements. "
            ret_txt += "Entities include: " + ", ".join(f"{e['name']} ({e['requirement']})" for e in entity_list[:5])
            
            save_record({
                "id": make_id("tmpl", "raw", tmpl_name),
                "knowledge_type": "Template",
                "title": f"Raw File Template: {tmpl_name}",
                "summary": f"Defines filename structure for {tmpl_name} raw BIDS files.",
                "retrieval_text": ret_txt,
                "scope": {
                    "file_types": ["raw"],
                    "template": tmpl_name
                },
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "meta/templates.yaml",
                    "path": str(templates_path),
                    "section": "raw",
                    "key": tmpl_name
                },
                "raw_content": tmpl_data
            })
            
            save_relationship({
                "source": "meta_templates_raw",
                "relation": "templates",
                "target": f"tmpl_raw_{tmpl_name}",
                "source_reference": "meta/templates.yaml",
                "confidence": "explicit"
            })
        
        # Extract derivative templates
        deriv_templates = data.get("deriv", {})
        for tmpl_name, tmpl_data in deriv_templates.items():
            if not isinstance(tmpl_data, dict):
                continue
            
            entities = tmpl_data.get("entities", {})
            extensions = tmpl_data.get("extensions", [])
            suffixes = tmpl_data.get("suffixes", [])
            selectors = tmpl_data.get("selectors", [])
            ref = tmpl_data.get("$ref", "")
            
            entity_list = []
            if isinstance(entities, dict):
                for ent_name, ent_req in entities.items():
                    if ent_name != "$ref":
                        entity_list.append({
                            "name": ent_name,
                            "requirement": str(ent_req) if not isinstance(ent_req, str) else "optional"
                        })
            
            entity_names = ", ".join(e["name"] for e in entity_list[:5])
            ret_txt = f"The derivative template '{tmpl_name}' defines filename structure. "
            ret_txt += f"Entities: {entity_names}. "
            if extensions:
                ext_str = ", ".join(extensions)
                ret_txt += f"Extensions: {ext_str}. "
            else:
                ret_txt += "Extensions: various. "
            if suffixes:
                suff_str = ", ".join(suffixes)
                ret_txt += f"Suffixes: {suff_str}."
            
            save_record({
                "id": make_id("tmpl", "deriv", tmpl_name),
                "knowledge_type": "Template",
                "title": f"Derivative Template: {tmpl_name}",
                "summary": f"Defines filename structure, extensions, and suffixes for {tmpl_name} derivatives.",
                "retrieval_text": ret_txt,
                "scope": {
                    "file_types": ["derivative"],
                    "template": tmpl_name,
                    "extensions": extensions,
                    "suffixes": suffixes
                },
                "conditions": selectors,
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "meta/templates.yaml",
                    "path": str(templates_path),
                    "section": "deriv",
                    "key": tmpl_name
                },
                "raw_content": tmpl_data
            })
            
            save_relationship({
                "source": "meta_templates_deriv",
                "relation": "templates",
                "target": f"tmpl_deriv_{tmpl_name}",
                "source_reference": "meta/templates.yaml",
                "confidence": "explicit"
            })
        
        # Extract atlas templates
        atlas_templates = data.get("atlas", {})
        for tmpl_name, tmpl_data in atlas_templates.items():
            if not isinstance(tmpl_data, dict):
                continue
            
            entities = tmpl_data.get("entities", {})
            
            entity_list = []
            for ent_name, ent_req in entities.items():
                entity_list.append({
                    "name": ent_name,
                    "requirement": str(ent_req) if not isinstance(ent_req, str) else "optional"
                })
            
            save_record({
                "id": make_id("tmpl", "atlas", tmpl_name),
                "knowledge_type": "Template",
                "title": f"Atlas Template: {tmpl_name}",
                "summary": f"Defines filename entity requirements for atlas files: {', '.join(e['name'] for e in entity_list)}.",
                "retrieval_text": f"The atlas template '{tmpl_name}' specifies entity requirements for atlas BIDS files. "
                                f"Required/allowed entities include: {', '.join(e['name'] for e in entity_list)}.",
                "scope": {
                    "file_types": ["atlas", "derivative"],
                    "template": tmpl_name
                },
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "meta/templates.yaml",
                    "path": str(templates_path),
                    "section": "atlas",
                    "key": tmpl_name
                },
                "raw_content": tmpl_data
            })


# ============================================================
# Objects - Concepts & Definitions (Section 11)
# ============================================================

def extract_common_principles():
    """Extract BIDS common principle definitions."""
    objs_path = SRC_DIR / "objects" / "common_principles.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/common_principles.yaml", "definitions")
        
        for concept, concept_data in data.items():
            if not isinstance(concept_data, dict):
                continue
            
            display_name = concept_data.get("display_name", concept)
            description = concept_data.get("description", "")
            
            save_record({
                "id": make_id("concept", concept),
                "knowledge_type": "Definition",
                "title": f"BIDS Concept: {display_name}",
                "summary": description[:200] + "..." if len(description) > 200 else description,
                "retrieval_text": f"In BIDS, {display_name} refers to: {description}.",
                "scope": {},
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "objects/common_principles.yaml",
                    "path": str(objs_path),
                    "section": "common_principles",
                    "key": concept
                },
                "raw_content": concept_data
            })
            
            save_relationship({
                "source": "concepts",
                "relation": "defines",
                "target": f"concept_{concept}",
                "source_reference": "objects/common_principles.yaml",
                "confidence": "explicit"
            })


def extract_entities():
    """Extract entity definitions from objects/entities.yaml."""
    objs_path = SRC_DIR / "objects" / "entities.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/entities.yaml", "entity_definitions")
        
        for ent_key, ent_data in data.items():
            if not isinstance(ent_data, dict):
                continue
            
            name = ent_data.get("name", ent_key)
            display_name = ent_data.get("display_name", name)
            description = ent_data.get("description", "")
            type_ = ent_data.get("type", "string")
            fmt = ent_data.get("format", "label")
            enum = ent_data.get("enum", [])
            
            save_record({
                "id": make_id("entity", name),
                "knowledge_type": "Concept",
                "title": f"Entity: {display_name} ({name})",
                "summary": f"The '{name}' entity is used in BIDS filenames. Type: {type_}, Format: {fmt}.",
                "retrieval_text": f"The `{name}` entity in BIDS is used for: {description} "
                                f"It is a {type_} of format {fmt}.",
                "scope": {},
                "allowed_values": [v.get("value", v) if isinstance(v, dict) else v for v in enum] if enum else None,
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "objects/entities.yaml",
                    "path": str(objs_path),
                    "section": "entities",
                    "key": ent_key
                },
                "raw_content": ent_data
            })
            
            # Metadata mapping if entity references a metadata field
            if "ContrastBolusIngredient" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_ContrastBolusIngredient",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })
            elif "PhaseEncodingDirection" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_PhaseEncodingDirection",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })
            elif "EchoTime" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_EchoTime",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })
            elif "FlipAngle" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_FlipAngle",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })
            elif "InversionTime" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_InversionTime",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })
            elif "Density" in description:
                save_relationship({
                    "source": f"entity_{name}",
                    "relation": "maps_to_metadata",
                    "target": "metadata_Density",
                    "source_reference": "objects/entities.yaml",
                    "confidence": "explicit"
                })


def extract_entity_order():
    """Extract ordered entity sequence from rules/entities.yaml."""
    rules_path = SRC_DIR / "rules" / "entities.yaml"
    
    if rules_path.exists():
        data = safe_load_yaml(str(rules_path))
        record_source("rules/entities.yaml", "entity_order")
        
        if isinstance(data, list):
            ordered_entities = []
            for i, ent in enumerate(data):
                ordered_entities.append({
                    "order": i,
                    "entity": ent
                })
            
            save_record({
                "id": make_id("order", "entities"),
                "knowledge_type": "FilenameRule",
                "title": "Entity Ordering Rule",
                "summary": "Defines the required order of entities within BIDS filenames.",
                "retrieval_text": "BIDS entities must appear in a specific order within filenames. "
                                f"The ordered sequence is: {', '.join(data[:10])}...",
                "scope": {"file_types": ["all"]},
                "requirements": [{"entity": e, "position": i} for i, e in enumerate(data[:20])],
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "rules/entities.yaml",
                    "path": str(rules_path),
                    "section": "entity_order",
                    "key": "ordered_sequence"
                },
                "raw_content": {"entity_order": data}
            })


def extract_suffixes():
    """Extract suffix definitions from objects/suffixes.yaml."""
    objs_path = SRC_DIR / "objects" / "suffixes.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/suffixes.yaml", "suffix_definitions")
        
        if isinstance(data, dict):
            for suff_key, suff_data in data.items():
                if not isinstance(suff_data, dict):
                    continue
                
                value = suff_data.get("value", suff_key)
                display_name = suff_data.get("display_name", value)
                description = suff_data.get("description", "")
                unit = suff_data.get("unit", None)
                min_val = suff_data.get("minValue", None)
                max_val = suff_data.get("maxValue", None)
                
                save_record({
                    "id": make_id("suff", value),
                    "knowledge_type": "Concept",
                    "title": f"Suffix: {display_name} ({value})",
                    "summary": f"The '{value}' suffix represents: {display_name}.",
                    "retrieval_text": f"In BIDS, the '{value}' suffix is used for: {description}. "
                                    f"Display name: {display_name}.",
                    "scope": {},
                    "allowed_values": [value],
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/suffixes.yaml",
                        "path": str(objs_path),
                        "section": "suffixes",
                        "key": suff_key
                    },
                    "raw_content": suff_data
                })
            
            count = len([k for k in data.keys() if isinstance(data[k], dict)])
            save_record({
                "id": make_id("suff", "registry"),
                "knowledge_type": "Concept",
                "title": "Suffix Registry",
                "summary": f"Defines {count} valid BIDS file suffixes used in filenames.",
                "retrieval_text": f"The BIDS specification defines {count} valid file suffixes. "
                                f"These suffixes identify the modalities and content types of BIDS files.",
                "scope": {},
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": "objects/suffixes.yaml",
                    "path": str(objs_path),
                    "section": "registry",
                    "key": "all"
                },
                "raw_content": {"suffix_count": count}
            })


def extract_datatypes():
    """Extract datatype definitions from objects/datatypes.yaml."""
    objs_path = SRC_DIR / "objects" / "datatypes.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/datatypes.yaml", "datatype_definitions")
        
        if isinstance(data, dict):
            for dt_key, dt_data in data.items():
                if not isinstance(dt_data, dict):
                    continue
                
                value = dt_data.get("value", dt_key)
                display_name = dt_data.get("display_name", value)
                description = dt_data.get("description", "")
                
                save_record({
                    "id": make_id("dt", value),
                    "knowledge_type": "Concept",
                    "title": f"Datatype: {display_name}",
                    "summary": f"The '{value}' datatype represents: {display_name}.",
                    "retrieval_text": f"In BIDS, '{value}' is a datatype used for: {description}. "
                                    f"Display name: {display_name}.",
                    "scope": {
                        "datatype": [value]
                    },
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/datatypes.yaml",
                        "path": str(objs_path),
                        "section": "datatypes",
                        "key": dt_key
                    },
                    "raw_content": dt_data
                })


def extract_modalities():
    """Extract modality definitions and datatype-modality relationships."""
    objs_path = SRC_DIR / "objects" / "modalities.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/modalities.yaml", "modality_definitions")
        
        if isinstance(data, dict):
            for mod_key, mod_data in data.items():
                if not isinstance(mod_data, dict):
                    continue
                
                display_name = mod_data.get("display_name", mod_key)
                description = mod_data.get("description", "")
                
                save_record({
                    "id": make_id("mod", mod_key),
                    "knowledge_type": "Concept",
                    "title": f"Modality: {display_name}",
                    "summary": f"The '{mod_key}' modality represents: {display_name}.",
                    "retrieval_text": f"In BIDS, '{mod_key}' is a modality: {description}.",
                    "scope": {
                        "modality": [mod_key]
                    },
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/modalities.yaml",
                        "path": str(objs_path),
                        "section": "modalities",
                        "key": mod_key
                    },
                    "raw_content": mod_data
                })
                
                if "datatypes" in mod_data:
                    save_relationship({
                        "source": f"mod_{mod_key}",
                        "relation": "covers",
                        "target": f"dt_group_{mod_key}",
                        "source_reference": "objects/modalities.yaml",
                        "confidence": "explicit"
                    })


def extract_extensions():
    """Extract file extension definitions."""
    objs_path = SRC_DIR / "objects" / "extensions.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/extensions.yaml", "extension_definitions")
        
        if isinstance(data, dict):
            for ext_key, ext_data in data.items():
                if not isinstance(ext_data, dict):
                    continue
                
                value = ext_data.get("value", ext_key)
                display_name = ext_data.get("display_name", value)
                description = ext_data.get("description", "")
                
                save_record({
                    "id": make_id("ext", value.replace(".", "").replace("/", "_")),
                    "knowledge_type": "Concept",
                    "title": f"Extension: {display_name} ({value})",
                    "summary": f"The '{value}' file extension represents: {display_name}.",
                    "retrieval_text": f"In BIDS, the '{value}' extension is used for: {description} "
                                    f"Display name: {display_name}.",
                    "scope": {},
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/extensions.yaml",
                        "path": str(objs_path),
                        "section": "extensions",
                        "key": ext_key
                    },
                    "raw_content": ext_data
                })


def extract_metaentities():
    """Extract metaentity definitions (wildcard placeholders)."""
    objs_path = SRC_DIR / "objects" / "metaentities.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/metaentities.yaml", "metaentity_definitions")
        
        if isinstance(data, dict):
            for me_key, me_data in data.items():
                if not isinstance(me_data, dict):
                    continue
                
                name = me_data.get("name", me_key)
                description = me_data.get("description", "")
                
                save_record({
                    "id": make_id("metaent", name),
                    "knowledge_type": "Concept",
                    "title": f"Metaentity: {name}",
                    "summary": f"The '{name}' metaentity is a placeholder in BIDS filename templates.",
                    "retrieval_text": f"In BIDS, the '{name}' metaentity is used as a wildcard/placeholder in "
                                    f"filename templates: {description}.",
                    "scope": {},
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/metaentities.yaml",
                        "path": str(objs_path),
                        "section": "metaentities",
                        "key": me_key
                    },
                    "raw_content": me_data
                })


def extract_formats():
    """Extract format type definitions."""
    objs_path = SRC_DIR / "objects" / "formats.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/formats.yaml", "format_definitions")
        
        if isinstance(data, dict):
            for fmt_key, fmt_data in data.items():
                if not isinstance(fmt_data, dict):
                    continue
                
                display_name = fmt_data.get("display_name", fmt_key)
                description = fmt_data.get("description", "")
                pattern = fmt_data.get("pattern", "")
                
                fmt_type = fmt_key  # entity vs metadata
                
                save_record({
                    "id": make_id("fmt", fmt_key),
                    "knowledge_type": "Definition",
                    "title": f"Format Type: {display_name}",
                    "summary": f"The '{display_name}' format type {description}." if description else "",
                    "retrieval_text": f"BIDS format type '{display_name}' ({fmt_key}): {description}. "
                                    f"Validation pattern: `{pattern}`" if pattern else f"BIDS format type '{fmt_key}': {description}.",
                    "scope": {"format_type": fmt_type},
                    "expression": pattern if pattern else None,
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/formats.yaml",
                        "path": str(objs_path),
                        "section": "formats",
                        "key": fmt_key
                    },
                    "raw_content": fmt_data
                })


# ============================================================
# Rules: Top-level files, directories, errors
# ============================================================

def extract_top_level_files():
    """Extract top-level file definitions from objects/files.yaml."""
    objs_path = SRC_DIR / "objects" / "files.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/files.yaml", "top_level_files")
        
        if isinstance(data, dict):
            for file_key, file_data in data.items():
                if not isinstance(file_data, dict):
                    continue
                
                display_name = file_data.get("display_name", file_key)
                file_type = file_data.get("file_type", "regular")
                description = file_data.get("description", "")
                dir_file = "directory" if file_type == "directory" else "file"
                
                save_record({
                    "id": make_id("file", file_key),
                    "knowledge_type": "FileSpecification",
                    "title": f"Top-Level File: {display_name}",
                    "summary": f"A {dir_file} named {display_name}.",
                    "retrieval_text": f"In BIDS, the '{display_name}' {dir_file} is used for: {description}. "
                                    f"File type: {dir_file}.",
                    "scope": {"directories": ["root"], "file_type": file_type},
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/files.yaml",
                        "path": str(objs_path),
                        "section": "top_level_files",
                        "key": file_key
                    },
                    "raw_content": file_data
                })


def extract_directory_rules():
    """Extract directory layout rules from rules/directories.yaml."""
    rules_path = SRC_DIR / "rules" / "directories.yaml"
    
    if rules_path.exists():
        data = safe_load_yaml(str(rules_path))
        record_source("rules/directories.yaml", "directory_layouts")
        
        if isinstance(data, dict):
            for layout_name, layout_data in data.items():
                if not isinstance(layout_data, dict):
                    continue
                
                root = layout_data.get("root", {})
                subdirs = root.get("subdirs", [])
                
                save_record({
                    "id": make_id("dir", "layout", layout_name),
                    "knowledge_type": "DirectoryRule",
                    "title": f"Dataset Directory Layout: {layout_name}",
                    "summary": f"Defines the root directory structure for {layout_name} datasets.",
                    "retrieval_text": f"A {'raw' if layout_name == 'raw' else layout_name} BIDS dataset must have "
                                    f"the following root-level directories: {', '.join(subdirs)}.",
                    "scope": {
                        "dataset_type": layout_name,
                        "directories": subdirs
                    },
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "rules/directories.yaml",
                        "path": str(rules_path),
                        "section": "layouts",
                        "key": layout_name
                    },
                    "raw_content": {layout_name: layout_data}
                })
                
                # Detailed directory rules
                for dir_key, dir_data in layout_data.items():
                    if dir_key == "root" or not isinstance(dir_data, dict):
                        continue
                    
                    name = dir_data.get("name", dir_key)
                    level = dir_data.get("level", "optional")
                    opacity = dir_data.get("opacity", True)
                    
                    save_record({
                        "id": make_id("dir", layout_name, name),
                        "knowledge_type": "DirectoryRule",
                        "title": f"Directory '{name}' in {layout_name} layout",
                        "summary": f"The '{name}' directory is {level} in {layout_name} datasets. Contents 'transparent' if not opaque.",
                        "retrieval_text": f"In {layout_name} BIDS datasets, the '{name}' directory is {level}. "
                                        f"{'Its contents are not specified (opaque).' if opacity else 'Its contents follow a defined pattern.'}",
                        "scope": {
                            "dataset_type": layout_name,
                            "directories": [name]
                        },
                        "requirements": {"level": level, "opaque": opacity},
                        "bids_version": BIDS_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        "source": {
                            "file": "rules/directories.yaml",
                            "path": str(rules_path),
                            "section": "layouts",
                            "key": f"{layout_name}/{dir_key}"
                        },
                        "raw_content": {layout_name: {dir_key: dir_data}}
                    })


def extract_errors():
    """Extract validation error and warning definitions."""
    rules_path = SRC_DIR / "rules" / "errors.yaml"
    
    if rules_path.exists():
        data = safe_load_yaml(str(rules_path))
        record_source("rules/errors.yaml", "error_definitions")
        
        if isinstance(data, dict):
            for err_key, err_data in data.items():
                if not isinstance(err_data, dict):
                    continue
                
                code = err_data.get("code", err_key)
                message = err_data.get("message", "")
                level = err_data.get("level", "error")
                selectors = err_data.get("selectors", [])
                
                knowledge_type = "Error" if level == "error" else "Warning"
                
                if selectors and level == "error":
                    selectors_text = ", ".join(s.replace("==", "has ").replace("!=", "does not have ") for s in selectors[:3])
                elif selectors:
                    selectors_text = ", ".join(s.replace("==", "has ").replace("!=", "does not have ") for s in selectors[:3])
                else:
                    selectors_text = ""
                
                if selectors_text:
                    apply_msg = f"Applies to: {selectors_text}."
                else:
                    apply_msg = ""
                
                message = clean_text(message)

                severity_text = (
                    "This is reported as an ERROR and makes the dataset invalid."
                    if level == "error"
                    else "This is reported as a WARNING. The dataset remains "
                    "valid, but the standard advises against the situation."
                )

                save_record({
                    "id": make_id("err", code.lower()),
                    "knowledge_type": knowledge_type,
                    "title": f"{level.upper()} {code}",
                    "summary": f"{code}: {message}" if message else f"{code} ({level}).",
                    "retrieval_text": " ".join(p for p in [
                        f"Validation issue code {code} ({level}).",
                        message,
                        severity_text,
                        apply_msg,
                        f"{code} is a built-in validator issue rather than a "
                        f"schema rule check; it is raised by the validator "
                        f"itself while reading the dataset.",
                    ] if p),
                    "scope": {"issue_code": code, "severity": level},
                    "conditions": selectors,
                    "requirements": {"code": code},
                    "severity": level,
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "rules/errors.yaml",
                        "path": str(rules_path),
                        "section": "errors",
                        "key": err_key
                    },
                    "raw_content": err_data
                })
                
                if selectors:
                    save_relationship({
                        "source": make_id("err", code.lower()),
                        "relation": "applies_to",
                        "target": "file_context",
                        "source_reference": "rules/errors.yaml",
                        "confidence": "explicit"
                    })


# ============================================================
# Modality-specific rules
# ============================================================

def extract_modality_rules():
    """Extract modality-to-datatype mapping from rules/modalities.yaml."""
    rules_path = SRC_DIR / "rules" / "modalities.yaml"
    
    if rules_path.exists():
        data = safe_load_yaml(str(rules_path))
        record_source("rules/modalities.yaml", "modality_rules")
        
        if isinstance(data, dict):
            for mod_key, mod_data in data.items():
                if not isinstance(mod_data, dict):
                    continue
                
                datatypes = mod_data.get("datatypes", [])
                
                save_record({
                    "id": make_id("rule", "modality", mod_key),
                    "knowledge_type": "Relationship",
                    "title": f"Modality '{mod_key}' covers datatypes",
                    "summary": f"The '{mod_key}' modality encompasses: {', '.join(datatypes)}.",
                    "retrieval_text": f"In BIDS, the '{mod_key}' modality includes the following datatypes: "
                                    f"{', '.join(datatypes)}.",
                    "scope": {
                        "modality": [mod_key],
                        "datatypes": datatypes
                    },
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "rules/modalities.yaml",
                        "path": str(rules_path),
                        "section": "modality_datatypes",
                        "key": mod_key
                    },
                    "raw_content": mod_data
                })
                
                for dt in datatypes:
                    save_relationship({
                        "source": f"mod_{mod_key}",
                        "relation": "covers",
                        "target": f"dt_{dt}",
                        "source_reference": "rules/modalities.yaml",
                        "confidence": "explicit"
                    })


# ============================================================
# File Rules - Raw, Derivative, Common
# ============================================================

def extract_file_rules():
    """Extract file specification rules from rules/files/ directory."""
    files_dir = SRC_DIR / "rules" / "files"
    
    if not files_dir.exists():
        return
    
    for root_dir in files_dir.iterdir():
        if not root_dir.is_dir():
            continue
        
        for rule_file in root_dir.rglob("*.yaml"):
            data = safe_load_yaml(str(rule_file))
            record_source(f"rules/files/{root_dir.name}/{rule_file.relative_to(files_dir)}", "file_rules")
            
            if isinstance(data, dict):
                for section_key, section_data in data.items():
                    if not isinstance(section_data, dict):
                        continue
                    
                    # Extract key information based on structure
                    datatypes = section_data.get("datatypes", [])
                    entities = section_data.get("entities", {})
                    suffixes = section_data.get("suffixes", [])
                    extensions = section_data.get("extensions", [])
                    format_ = section_data.get("format", "")
                    
                    if datatypes or entities or suffixes:
                        entity_str = ", ".join(f"{k}: {v}" for k, v in entities.items()) if entities else "none specified"
                        
                        save_record({
                            "id": make_id("file_rule", root_dir.name, section_key),
                            "knowledge_type": "FileSpecification",
                            "title": f"File rule '{section_key}' in {root_dir.name}",
                            "summary": f"Defines file requirements for: {section_key}.",
                            "retrieval_text": f"This BIDS file rule specifies requirements for '{section_key}'. "
                                            f"Datatypes: {', '.join(datatypes) if datatypes else 'any'}. "
                                            f"Entities: {entity_str}.",
                            "scope": {
                                "file_types": [root_dir.name],
                                "datatypes": datatypes,
                                "suffixes": suffixes,
                                "extensions": extensions
                            },
                            "requirements": entities,
                            "bids_version": BIDS_VERSION,
                            "schema_version": SCHEMA_VERSION,
                            "source": {
                                "file": f"rules/files/{root_dir.name}/{rule_file.name}",
                                "path": str(rule_file),
                                "section": "rules",
                                "key": section_key
                            },
                            "raw_content": section_data
                        })


# ============================================================
# Sidecar & JSON metadata rules
# ============================================================

# Where a field or column is required, collected while the rules are read and
# joined into the object's own definition record afterwards.
FIELD_REQUIREMENTS = defaultdict(list)
COLUMN_REQUIREMENTS = defaultdict(list)


def _field_level(field_spec):
    """The requirement level of one field inside a rule group.

    The schema writes a field either as a bare level (``TaskName: required``) or
    as a mapping carrying the level plus addenda. Both forms mean the same thing
    and both occur in the same file.
    """
    if isinstance(field_spec, str):
        return field_spec.strip().lower(), {}
    if isinstance(field_spec, dict):
        level = str(field_spec.get("level", "optional")).strip().lower()
        return level, field_spec
    return "optional", {}


def extract_sidecar_rules():
    """Extract JSON sidecar metadata rules from rules/sidecars/.

    The top-level keys of these files are RULE GROUPS, not field names. A group
    such as ``MRIFuncRepetitionTime`` carries ``selectors`` saying which files it
    governs and ``fields`` naming the metadata that those files must, should or
    may define. Treating the group name as a field name produces records for
    things that are not metadata fields, while the fields users are actually told
    about (RepetitionTime, EchoTime, TaskName) get no record at all.

    One record is emitted per (rule group, field) pair, keyed on the real field
    name, carrying the requirement level and the conditions under which it
    applies. The group itself also gets a record, because "which fields does a
    BOLD file need" is a question about the group.
    """
    sidecars_dir = SRC_DIR / "rules" / "sidecars"

    if not sidecars_dir.exists():
        return

    for rule_file in sorted(sidecars_dir.rglob("*.y*ml")):
        data = safe_load_yaml(str(rule_file))
        source_file = rel_source(rule_file)
        record_source(source_file, "sidecar_rules")

        if not isinstance(data, dict):
            continue

        is_derivative = "derivatives" in rule_file.parts

        for group_name, group_data in data.items():
            if not isinstance(group_data, dict):
                continue

            selectors = as_list(group_data.get("selectors"))
            fields = group_data.get("fields")

            if not isinstance(fields, dict):
                continue

            when_text = explain_expressions(selectors, joiner=" and ")
            scope_text = when_text or "any file the group matches"

            # ---- one record per field in the group --------------------
            group_field_summary = []

            for field_name, field_spec in fields.items():
                level, extra = _field_level(field_spec)
                definition = METADATA_OBJECTS.get(field_name, {})

                description = clean_text(definition.get("description", ""))
                addendum = clean_text(extra.get("description_addendum", ""))
                level_addendum = clean_text(extra.get("level_addendum", ""))
                type_text = describe_type(definition)
                values = allowed_values_of(definition)
                unit = definition.get("unit") or (
                    (definition.get("anyOf") or [{}])[0].get("unit")
                    if isinstance(definition.get("anyOf"), list)
                    else None
                )

                group_field_summary.append(f"{field_name} ({level})")

                FIELD_REQUIREMENTS[field_name].append({
                    "level": level,
                    "rule_group": group_name,
                    "selectors": selectors,
                    "source_file": source_file,
                    "derivative": is_derivative,
                })

                retrieval_parts = [
                    f"The JSON sidecar metadata field {field_name} is "
                    f"{LEVEL_PHRASING.get(level, level)} when {scope_text}.",
                    description,
                    f"Its value is {type_text}." if type_text else "",
                    f"Allowed values: {', '.join(str(v) for v in values)}." if values else "",
                    f"It is expressed in {unit}."
                    if unit and f"in {unit}" not in type_text else "",
                    level_addendum,
                    addendum,
                    f"This requirement comes from the {group_name} rule group in "
                    f"the BIDS schema"
                    + (" for derivative datasets." if is_derivative else "."),
                ]

                save_record({
                    "id": make_id("metafield", f"{group_name}_{field_name}"),
                    "knowledge_type": "MetadataRule",
                    "title": f"Sidecar field {field_name} is {level} ({group_name})",
                    "summary": (
                        f"The JSON sidecar field '{field_name}' is {level} when "
                        f"{scope_text}."
                    ),
                    "retrieval_text": " ".join(p for p in retrieval_parts if p),
                    "scope": {
                        "metadata_fields": [field_name],
                        "rule_group": group_name,
                        "level": level,
                        "dataset_types": ["derivative"] if is_derivative else ["raw"],
                    },
                    "conditions": selectors,
                    "requirements": {
                        "field": field_name,
                        "level": level,
                        "applies_when": selectors,
                        "type": type_text or None,
                    },
                    "allowed_values": values,
                    "unit": unit,
                    "severity": "error" if level == "required" else (
                        "warning" if level == "recommended" else None
                    ),
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": source_file,
                        "path": str(rule_file),
                        "section": group_name,
                        "key": field_name,
                    },
                    "raw_content": {
                        "rule_group": group_name,
                        "selectors": selectors,
                        "field": field_name,
                        "spec": field_spec,
                    },
                })

                save_relationship({
                    "source": make_id("metafield", f"{group_name}_{field_name}"),
                    "relation": "requires" if level == "required" else "applies_to",
                    "target": make_id("meta", field_name),
                    "source_reference": source_file,
                    "confidence": "explicit",
                })

            # ---- the rule group as a whole ----------------------------
            required_fields = [
                name for name, spec in fields.items() if _field_level(spec)[0] == "required"
            ]
            recommended_fields = [
                name for name, spec in fields.items() if _field_level(spec)[0] == "recommended"
            ]

            group_parts = [
                f"The BIDS sidecar rule group {group_name} applies when "
                f"{scope_text}." if when_text else
                f"The BIDS sidecar rule group {group_name} groups related "
                f"metadata fields.",
                f"It makes these fields REQUIRED: {', '.join(required_fields)}."
                if required_fields else "",
                f"It RECOMMENDS these fields: {', '.join(recommended_fields)}."
                if recommended_fields else "",
                f"All fields in the group: {', '.join(group_field_summary)}.",
            ]

            save_record({
                "id": make_id("sidecargroup", group_name),
                "knowledge_type": "MetadataRule",
                "title": f"Sidecar rule group: {group_name}",
                "summary": (
                    f"Rule group '{group_name}' defines {len(fields)} sidecar "
                    f"field(s) for files where {scope_text}."
                ),
                "retrieval_text": " ".join(p for p in group_parts if p),
                "scope": {
                    "rule_group": group_name,
                    "metadata_fields": list(fields.keys()),
                    "dataset_types": ["derivative"] if is_derivative else ["raw"],
                },
                "conditions": selectors,
                "requirements": {
                    "required": required_fields,
                    "recommended": recommended_fields,
                    "applies_when": selectors,
                },
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": source_file,
                    "path": str(rule_file),
                    "section": "rule_groups",
                    "key": group_name,
                },
                "raw_content": group_data,
            })


# ============================================================
# Tabular Data Rules
# ============================================================

def extract_tabular_rules():
    """Extract TSV column rules from rules/tabular_data/.

    A column is written either as a bare level (``age: recommended``) or as a
    mapping carrying the level. Reading the bare form as a description turns the
    word "recommended" into the column's documentation and loses the requirement
    level entirely, so every column ends up described as optional including the
    ones the standard requires.

    The column's meaning lives in objects/columns.yaml, not here, so the two are
    joined. A table also carries rules about the table as a whole (which columns
    must come first, which must be unique, whether extra columns are allowed),
    and those are what several validator messages are about.
    """
    tabular_dir = SRC_DIR / "rules" / "tabular_data"

    if not tabular_dir.exists():
        return

    for rule_file in sorted(tabular_dir.rglob("*.y*ml")):
        data = safe_load_yaml(str(rule_file))
        source_file = rel_source(rule_file)
        record_source(source_file, "tabular_rules")

        if not isinstance(data, dict):
            continue

        for table_name, table_data in data.items():
            if not isinstance(table_data, dict):
                continue

            columns = table_data.get("columns")
            if not isinstance(columns, dict):
                continue

            selectors = as_list(table_data.get("selectors"))
            initial_columns = as_list(table_data.get("initial_columns"))
            index_columns = as_list(table_data.get("index_columns"))
            additional = table_data.get("additional_columns", "not_allowed")

            when_text = explain_expressions(selectors, joiner=" and ")
            scope_text = when_text or f"the {table_name} table"

            # ---- one record per column --------------------------------
            for col_name, col_spec in columns.items():
                level, extra = _field_level(col_spec)
                definition = COLUMN_OBJECTS.get(col_name, {})

                # Column keys carry a disambiguating suffix (type__channels)
                # because the same column name means different things in
                # different tables. The name written in the file is the part
                # before the double underscore.
                header_name = definition.get("name") or col_name.split("__")[0]

                description = clean_text(definition.get("description", ""))
                addendum = clean_text(extra.get("description_addendum", ""))
                level_addendum = clean_text(extra.get("level_addendum", ""))
                type_text = describe_type(definition)
                values = allowed_values_of(definition)
                unit = (definition.get("definition") or {}).get("Units") if isinstance(
                    definition.get("definition"), dict
                ) else definition.get("unit")

                COLUMN_REQUIREMENTS[col_name].append({
                    "level": level,
                    "table": table_name,
                    "selectors": selectors,
                    "source_file": source_file,
                })

                position_note = ""
                if header_name in [str(c) for c in initial_columns]:
                    position = [str(c) for c in initial_columns].index(header_name) + 1
                    position_note = (
                        f"It must appear as column number {position} of the file; "
                        f"the order of the first columns is fixed."
                    )

                unique_note = (
                    f"Values in this column must be unique across the file."
                    if header_name in [str(c) for c in index_columns]
                    else ""
                )

                retrieval_parts = [
                    f"In the {table_name} TSV table, the column {header_name} is "
                    f"{LEVEL_PHRASING.get(level, level)}.",
                    f"The table is identified when {when_text}." if when_text else "",
                    description,
                    f"Its values are {type_text}." if type_text else "",
                    f"Allowed values: {', '.join(str(v) for v in values)}." if values else "",
                    f"It is expressed in {unit}."
                    if unit and f"in {unit}" not in type_text else "",
                    position_note,
                    unique_note,
                    level_addendum,
                    addendum,
                ]

                save_record({
                    "id": make_id("tabcol", table_name, col_name),
                    "knowledge_type": "TabularRule",
                    "title": f"Column {header_name} is {level} in {table_name}",
                    "summary": (
                        f"The '{header_name}' column of the {table_name} TSV "
                        f"table is {level}."
                    ),
                    "retrieval_text": " ".join(p for p in retrieval_parts if p),
                    "scope": {
                        "tabular_files": [table_name],
                        "columns": [header_name],
                        "level": level,
                    },
                    "conditions": selectors,
                    "requirements": {
                        "column": header_name,
                        "level": level,
                        "type": type_text or None,
                        "must_be_unique": header_name in [str(c) for c in index_columns],
                        "fixed_position": position_note or None,
                    },
                    "allowed_values": values,
                    "unit": unit,
                    "severity": "error" if level == "required" else (
                        "warning" if level == "recommended" else None
                    ),
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": source_file,
                        "path": str(rule_file),
                        "section": table_name,
                        "key": col_name,
                    },
                    "raw_content": {
                        "table": table_name,
                        "selectors": selectors,
                        "column": col_name,
                        "spec": col_spec,
                    },
                })

            # ---- the table as a whole ---------------------------------
            required_cols = [
                (COLUMN_OBJECTS.get(c, {}).get("name") or c.split("__")[0])
                for c, spec in columns.items()
                if _field_level(spec)[0] == "required"
            ]
            recommended_cols = [
                (COLUMN_OBJECTS.get(c, {}).get("name") or c.split("__")[0])
                for c, spec in columns.items()
                if _field_level(spec)[0] == "recommended"
            ]

            additional_text = {
                "allowed": "Columns beyond those listed are allowed, but each one "
                           "should be documented in the accompanying JSON sidecar.",
                "allowed_if_defined": "Columns beyond those listed are allowed only "
                                      "if they are defined in the accompanying JSON "
                                      "sidecar.",
                "not_allowed": "No columns beyond those listed are allowed.",
            }.get(str(additional), f"Additional columns: {additional}.")

            table_parts = [
                f"The {table_name} TSV table is identified when {when_text}."
                if when_text else f"The {table_name} TSV table.",
                f"Its REQUIRED columns are: {', '.join(required_cols)}."
                if required_cols else "This table has no required columns.",
                f"Its RECOMMENDED columns are: {', '.join(recommended_cols)}."
                if recommended_cols else "",
                f"The first columns must be, in this exact order: "
                f"{', '.join(str(c) for c in initial_columns)}."
                if initial_columns else "",
                f"Values must be unique in: {', '.join(str(c) for c in index_columns)}."
                if index_columns else "",
                additional_text,
            ]

            save_record({
                "id": make_id("tabtable", table_name),
                "knowledge_type": "TabularRule",
                "title": f"TSV table: {table_name}",
                "summary": (
                    f"The {table_name} TSV table defines {len(columns)} column(s); "
                    f"{len(required_cols)} required."
                ),
                "retrieval_text": " ".join(p for p in table_parts if p),
                "scope": {
                    "tabular_files": [table_name],
                    "columns": list(columns.keys()),
                },
                "conditions": selectors,
                "requirements": {
                    "required_columns": required_cols,
                    "recommended_columns": recommended_cols,
                    "initial_columns": [str(c) for c in initial_columns],
                    "index_columns": [str(c) for c in index_columns],
                    "additional_columns": str(additional),
                },
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "file": source_file,
                    "path": str(rule_file),
                    "section": "tables",
                    "key": table_name,
                },
                "raw_content": table_data,
            })


# ============================================================
# Derivative-sidecar and derivative-tabular rules
# ============================================================

def extract_derivative_rules():
    """Extract derivative-specific sidecar and tabular rules."""
    derivs_dir = SRC_DIR / "rules" / "sidecars" / "derivatives"
    
    if derivs_dir.exists():
        for rule_file in derivs_dir.rglob("*.yaml"):
            data = safe_load_yaml(str(rule_file))
            record_source(f"rules/sidecars/derivatives/{rule_file.relative_to(derivs_dir)}", "deriv_sidecar_rules")
            
            if isinstance(data, dict):
                for field_name, field_data in data.items():
                    if not isinstance(field_data, dict):
                        continue
                    save_record({
                        "id": make_id("deriv_meta", field_name),
                        "knowledge_type": "MetadataRule",
                        "title": f"Derivative Metadata: {field_name}",
                        "summary": f"Derivative-sidecar field '{field_name}' definition.",
                        "retrieval_text": f"In derivative BIDS datasets, the metadata field '{field_name}' is defined in "
                                        f"{rule_file.name} with the specification from this file.",
                        "scope": {
                            "dataset_types": ["derivative"],
                            "metadata_fields": [field_name]
                        },
                        "bids_version": BIDS_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        "source": {
                            "file": f"rules/sidecars/derivatives/{rule_file.name}",
                            "path": str(rule_file),
                            "section": "fields",
                            "key": field_name
                        },
                        "raw_content": field_data
                    })


# ============================================================
# Object definitions: metadata fields and tabular columns
#
# These two files are the specification's dictionary. Every message a validator
# prints about a sidecar field or a TSV column names something defined here, so
# without them the knowledge base can say that a field is required but not what
# the field is, what it should contain, or what a valid value looks like.
# ============================================================

def _summarise_requirements(entries, subject: str) -> tuple:
    """Turn collected rule hits into prose plus the strongest level seen."""
    if not entries:
        return "", None

    by_level = defaultdict(list)
    for entry in entries:
        context = explain_expressions(entry.get("selectors"), joiner=" and ")
        by_level[entry["level"]].append(context or entry.get("rule_group") or entry.get("table") or "")

    strongest = next((lv for lv in LEVEL_ORDER if lv in by_level), None)

    sentences = []
    for level in LEVEL_ORDER:
        contexts = [c for c in dict.fromkeys(by_level.get(level, [])) if c]
        if not contexts:
            continue
        shown = contexts[:4]
        more = len(contexts) - len(shown)
        tail = f", and {more} further context(s)" if more > 0 else ""
        sentences.append(
            f"{subject} is {level.upper()} when {'; or when '.join(shown)}{tail}."
        )

    return " ".join(sentences), strongest


def extract_metadata_objects():
    """Extract every JSON sidecar metadata field from objects/metadata.yaml.

    Must run after extract_sidecar_rules(), which collects where each field is
    required so the definition and its requirement contexts land in one record.
    """
    objs_path = SRC_DIR / "objects" / "metadata.yaml"

    if not objs_path.exists():
        return

    data = safe_load_yaml(str(objs_path))
    source_file = rel_source(objs_path)
    record_source(source_file, "metadata_field_definitions")

    if not isinstance(data, dict):
        return

    for field_key, definition in data.items():
        if not isinstance(definition, dict):
            continue

        name = definition.get("name", field_key)
        display = definition.get("display_name", name)
        description = clean_text(definition.get("description", ""))

        # The schema disambiguates a field that means different things in
        # different places by suffixing the key: EchoTime__fmap is EchoTime as it
        # applies to fieldmaps. The suffix is not part of the name written into a
        # sidecar, but it does have to stay in the id or the variants collide.
        context = field_key.split("__", 1)[1] if "__" in field_key else ""
        type_text = describe_type(definition)
        values = allowed_values_of(definition)
        unit = definition.get("unit")
        if not unit and isinstance(definition.get("anyOf"), list):
            for alt in definition["anyOf"]:
                if isinstance(alt, dict) and alt.get("unit"):
                    unit = alt["unit"]
                    break

        requirement_text, strongest = _summarise_requirements(
            FIELD_REQUIREMENTS.get(name, []),
            f"The {name} field",
        )

        if not requirement_text:
            requirement_text = (
                f"No sidecar rule group in this schema version makes {name} "
                f"required or recommended, so it is optional wherever it is "
                f"permitted."
            )

        retrieval_parts = [
            f"{name} ({display}) is a BIDS JSON sidecar metadata field.",
            f"This entry covers {name} as it applies to {context}."
            if context else "",
            description,
            f"Its value is {type_text}." if type_text else "",
            f"Allowed values: {', '.join(str(v) for v in values)}." if values else "",
            f"It is expressed in {unit}." if unit and f"in {unit}" not in type_text else "",
            requirement_text,
            f"In a JSON sidecar it is written as \"{name}\": <value>.",
        ]

        save_record({
            "id": make_id("meta", field_key),
            "knowledge_type": "MetadataRule",
            "title": (
                f"Metadata field: {name} (in {context})" if context
                else f"Metadata field: {name}"
            ),
            "summary": (
                f"'{name}' ({display}) is a BIDS JSON sidecar field"
                + (f" holding {type_text}." if type_text else ".")
            ),
            "retrieval_text": " ".join(p for p in retrieval_parts if p),
            "scope": {
                "metadata_fields": [name],
                "display_name": display,
                "level": strongest,
            },
            "requirements": {
                "field": name,
                "type": type_text or None,
                "strongest_level": strongest,
                "required_in": [
                    e["rule_group"] for e in FIELD_REQUIREMENTS.get(name, [])
                    if e["level"] == "required"
                ],
            },
            "allowed_values": values,
            "unit": unit,
            "bids_version": BIDS_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source": {
                "file": source_file,
                "path": str(objs_path),
                "section": "metadata",
                "key": field_key,
            },
            "raw_content": definition,
        })


def extract_column_objects():
    """Extract every TSV column definition from objects/columns.yaml.

    Must run after extract_tabular_rules() for the same reason as metadata
    fields: the definition says what the column means, the rules say where it is
    required, and a user needs both at once.
    """
    objs_path = SRC_DIR / "objects" / "columns.yaml"

    if not objs_path.exists():
        return

    data = safe_load_yaml(str(objs_path))
    source_file = rel_source(objs_path)
    record_source(source_file, "column_definitions")

    if not isinstance(data, dict):
        return

    for col_key, definition in data.items():
        if not isinstance(definition, dict):
            continue

        name = definition.get("name", col_key.split("__")[0])
        display = definition.get("display_name", name)
        description = clean_text(definition.get("description", ""))
        type_text = describe_type(definition)
        values = allowed_values_of(definition)

        embedded = definition.get("definition")
        unit = embedded.get("Units") if isinstance(embedded, dict) else definition.get("unit")

        requirement_text, strongest = _summarise_requirements(
            COLUMN_REQUIREMENTS.get(col_key, []),
            f"The {name} column",
        )

        tables = sorted({
            e["table"] for e in COLUMN_REQUIREMENTS.get(col_key, [])
        })

        retrieval_parts = [
            f"{name} ({display}) is a column in a BIDS TSV table.",
            description,
            f"Its values are {type_text}." if type_text else "",
            f"Allowed values: {', '.join(str(v) for v in values)}." if values else "",
            f"It is expressed in {unit}." if unit and f"in {unit}" not in type_text else "",
            f"It appears in these tables: {', '.join(tables)}." if tables else "",
            requirement_text,
        ]

        save_record({
            "id": make_id("column", col_key),
            "knowledge_type": "TabularRule",
            "title": f"TSV column: {name}",
            "summary": (
                f"'{name}' ({display}) is a BIDS TSV column"
                + (f" holding {type_text}." if type_text else ".")
            ),
            "retrieval_text": " ".join(p for p in retrieval_parts if p),
            "scope": {
                "columns": [name],
                "tabular_files": tables,
                "display_name": display,
                "level": strongest,
            },
            "requirements": {
                "column": name,
                "type": type_text or None,
                "strongest_level": strongest,
                "tables": tables,
            },
            "allowed_values": values,
            "unit": unit,
            "bids_version": BIDS_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source": {
                "file": source_file,
                "path": str(objs_path),
                "section": "columns",
                "key": col_key,
            },
            "raw_content": definition,
        })


# ============================================================
# Check extraction (validation procedures)
# ============================================================

def extract_validation_checks():
    """Extract validation check definitions from rules/checks/ directory.

    A check is the unit a validator actually reports. Each one carries an
    ``issue`` block holding the CODE the user sees in the validator output, the
    message shown next to it, and the severity. Those three live one level down
    from the check body, so the code, the message and the real severity must be
    read from ``check_data["issue"]`` rather than from the check itself.

    The code is put into the record id, title, summary and retrieval text on
    purpose. A user asking about a failed validation pastes the code and nothing
    else, so any record whose code is reachable only through nested raw content
    cannot be found by the one query that matters.
    """
    checks_dir = SRC_DIR / "rules" / "checks"

    if not checks_dir.exists():
        return

    # ---- pass 1: gather every check, grouped by the code it reports ----
    #
    # One code can be reached from several checks. IntendedFor is verified
    # separately for four kinds of reference, and the deprecated-coordinate-system
    # warning is repeated once per coordinate field. They are one answer to the
    # user, who sees one code and asks one question, so they are aggregated into
    # one record rather than several records competing for the same identifier.
    by_code = {}

    for check_file in sorted(checks_dir.rglob("*.y*ml")):
        data = safe_load_yaml(str(check_file))
        source_file = rel_source(check_file)
        record_source(source_file, "validation_checks")

        if not isinstance(data, dict):
            continue

        # The datatype a check belongs to is the file it is defined in:
        # rules/checks/func.yaml holds the checks for functional data.
        check_group = check_file.stem

        for check_name, check_data in data.items():
            if not isinstance(check_data, dict):
                continue

            # A check may inherit its issue block from a sibling via $ref.
            check_data = expand_schema_ref(check_data)

            issue = check_data.get("issue") or {}
            if not isinstance(issue, dict):
                issue = {}

            code = issue.get("code") or check_data.get("code") or check_name
            message = clean_text(issue.get("message") or check_data.get("message", ""))
            level = (issue.get("level") or check_data.get("level") or "error").lower()

            entry = by_code.setdefault(code, {
                "code": code,
                "message": message,
                "level": level,
                "variants": [],
                "groups": [],
                "source_file": source_file,
                "path": str(check_file),
                "first_name": check_name,
            })

            # The strongest severity wins: if any route to this code reports an
            # error, a user seeing the code is looking at an error.
            if level == "error":
                entry["level"] = "error"
            if message and not entry["message"]:
                entry["message"] = message
            if check_group not in entry["groups"]:
                entry["groups"].append(check_group)

            entry["variants"].append({
                "check_name": check_name,
                "check_group": check_group,
                "selectors": as_list(check_data.get("selectors")),
                "checks": as_list(check_data.get("checks") or check_data.get("condition")),
                "source_file": source_file,
                "raw": check_data,
            })

    # ---- pass 2: one record per code -----------------------------------
    for code, entry in sorted(by_code.items()):
        level = entry["level"]
        message = entry["message"]
        variants = entry["variants"]
        knowledge_type = "Warning" if level == "warning" else "Check"

        all_selectors = []
        all_assertions = []
        for variant in variants:
            for item in variant["selectors"]:
                if item not in all_selectors:
                    all_selectors.append(item)
            for item in variant["checks"]:
                if item not in all_assertions:
                    all_assertions.append(item)

        severity_text = (
            "Failing this check is reported as an ERROR, which makes the "
            "dataset invalid."
            if level == "error"
            else "Failing this check is reported as a WARNING. The dataset "
            "is still valid, but the standard recommends against it."
        )

        # Each route to the code is described on its own, because "when does
        # this fire" has a different answer per route.
        context_sentences = []
        for variant in variants:
            when_text = explain_expressions(variant["selectors"], joiner=" and ")
            must_text = explain_expressions(variant["checks"], joiner=" and ")
            if when_text and must_text:
                context_sentences.append(
                    f"When {when_text}, it requires that {must_text}."
                )
            elif must_text:
                context_sentences.append(f"It requires that {must_text}.")
            elif when_text:
                context_sentences.append(f"It is evaluated when {when_text}.")

        variant_note = (
            f"This code is reported from {len(variants)} separate checks in the "
            f"BIDS schema ({', '.join(v['check_name'] for v in variants)}), so it "
            f"can be raised in more than one situation."
            if len(variants) > 1
            else f"The check is named {entry['first_name']} in the BIDS schema."
        )

        retrieval_parts = [
            f"Validation issue code {code} ({level}).",
            message,
            " ".join(context_sentences),
            severity_text,
            variant_note,
            f"It concerns {', '.join(entry['groups'])} data.",
        ]

        record_id = make_id("check", code.lower())

        save_record({
            "id": record_id,
            "knowledge_type": knowledge_type,
            "title": f"{level.upper()} {code}",
            "summary": (
                f"{code}: {message}"
                if message
                else f"{code}: validation check '{entry['first_name']}' ({level})."
            ),
            "retrieval_text": " ".join(p for p in retrieval_parts if p),
            "scope": {
                "issue_code": code,
                "check_name": entry["first_name"],
                "check_group": entry["groups"][0] if entry["groups"] else "",
                "check_groups": entry["groups"],
                "severity": level,
            },
            "conditions": all_selectors,
            "requirements": {
                "issue_code": code,
                "message": message,
                "level": level,
                "applies_when": all_selectors,
                "asserts": all_assertions,
                "variants": [
                    {
                        "check_name": v["check_name"],
                        "selectors": v["selectors"],
                        "checks": v["checks"],
                    }
                    for v in variants
                ],
            },
            "allowed_values": None,
            "severity": level,
            "expression": " AND ".join(str(a) for a in all_assertions) or None,
            "bids_version": BIDS_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source": {
                "file": entry["source_file"],
                "path": entry["path"],
                "section": "checks",
                "key": entry["first_name"],
            },
            "raw_content": (
                variants[0]["raw"] if len(variants) == 1
                else {v["check_name"]: v["raw"] for v in variants}
            ),
        })

        # A check constrains the datatype and suffix named in its selectors.
        # Recording that lets a question about a datatype reach every check
        # that can fire on it.
        for selector in all_selectors:
            datatype_match = re.match(r'^datatype\s*==\s*[\'"](\w+)[\'"]$', str(selector).strip())
            if datatype_match:
                save_relationship({
                    "source": record_id,
                    "relation": "applies_to",
                    "target": f"dt_{datatype_match.group(1)}",
                    "source_reference": entry["source_file"],
                    "confidence": "explicit",
                })
            suffix_match = re.match(r'^suffix\s*==\s*[\'"]([\w\-]+)[\'"]$', str(selector).strip())
            if suffix_match:
                save_relationship({
                    "source": record_id,
                    "relation": "applies_to",
                    "target": f"suff_{suffix_match.group(1)}",
                    "source_reference": entry["source_file"],
                    "confidence": "explicit",
                })

        # The sidecar fields a check reads are the fields a user must look at.
        for expression in all_selectors + all_assertions:
            for field in re.findall(r'sidecar\.(\w+)', str(expression)):
                save_relationship({
                    "source": record_id,
                    "relation": "requires",
                    "target": make_id("meta", field),
                    "source_reference": entry["source_file"],
                    "confidence": "explicit",
                })


# ============================================================
# Enum Extraction
# ============================================================

def extract_enums():
    """Extract enumeration definitions from objects/enums.yaml."""
    objs_path = SRC_DIR / "objects" / "enums.yaml"
    
    if objs_path.exists():
        data = safe_load_yaml(str(objs_path))
        record_source("objects/enums.yaml", "enum_definitions")
        
        if isinstance(data, dict):
            for enum_key, enum_data in data.items():
                if not isinstance(enum_data, dict):
                    continue
                
                type_ = enum_data.get("type", "string")
                values = enum_data.get("enum", [])
                
                # Resolve values if they are $ref references
                resolved_values = []
                for v in values:
                    if isinstance(v, str) and v.startswith("$ref:"):
                        resolved_values.append(v.replace("$ref:", "").strip())
                    else:
                        resolved_values.append(v)
                
                save_record({
                    "id": make_id("enum", enum_key),
                    "knowledge_type": "Enum",
                    "title": f"Enumeration: {enum_key}",
                    "summary": f"Defines allowed values for {enum_key}.",
                    "retrieval_text": f"Enumeration '{enum_key}' defines valid values of type '{type_}'. "
                                    f"Allowed values include: {', '.join(str(v) for v in resolved_values[:10])}{'...' if len(resolved_values) > 10 else ''}.",
                    "scope": {"enum_type": type_},
                    "allowed_values": resolved_values,
                    "bids_version": BIDS_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "source": {
                        "file": "objects/enums.yaml",
                        "path": str(objs_path),
                        "section": "enums",
                        "key": enum_key
                    },
                    "raw_content": enum_data
                })


# ============================================================
# MAIN EXECUTION
# ============================================================

def main():
    print("=" * 70)
    print("BIDS Knowledge Base Extraction")
    print("=" * 70)
    print(f"Schema source: {SRC_DIR}")
    print(f"BIDS Version: {BIDS_VERSION}")
    print(f"Schema Version: {SCHEMA_VERSION}")
    print()

    if not SRC_DIR.exists():
        raise SystemExit(
            f"Raw BIDS schema not found at {SRC_DIR}.\n"
            f"Set BIDS_RULES_DIR to the directory holding objects/ and rules/."
        )

    # Reset the module-level accumulators, so calling main() twice in one
    # process produces the same output as running the script twice.
    knowledge_records.clear()
    relationship_records.clear()
    source_registry.clear()
    _used_ids.clear()
    FIELD_REQUIREMENTS.clear()
    COLUMN_REQUIREMENTS.clear()
    for key in stats:
        stats[key] = 0

    # 0. The object definitions that rules refer to by name. Loaded up front
    #    because the rule extractors join against them.
    load_schema_objects()
    print(
        f"Loaded schema objects: {len(METADATA_OBJECTS)} metadata fields, "
        f"{len(COLUMN_OBJECTS)} columns, {len(ENUM_OBJECTS)} enumerations."
    )

    # 1. Version Information
    print("[1/12] Extracting version information...")
    extract_version_info()
    
    # 2. Context & Associates
    print("[2/12] Extracting context and association definitions...")
    extract_context()
    
    # 3. Expression Tests
    print("[3/12] Extracting expression tests...")
    extract_expression_tests()
    
    # 4. Templates
    print("[4/12] Extracting filename templates...")
    extract_templates()
    
    # 5. Objects - Concepts & Definitions
    print("[5/12] Extracting objects (concepts & definitions)...")
    extract_common_principles()
    extract_entities()
    extract_entity_order()
    extract_suffixes()
    extract_datatypes()
    extract_modalities()
    extract_extensions()
    extract_metaentities()
    extract_formats()
    
    # 6. Objects - Top-level files
    print("[6/12] Extracting top-level file definitions...")
    extract_top_level_files()
    
    # 7. Rules
    print("[7/12] Extracting rules (directory layouts, errors, modalities)...")
    extract_directory_rules()
    extract_errors()
    extract_modality_rules()
    extract_file_rules()
    
    # 8. Sidecar & JSON metadata
    print("[8/12] Extracting sidecar/metadata rules...")
    extract_sidecar_rules()

    # 9. Tabular data rules
    print("[9/12] Extracting tabular data rules...")
    extract_tabular_rules()

    # 10. Object definitions for the fields and columns the rules name.
    #     Runs after the rules so each definition can state where it is
    #     required as well as what it means.
    print("[10/12] Extracting metadata field and column definitions...")
    extract_metadata_objects()
    extract_column_objects()

    # 11. Derivative rules
    print("[11/12] Extracting derivative rules...")
    extract_derivative_rules()

    # 12. Validation checks
    print("[12/12] Extracting validation checks...")
    extract_validation_checks()

    # Enums
    extract_enums()
    
    # ============================================================
    # Generate Outputs
    # ============================================================
    print()
    print("=" * 70)
    print("GENERATING OUTPUT FILES")
    print("=" * 70)
    
    # A. knowledge.jsonl
    with open(OUTPUT_KNOWLEDGE, "w", encoding="utf-8") as f:
        for rec in knowledge_records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"  -> {OUTPUT_KNOWLEDGE.name}: {len(knowledge_records)} records")
    
    # B. relationships.jsonl
    with open(OUTPUT_RELATIONSHIPS, "w", encoding="utf-8") as f:
        for rel in relationship_records:
            f.write(json.dumps(rel, ensure_ascii=False, default=str) + "\n")
    print(f"  -> {OUTPUT_RELATIONSHIPS.name}: {len(relationship_records)} relationships")
    
    # C. sources.jsonl
    with open(OUTPUT_SOURCES, "w", encoding="utf-8") as f:
        for filepath, entries in source_registry.items():
            f.write(json.dumps({
                "source_path": filepath,
                "category": entries[0]["category"] if entries else "unknown",
                "sections": [e["section"] for e in entries],
                "keys": [e["key"] for e in entries],
                "record_count": stats.get("records_created", 0),
                "relationship_count": stats.get("relationships_created", 0),
                "bids_version": BIDS_VERSION,
                "schema_version": SCHEMA_VERSION,
                "status": "processed"
            }, ensure_ascii=False) + "\n")
    print(f"  -> {OUTPUT_SOURCES.name}: {len(source_registry)} source entries")
    
    # D. processing_report.json
    report = {
        "files_processed": len(source_registry),
        "records_created": len(knowledge_records),
        "relationships_created": len(relationship_records),
        "duplicates_found": stats["duplicates_found"],
        "conflicts_found": stats["conflicts_found"],
        "inferred_records": stats["inferred_records"],
        "unclassified_sections": stats["unclassified_sections"],
        "errors": stats["errors"],
        "warnings": stats["warnings"],
        "bids_version": BIDS_VERSION,
        "schema_version": SCHEMA_VERSION,
        "output_files": [
            "knowledge.jsonl",
            "relationships.jsonl",
            "sources.jsonl",
            "KB_README.md"
        ],
        "knowledge_categories": list(set(r["knowledge_type"] for r in knowledge_records)),
        "source_categories": list(set(e["key"] for entries in source_registry.values() for e in entries))
    }
    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  -> {OUTPUT_REPORT.name}")
    
    # E. README (human-readable summary)
    categories = defaultdict(int)
    for rec in knowledge_records:
        categories[rec.get("knowledge_type", "unknown")] += 1
    
    readme = f"""# BIDS Knowledge Base – Generation Summary

## Version Information
- **BIDS Version**: {BIDS_VERSION}
- **Schema Version**: {SCHEMA_VERSION}

## What Was Extracted
The BIDS specification YAML files were processed into **{len(knowledge_records)} atomic knowledge records** and
**{len(relationship_records)} relationship edges**.

### Knowledge Categories
| Category | Count |
|----------|-------|
"""
    for cat, cnt in sorted(categories.items()):
        readme += f"| {cat} | {cnt} |\n"
    readme += f"\n**Total**: {sum(categories.values())} records\n\n"

    readme += f"""## How Knowledge Was Categorized
Each source file was classified and knowledge extracted according to its semantic content:

| Source Directory | Category | Description |
|------------------|----------|-------------|
| `meta/` | Version, Association, Template | Specification metadata, file associations, filename templates |
| `objects/` | Concept, Definition | BIDS object definitions (entities, suffixes, datatypes, modalities, etc.) |
| `rules/` | FilenameRule, DirectoryRule, EntityRule | Validation rules, directory layouts, entity orderings |
| `rules/checks/` | Check, Error, Warning | Validation checks and their severity levels |
| `rules/sidecars/` | MetadataRule | JSON sidecar field definitions and requirements |
| `rules/tabular_data/` | TabularRule | TSV/CSV column definitions and constraints |
| `rules/files/` | FileSpecification | File type, suffix, extension, and entity rules |
| `BIDS_VERSION`, `SCHEMA_VERSION` | Version | Version information for reproducibility |

## Relationships
Relationships were constructed using a controlled vocabulary:

| Relation | Description |
|----------|-------------|
| `defines` | A source defines or introduces a concept |
| `covers` | A modality covers a set of datatypes |
| `applies_to` | A rule applies to specific file types |
| `triggers` | A check triggers an error/warning |
| `maps_to_metadata` | An entity maps to a JSON metadata field |
| `templates` | A template defines filename structure |
| `validates` | An expression test validates engine behavior |
| `requires` | A rule requires certain fields or values |

## Provenance
Every knowledge record contains:
- `source.file` – Original YAML filename
- `source.path` – Full path to source file
- `source.section` – YAML section/key where information was found
- `source.key` – Specific key within the section
- `bids_version` / `schema_version` – For version-aware retrieval
- `raw_content` – The original YAML fragment for traceability

## What Could Not Be Classified
{stats["unclassified_sections"]} sections were not confidently classified. These were either:
- Empty or whitespace-only YAML sections
- Nested structures that did not represent atomic knowledge
- Metadata about the specification's construction rather than user-facing BIDS rules

## Inference
All knowledge records were marked as `confidence: explicit`. No inferred knowledge was added.
Inference is available as a future enhancement but not used in this extraction.

## Conflicts
{stats["conflicts_found"]} potential conflicts were found between source files.
Conflicting records preserve both definitions and are flagged in their metadata.

## File Descriptions
- **knowledge.jsonl** – One JSON object per line; 121+ unique atomic knowledge records
- **relationships.jsonl** – One relationship per line; graph edges connecting concepts
- **sources.jsonl** – Inventory of all source files and their contribution to the knowledge base
- **processing_report.json** – Summary statistics and quality metrics
- **KB_README.md** – This file (human-readable summary)

## Usage Notes
This knowledge base is designed for:
1. **Exact retrieval**: Entity names, suffixes, metadata fields, error codes
2. **Semantic retrieval**: Natural-language questions via `retrieval_text`
3. **Relationship traversal**: Graph queries through `relationships.jsonl`

The raw BIDS source files remain the authoritative reference.
This knowledge base is a structured, machine-readable intermediate representation.
"""
    with open(OUTPUT_README, "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"  -> {OUTPUT_README.name}")
    
    print()
    print("=" * 70)
    print(f"EXTRACTION COMPLETE")
    print(f"  Files processed: {len(source_registry)}")
    print(f"  Records created: {len(knowledge_records)}")
    print(f"  Relationships:   {len(relationship_records)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
