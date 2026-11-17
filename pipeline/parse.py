"""Stage 3: structured parsing.

Deterministic parsers for everything the pipeline needs later:

* the specification schema tree (YAML) including $ref targets, raw-text
  legacy issue-code comments and rule line numbers,
* the validator's TypeScript issue catalogue and issue-raising sites,
* published documentation pages (HTML/MkDocs) split into sections,
* specification / validator markdown sources split into sections,
* the bids-examples repository (dataset structures, sidecars, TSV headers),
* the bids-schema repository's versioned schema snapshots.

Nothing here creates knowledge records; it only turns source bytes into
structured Python objects plus provenance.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any

import yaml
from bs4 import BeautifulSoup

from . import config, util

# ===========================================================================
# YAML schema of the specification repository
# ===========================================================================

_KEY_LINE_RE = re.compile(r"(?m)^([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$")
_LEGACY_RE = re.compile(r"(?m)^#\s*(?:BIDS Validator Original Issue Code\s*)?#?(\d+)\s*$")


def load_yaml(path: str) -> tuple[Any, str | None]:
    text, _, problems = util.read_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if problems:
        return data, "; ".join(problems)
    return data, None


def _rule_line_index(text: str) -> dict[str, tuple[int, int | None]]:
    """Map top-level keys to (line_start, line_end) using raw text."""
    hits: list[tuple[str, int]] = []
    for m in _KEY_LINE_RE.finditer(text):
        hits.append((m.group(1), text.count("\n", 0, m.start()) + 1))
    out: dict[str, tuple[int, int | None]] = {}
    total_lines = text.count("\n") + 1
    for i, (key, line) in enumerate(hits):
        if key in out:
            continue
        end = hits[i + 1][1] - 1 if i + 1 < len(hits) else total_lines
        out[key] = (line, end)
    return out


def _legacy_index(text: str) -> dict[str, int]:
    """Legacy numeric validator issue codes come from raw comments such as
    ``# BIDS Validator Original Issue Code #95`` (or a bare ``# 95``) that sit
    directly above a rule definition.  YAML drops comments, so scan raw text."""
    out: dict[str, int] = {}
    lines = text.splitlines()
    pending: int | None = None
    for line in lines:
        m = re.match(r"^#\s*(?:BIDS Validator Original Issue Code\s*)?#?(\d+)\s*$", line)
        if m:
            pending = int(m.group(1))
            continue
        if line.startswith("#") or not line.strip():
            # keep `pending` only across adjacent comment lines
            if line.strip() == "#" or line.startswith("# "):
                continue
            continue
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if km and pending is not None:
            out[km.group(1)] = pending
        pending = None
    return out


def parse_spec_schema(spec_root: str | None = None) -> dict:
    """Parse src/schema of the specification repository.

    Returns a dict with:
      bids_version, schema_version, objects, rules (relpath -> data),
      meta, tree (merged nested tree for $ref resolution), rule_lines,
      legacy_numbers, failures.
    """
    spec_root = spec_root or os.path.join(config.SOURCES_DIR, "bids-specification")
    schema_dir = os.path.join(spec_root, config.SPEC_SCHEMA_DIR)
    out: dict = {
        "schema_dir": schema_dir,
        "bids_version": None,
        "schema_version": None,
        "objects": {},
        "rules": {},
        "meta": {},
        "rule_lines": {},   # (relpath, key) -> (line_start, line_end)
        "rule_files_raw": {},  # relpath -> raw text (for line refs of nested keys)
        "legacy_numbers": {},  # (relpath, key) -> int
        "failures": [],
        "files_parsed": [],
    }
    if not os.path.isdir(schema_dir):
        out["failures"].append({"path": schema_dir, "error": "schema directory missing"})
        return out

    for name in ("BIDS_VERSION", "SCHEMA_VERSION"):
        p = os.path.join(schema_dir, name)
        if os.path.exists(p):
            text, _, _ = util.read_text(p)
            out["bids_version" if name == "BIDS_VERSION" else "schema_version"] = text.strip()

    for dirpath, dirnames, filenames in os.walk(schema_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git",))
        for fn in sorted(filenames):
            if not fn.endswith((".yaml", ".yml")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, schema_dir).replace("\\", "/")
            data, err = load_yaml(full)
            if err and data is None:
                out["failures"].append({"path": "src/schema/" + rel, "error": err})
                continue
            if err:
                out["failures"].append({"path": "src/schema/" + rel, "warning": err})
            out["files_parsed"].append(rel)
            if rel.startswith("objects/"):
                out["objects"][rel[len("objects/"):]] = data if isinstance(data, dict) else {"_value": data}
            elif rel.startswith("meta/"):
                out["meta"][rel[len("meta/"):]] = data
            elif rel.startswith("rules/"):
                out["rules"][rel[len("rules/"):]] = data
                text, _, _ = util.read_text(full)
                out["rule_files_raw"][rel[len("rules/"):]] = text
                if isinstance(data, dict):
                    for key, (lstart, lend) in _rule_line_index(text).items():
                        out["rule_lines"][(rel[len("rules/"):], key)] = (lstart, lend)
                    for key, num in _legacy_index(text).items():
                        out["legacy_numbers"][(rel[len("rules/"):], key)] = num

    out["tree"] = build_schema_tree(out)
    return out


def build_schema_tree(parsed: dict) -> dict:
    """Merge schema files into a nested dict so dotted $ref paths resolve.

    ``rules/checks/anat.yaml`` becomes tree['rules']['checks']['anat'].
    """
    tree: dict = {"rules": {}, "objects": {}, "meta": {}}
    for rel, data in parsed["rules"].items():
        _set_dotted(tree["rules"], rel, data)
    for rel, data in parsed["objects"].items():
        _set_dotted(tree["objects"], rel, data)
    for rel, data in parsed["meta"].items():
        _set_dotted(tree["meta"], rel, data)
    return tree


def _set_dotted(root: dict, rel: str, data) -> None:
    parts = rel.replace(".yaml", "").replace(".yml", "").split("/")
    node = root
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            return
    node[parts[-1]] = data


def resolve_ref(dotted: str, parsed: dict, _depth: int = 0) -> Any:
    """Resolve a schema ``$ref`` dotted path (e.g.
    ``meta.templates.raw.mri.entities`` or
    ``rules.checks.deprecations.AnatomicalLandmarkCoordinateSystemDeprecation``)
    against the merged schema tree.  Returns None when unresolvable.
    """
    if _depth > 5:
        return None
    node: Any = parsed.get("tree", {})
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def resolve_rule(rule: dict, parsed: dict, _depth: int = 0) -> dict:
    """Return the rule with one level of ``$ref`` inheritance applied."""
    if not isinstance(rule, dict):
        return rule
    ref = rule.get("$ref")
    if ref and _depth < 5:
        target = resolve_ref(str(ref), parsed)
        if isinstance(target, dict):
            merged = dict(resolve_rule(target, parsed, _depth + 1))
            merged.update({k: v for k, v in rule.items() if k != "$ref"})
            return merged
        # unresolvable ref: keep the rule but flag it
        out = {k: v for k, v in rule.items() if k != "$ref"}
        out["_unresolved_ref"] = str(ref)
        return out
    return rule


# ===========================================================================
# Validator: issue catalogue, raise sites, docs
# ===========================================================================

def parse_validator_issue_list(validator_root: str | None = None) -> list[dict]:
    """Parse src/issues/list.ts into [{code, severity, reason, line}]."""
    validator_root = validator_root or os.path.join(config.SOURCES_DIR, "bids-validator")
    path = os.path.join(validator_root, "src", "issues", "list.ts")
    text, _, _ = util.read_text(path)
    out: list[dict] = []
    # Find `  CODE: {` and take the balanced block.
    for m in re.finditer(r"(?m)^  ([A-Z][A-Z0-9_]+): \{", text):
        code = m.group(1)
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = text[start : i + 1]
        sev = re.search(r"severity:\s*'(\w+)'", block)
        reason_m = re.search(r"reason:\s*(.*?)(?:,\s*\n\s*\}|\n\s*\})", block, re.S)
        reason = ""
        if reason_m:
            lits = re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", reason_m.group(1))
            reason = "".join((a or b) for a, b in lits)
            reason = reason.replace("\\'", "'").replace('\\"', '"')
        line = text.count("\n", 0, m.start()) + 1
        out.append(
            {
                "code": code,
                "severity": sev.group(1) if sev else None,
                "reason": reason.strip(),
                "line": line,
            }
        )
    return out


_ISSUE_CODE_RE = re.compile(r"code:\s*'([A-Z][A-Z0-9_]{2,})'")


def parse_validator_code_sites(validator_root: str | None = None) -> dict[str, list[dict]]:
    """Map every issue code literal raised in validator source to its files."""
    validator_root = validator_root or os.path.join(config.SOURCES_DIR, "bids-validator")
    sites: dict[str, list[dict]] = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(validator_root, "src")):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fn in sorted(filenames):
            if not fn.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, validator_root).replace("\\", "/")
            text, _, _ = util.read_text(full)
            for m in _ISSUE_CODE_RE.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                sites.setdefault(m.group(1), []).append(
                    {"file": rel, "line": line, "test": ".test.ts" in fn}
                )
    return sites


def parse_validator_issue_model(validator_root: str | None = None) -> dict:
    """Extract the documented Issue field table from docs/user_guide/issues.md."""
    validator_root = validator_root or os.path.join(config.SOURCES_DIR, "bids-validator")
    path = os.path.join(validator_root, "docs", "user_guide", "issues.md")
    if not os.path.exists(path):
        return {}
    text, _, _ = util.read_text(path)
    fields = []
    for m in re.finditer(r"(?m)^\|\s*`(\w+)`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$", text):
        name, ftype, desc = m.group(1), m.group(2), m.group(3)
        if name in ("code", "location", "severity") or ftype in (
            "string", "string[]", "number", "`string`", "`number`", "`string[]`",
        ):
            if desc and not desc.startswith("---") and "Description" not in desc:
                fields.append({"field": name, "type": ftype, "description": desc})
    # de-duplicate while keeping order
    seen, uniq = set(), []
    for f in fields:
        if f["field"] not in seen:
            seen.add(f["field"])
            uniq.append(f)
    return {"issue_fields": uniq, "source_path": "docs/user_guide/issues.md"}


# ===========================================================================
# Markdown section parser (spec repo + validator docs)
# ===========================================================================

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*#*\s*$")


def md_slug(heading: str) -> str:
    """Approximate MkDocs/GitHub anchor slug."""
    h = heading.strip().lower()
    h = re.sub(r"`", "", h)
    h = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", h)
    h = re.sub(r"[^\w\s-]", "", h, flags=re.U)
    h = re.sub(r"[\s_]+", "-", h)
    return h.strip("-")


def parse_md_sections(path: str) -> list[dict]:
    """Split a markdown file into heading-delimited sections."""
    text, enc, problems = util.read_text(path)
    lines = text.splitlines()
    sections: list[dict] = []
    cur = {"level": 0, "heading": os.path.basename(path), "anchor": "", "lines": [], "line_start": 1}
    in_fence = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
        m = _HEADING_RE.match(line) if not in_fence else None
        if m:
            if cur["lines"] or cur["level"] > 0:
                sections.append(_finish_md_section(cur, i - 1))
            cur = {
                "level": len(m.group(1)),
                "heading": m.group(2).strip(),
                "anchor": md_slug(m.group(2)),
                "lines": [],
                "line_start": i,
            }
        else:
            cur["lines"].append(line)
    sections.append(_finish_md_section(cur, len(lines)))
    out = []
    for s in sections:
        body = "\n".join(s.pop("lines")).strip()
        if not body and s["level"] == 0:
            continue
        s["text"] = body
        s["encoding"] = enc
        if problems:
            s["read_problems"] = problems
        out.append(s)
    return out


def _finish_md_section(cur: dict, end_line: int) -> dict:
    s = dict(cur)
    s["line_end"] = end_line
    return s


# ===========================================================================
# Published documentation pages (HTML)
# ===========================================================================

def _html_slug(heading_text: str) -> str:
    return md_slug(heading_text)


def parse_docs_page(payload: bytes, url: str) -> dict:
    """Parse one MkDocs-Rendered documentation page into sections."""
    soup = BeautifulSoup(payload, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    version = None
    m = re.search(r"Brain Imaging Data Structure\s+([0-9][0-9A-Za-z.\-+]*)", title)
    if m:
        version = m.group(1)
    article = soup.find("article") or soup.find("div", class_="md-content__inner") or soup.body
    sections: list[dict] = []
    if article is None:
        article = soup
    # Drop navigation/TOC chrome if it leaked in
    for junk in article.find_all(["nav", "script", "style"]):
        junk.decompose()

    def clean_text(node) -> str:
        return util.normalize_ws(node.get_text(" ", strip=True))

    headings = article.find_all(re.compile(r"^h[1-4]$"))
    if not headings:
        text = clean_text(article)
        if text:
            sections.append({"level": 1, "heading": title or url, "anchor": "", "text": text})
    else:
        for h in headings:
            level = int(h.name[1])
            anchor = h.get("id") or _html_slug(h.get_text(" ", strip=True))
            heading_text = h.get_text(" ", strip=True)
            parts: list[str] = []
            tables: list[list[list[str]]] = []
            codes: list[str] = []
            node = h.next_sibling
            while node is not None:
                if getattr(node, "name", None) and re.match(r"^h[1-4]$", node.name):
                    if int(node.name[1]) <= level:
                        break
                if getattr(node, "name", None) == "table":
                    rows = []
                    for tr in node.find_all("tr"):
                        cells = [util.normalize_ws(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
                        if cells:
                            rows.append(cells)
                    if rows:
                        tables.append(rows)
                    node = node.next_sibling
                    continue
                if getattr(node, "name", None) in ("pre",):
                    codes.append(node.get_text("\n", strip=False))
                    node = node.next_sibling
                    continue
                if getattr(node, "name", None) in ("p", "li", "dd", "dt", "blockquote"):
                    t = clean_text(node)
                    if t:
                        parts.append(t)
                node = node.next_sibling
            sections.append(
                {
                    "level": level,
                    "heading": heading_text,
                    "anchor": anchor,
                    "text": util.normalize_ws(" ".join(parts)),
                    "tables": tables,
                    "code_blocks": [c.strip() for c in codes if c.strip()],
                }
            )
    # Page-level metadata
    generator = soup.find("meta", attrs={"name": "generator"})
    out = {
        "url": url,
        "title": title,
        "bids_version": version,
        "generator": generator.get("content") if generator else None,
        "sections": [s for s in sections if s.get("text") or s.get("tables")],
    }
    return out


def docs_page_to_md_path(url: str) -> str | None:
    """Map a documentation URL to its source markdown path in the spec repo."""
    prefix = config.SOURCES["bids-specification-docs"]["url"]
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    if not rest.endswith(".html"):
        return None
    rest = rest[: -len(".html")]
    if rest in ("index", ""):
        rest = "index"
    return f"src/{rest}.md"


# ===========================================================================
# bids-examples
# ===========================================================================

_BIDS_FILE_RE = re.compile(
    r"^(?P<sub>sub-[a-zA-Z0-9]+)?(?:_(?P<ses>ses-[a-zA-Z0-9]+))?"
    r"(?P<ents>(?:_?[a-zA-Z0-9]+-[a-zA-Z0-9]+)*)"
    r"_(?P<suffix>[a-zA-Z0-9]+)(?P<ext>\.[a-zA-Z0-9.]+)?$"
)

MAX_FILES_PER_DATASET = 6000
MAX_JSON_BYTES = 4 * 1024 * 1024


def _parse_bids_filename(name: str) -> dict | None:
    """Parse a BIDS-style filename into entities/suffix/extension."""
    stem = name
    ext = ""
    m_ext = re.match(r"^(.*?)(\.[A-Za-z0-9.]+)?$", name)
    if m_ext:
        stem, ext = m_ext.group(1), (m_ext.group(2) or "")
    m = _BIDS_FILE_RE.match(stem)
    if not m:
        return None
    ents: dict[str, str] = {}
    for pair in re.findall(r"(?:^|_)([a-zA-Z0-9]+)-([a-zA-Z0-9]+)", m.group("ents") or ""):
        ents[pair[0]] = pair[1]
    if m.group("sub"):
        subj = m.group("sub").split("-", 1)[1]
        ents["sub"] = subj
    if m.group("ses"):
        ents["ses"] = m.group("ses").split("-", 1)[1]
    return {"entities": ents, "suffix": m.group("suffix"), "extension": ext}


def scan_examples(examples_root: str | None = None) -> dict:
    """Structured summary of every example dataset in bids-examples."""
    examples_root = examples_root or os.path.join(config.SOURCES_DIR, "bids-examples")
    out: dict = {"datasets": [], "root_files": [], "failures": [], "listing": None}

    listing_path = os.path.join(examples_root, "dataset_listing.tsv")
    if os.path.exists(listing_path):
        text, _, _ = util.read_text(listing_path)
        rows = [r.split("\t") for r in text.splitlines() if r.strip()]
        if rows:
            header = rows[0]
            out["listing"] = {"header": header, "rows": [dict(zip(header, r)) for r in rows[1:]]}

    # root-level files
    for name in sorted(os.listdir(examples_root)):
        full = os.path.join(examples_root, name)
        if os.path.isfile(full):
            out["root_files"].append(name)

    for name in sorted(os.listdir(examples_root)):
        dspath = os.path.join(examples_root, name)
        if not os.path.isdir(dspath) or name.startswith(".") or name in ("docs",):
            continue
        try:
            out["datasets"].append(_scan_dataset(dspath, name))
        except Exception as exc:  # keep going: one broken dataset must not kill the stage
            out["failures"].append({"dataset": name, "error": f"{type(exc).__name__}: {exc}"})
    return out


def _scan_dataset(dspath: str, name: str) -> dict:
    ds: dict = {
        "name": name,
        "path": name,
        "files": [],
        "file_count": 0,
        "truncated": False,
        "dataset_description": None,
        "dataset_description_error": None,
        "skip_validation": os.path.exists(os.path.join(dspath, ".SKIP_VALIDATION")),
        "validator_config": None,
        "has_readme": False,
        "participants_columns": None,
        "root_json_keys": {},
        "tsv_headers": {},
        "suffixes": {},
        "datatypes": set() if False else {},
        "derivatives": [],
        "entities_by_suffix": {},
        "parsing_problems": [],
    }
    # files
    rels: list[str] = []
    for dirpath, dirnames, filenames in os.walk(dspath):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), dspath).replace("\\", "/")
            rels.append(rel)
            if len(rels) > MAX_FILES_PER_DATASET:
                ds["truncated"] = True
                break
        if ds["truncated"]:
            break
    rels.sort()
    ds["file_count_estimate"] = len(rels)

    dd_path = os.path.join(dspath, "dataset_description.json")
    if os.path.exists(dd_path):
        try:
            if os.path.getsize(dd_path) <= MAX_JSON_BYTES:
                text, _, _ = util.read_text(dd_path)
                ds["dataset_description"] = json.loads(text)
        except (OSError, ValueError) as exc:
            ds["dataset_description_error"] = f"{type(exc).__name__}: {exc}"

    cfg_path = os.path.join(dspath, ".bids-validator-config.json")
    if os.path.exists(cfg_path):
        try:
            text, _, _ = util.read_text(cfg_path)
            ds["validator_config"] = json.loads(text)
        except ValueError as exc:
            ds["parsing_problems"].append(f"validator config: {exc}")

    ds["has_readme"] = any(
        os.path.basename(r) in ("README", "README.md", "README.rst", "README.txt") and "/" not in r
        for r in rels
    )

    # filename structure + sidecars + tsv headers
    suffixes: dict[str, int] = {}
    datatypes: dict[str, int] = {}
    ents_by_suffix: dict[str, set] = {}
    subjects: set[str] = set()
    sessions: set[str] = set()
    for rel in rels:
        base = os.path.basename(rel)
        parent = os.path.dirname(rel)
        parts = parent.split("/")
        # datatype directory detection
        datatype = None
        for p in parts:
            if p in (
                "anat", "beh", "deriv", "dwi", "eeg", "emg", "fmap", "func", "ieeg",
                "meg", "micr", "motion", "mrs", "nirs", "perf", "pet", "photo",
                "stimuli", "phenotype", "sourcedata", "code", "derivatives", "histology",
            ):
                datatype = p
        parsed_fn = _parse_bids_filename(base)
        if parsed_fn:
            sfx = parsed_fn["suffix"]
            suffixes[sfx] = suffixes.get(sfx, 0) + 1
            if datatype and datatype not in ("stimuli", "phenotype", "derivatives", "code"):
                datatypes[datatype] = datatypes.get(datatype, 0) + 1
            key = f"{datatype or '-'}/{sfx}"
            ents_by_suffix.setdefault(key, set()).update(parsed_fn["entities"].keys())
            if "sub" in parsed_fn["entities"]:
                subjects.add(parsed_fn["entities"]["sub"])
            if "ses" in parsed_fn["entities"]:
                sessions.add(parsed_fn["entities"]["ses"])
        # sidecar json key inventory (bounded)
        if base.endswith(".json") and base != "dataset_description.json":
            full = os.path.join(dspath, rel)
            try:
                if os.path.getsize(full) <= MAX_JSON_BYTES:
                    text, _, _ = util.read_text(full)
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        ds["root_json_keys"][rel] = sorted(obj.keys())[:80]
            except (OSError, ValueError) as exc:
                ds["parsing_problems"].append(f"{rel}: {type(exc).__name__}")
        # tsv headers
        if base.endswith(".tsv"):
            full = os.path.join(dspath, rel)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    header = fh.readline().rstrip("\n\r")
                ds["tsv_headers"][rel] = header.split("\t")
            except OSError:
                ds["parsing_problems"].append(f"{rel}: read error")

    if os.path.exists(os.path.join(dspath, "participants.tsv")):
        ds["participants_columns"] = ds["tsv_headers"].get("participants.tsv")
    ds["suffixes"] = dict(sorted(suffixes.items()))
    ds["datatypes"] = dict(sorted(datatypes.items()))
    ds["entities_by_suffix"] = {k: sorted(v) for k, v in sorted(ents_by_suffix.items())}
    ds["subject_count"] = len(subjects)
    ds["session_count"] = len(sessions)
    # bounded file sample for structure reference
    ds["files"] = rels[:120]

    # derivatives subdatasets
    deriv_root = os.path.join(dspath, "derivatives")
    if os.path.isdir(deriv_root):
        for sub in sorted(os.listdir(deriv_root)):
            subpath = os.path.join(deriv_root, sub)
            if not os.path.isdir(subpath):
                continue
            dd = None
            p = os.path.join(subpath, "dataset_description.json")
            if os.path.exists(p):
                try:
                    text, _, _ = util.read_text(p)
                    dd = json.loads(text)
                except ValueError:
                    dd = {"_parse_error": True}
            ds["derivatives"].append({"name": sub, "dataset_description": dd})

    # JSON-serializable copy of sets (kept simple: convert at the end)
    return ds


# ===========================================================================
# bids-schema versioned snapshots
# ===========================================================================

def _canonical_hash(obj) -> str:
    try:
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canon = repr(obj)
    return util.short_hash(canon, 12)


def collect_rule_index(schema_dir: str) -> dict:
    """Index issue codes and rule-content hashes for one schema snapshot.

    Returns {"issues": {code: {...}}, "rules": {dotted: hash}, "files": n}.
    """
    issues: dict[str, dict] = {}
    rules: dict[str, str] = {}
    nfiles = 0
    for dirpath, dirnames, filenames in os.walk(os.path.join(schema_dir, "rules")):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            if not fn.endswith((".yaml", ".yml")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, schema_dir).replace("\\", "/")
            nfiles += 1
            data, err = load_yaml(full)
            if not isinstance(data, dict):
                continue
            prefix = rel.replace(".yaml", "").replace(".yml", "").replace("/", ".")
            for key, val in data.items():
                if not isinstance(val, dict):
                    continue
                rules[f"{prefix}.{key}"] = _canonical_hash(val)
                issue = None
                if isinstance(val.get("issue"), dict):
                    issue = val["issue"]
                elif val.get("code") and val.get("message"):
                    issue = {"code": val.get("code"), "message": val.get("message"), "level": val.get("level")}
                if isinstance(issue, dict) and issue.get("code"):
                    code = issue["code"]
                    entry = {
                        "level": issue.get("level"),
                        "message": util.normalize_ws(str(issue.get("message") or "")),
                        "rule": f"{prefix}.{key}",
                    }
                    if code in issues and issues[code] != entry:
                        entry["also_defined_at"] = issues[code].get("rule")
                    issues[code] = entry
    return {"issues": issues, "rules": rules, "files": nfiles}


def parse_schema_repo(schema_root: str | None = None, spec_root: str | None = None) -> dict:
    """Parse versions/ of bids-schema plus a cross-reference to the spec repo."""
    schema_root = schema_root or os.path.join(config.SOURCES_DIR, "bids-schema")
    spec_root = spec_root or os.path.join(config.SOURCES_DIR, "bids-specification")
    out: dict = {"versions": {}, "failures": [], "cross_reference": {}}

    versions_dir = os.path.join(schema_root, "versions")
    if os.path.isdir(versions_dir):
        for name in sorted(os.listdir(versions_dir)):
            vdir = os.path.join(versions_dir, name)
            if not os.path.isdir(vdir):
                continue
            entry: dict = {"snapshot": name, "files": []}
            for fn in ("schema/BIDS_VERSION", "schema/SCHEMA_VERSION", "BIDS_VERSION_LASTEDIT"):
                p = os.path.join(vdir, fn.replace("/", os.sep))
                if os.path.exists(p):
                    text, _, _ = util.read_text(p)
                    entry[fn.split("/")[-1]] = text.strip()
            sdir = os.path.join(vdir, "schema")
            if os.path.isdir(sdir):
                try:
                    idx = collect_rule_index(sdir)
                    entry["issue_index"] = idx["issues"]
                    entry["rule_hashes"] = idx["rules"]
                    entry["rule_files"] = idx["files"]
                except Exception as exc:
                    out["failures"].append({"snapshot": name, "error": f"{type(exc).__name__}: {exc}"})
            # schema.json presence (published artifact form)
            for jf in ("schema.json", "schema_pp.json"):
                if os.path.exists(os.path.join(vdir, jf)):
                    entry["files"].append(jf)
            entry.pop("rule_hashes", None) if False else None
            out["versions"][name] = entry

    # Cross-reference: does the spec repository schema equal snapshots?
    spec_parsed = parse_spec_schema(spec_root)
    spec_rule_hashes = {}
    for rel, data in spec_parsed.get("rules", {}).items():
        if not isinstance(data, dict):
            continue
        prefix = rel.replace(".yaml", "").replace(".yml", "").replace("/", ".")
        for key, val in data.items():
            if isinstance(val, dict):
                spec_rule_hashes[f"{prefix}.{key}"] = _canonical_hash(val)
    out["cross_reference"]["spec_schema_files"] = len(spec_parsed.get("files_parsed", []))
    out["cross_reference"]["spec_rule_count"] = len(spec_rule_hashes)
    for name, entry in out["versions"].items():
        if "issue_index" in entry:
            entry["issue_count"] = len(entry["issue_index"])
    return out
