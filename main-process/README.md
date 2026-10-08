# BIDS Knowledge Engineering Pipeline

This folder contains the code and output files that turn the raw BIDS
specification schema into a structured knowledge base for the BIDS Manager AI
agent, whose job is to explain validation findings and say how to resolve them.

## Quick Directory Structure

```
main-process/
├── extract_kb.py                       # Extraction: raw schema -> JSONL knowledge base
├── curated_enrichment.py               # Authored explanations, keyed by issue code
├── build_enrichment.py                 # Applies the authored explanations (offline)
├── knowledge_base_enricher.py          # Alternative enrichment via a model + web search
├── retriever.py                        # Token-scored retriever used by the agent
├── test_retriever.py                   # Test suite
│
├── knowledge.jsonl                     # Primary KB, 2,488 atomic knowledge records
├── enriched_knowledge.jsonl            # 311 of those records plus an explanation block
├── relationships.jsonl                 # 1,026 relationship edges
├── sources.jsonl                       # Source inventory with provenance
├── processing_report.json              # Statistics and quality metrics
├── KB_README.md                        # Generated summary of what was extracted
└── bids_knowledge_extraction.ipynb     # Notebook walking through the extraction
```

The raw schema itself lives in `../BIDS-Rules/`. `extract_kb.py` reads it from
there by default; set `BIDS_RULES_DIR` to point at a different checkout.

## Rebuilding everything

```bash
python extract_kb.py        # regenerate the knowledge base from the schema
python build_enrichment.py  # apply the authored explanations
python -m pytest test_retriever.py
```

Both scripts are idempotent: running them twice produces byte-identical output.

---

## 1. What Was Extracted

The raw BIDS specification was parsed from **110 YAML source files**:

| Directory | Contents | Records |
|-----------|----------|---------|
| `BIDS_VERSION`, `SCHEMA_VERSION` | Version identifiers | 2 |
| `meta/` | Context, associations, templates, expression tests | ~67 |
| `objects/` | Entity, suffix, datatype, modality, extension, enum definitions | ~866 |
| `objects/metadata.yaml` | Definitions of all 449 JSON sidecar metadata fields | 449 |
| `objects/columns.yaml` | Definitions of all 101 TSV columns | 101 |
| `rules/` | Directory layouts, error codes, modality mappings | ~85 |
| `rules/checks/` | Validation checks, one record per issue code | 130 |
| `rules/sidecars/` | Which fields apply where, and at what requirement level | ~730 |
| `rules/files/` | File suffix, type, extension and entity rules | 180 |
| `rules/tabular_data/` | Which columns apply where, and at what requirement level | ~200 |

### Knowledge Categories and Counts

```
MetadataRule            : 1277 records
TabularRule             :  309 records
Concept                 :  240 records
Enum                    :  218 records
FileSpecification       :  180 records
Check                   :   72 records
Warning                 :   58 records
Definition              :   36 records
DirectoryRule           :   33 records
Error                   :   22 records
Template                :   17 records
Association             :   13 records
Relationship            :   11 records
Version                 :    2 records
                        -------
Total                   : 2,488 records
```

---

## 1a. The two halves of the schema, and why both are needed

The BIDS schema keeps **definitions** and **rules** in separate files, and
neither is usable alone:

- `objects/metadata.yaml` defines `RepetitionTime`: what it means, its type, its
  unit. It never says where the field applies.
- `rules/sidecars/func.yaml` says `RepetitionTime` is **required** when
  `datatype == "func"` and `suffix == "bold"`. It never says what the field is.

A user whose validator reports a missing `RepetitionTime` needs both at once, so
the extractor joins them. Each metadata field and each TSV column gets one record
carrying its definition plus every context in which the rules make it required or
recommended, and each (rule group, field) pair gets its own record stating the
requirement level and the selectors that trigger it.

The same applies to validation checks. A check's issue block carries the **code**
the user actually sees, the message shown beside it, and the severity. All three
are one level below the check body, so they have to be read from
`check["issue"]`, and a check may inherit that whole block from a sibling through
`$ref`.

---

## 2. How Knowledge Was Categorized

Each source YAML/Markdown file was examined for its actual content before deciding how to classify it. **File names do not describe what is inside them.**

### Classification Strategy

| Source File Type | Classification Approach |
|------------------|------------------------|
| `objects/*.yaml` | Every definition becomes a standalone Concept or Definition |
| `objects/enums.yaml` | Enumeration values are stored with their allowed_values |
| `meta/associations.yaml` | File association rules become Association records with selectors/target |
| `meta/templates.yaml` | Filename templates become Template records with entities/extensions |
| `meta/expression_tests.yaml` | Expression tests become Expression records for engine validation |
| `meta/context.yaml` | Namespace definitions become Concept records |
| `rules/errors.yaml` | Every error/warning code becomes an Error or Warning record with severity |
| `rules/directories.yaml` | Directory layouts become DirectoryRule records |
| `rules/modalities.yaml` | Modality→datatype mappings become Relationship records |
| `rules/checks/*.yaml` | Validation checks become Check records with conditions/severity |
| `rules/sidecars/*.yaml` | Metadata field definitions become MetadataRule records |
| `rules/files/**/*.yaml` | File specification rules become FileSpecification records |
| `rules/tabular_data/*.yaml` | Column definitions become TabularRule records |

### Atomicity Principle

Each knowledge record represents **one atomic unit of BIDS knowledge**:

- **Bad** (too much in one record): *"Functional MRI files use task and run entities, bold suffix, NIfTI format, and require JSON sidecars..."*
- **Good** (atomic): *"The bold suffix belongs to the func datatype. The task entity applies to func filenames. ..."

Exception: conditional rules like *"If X then Y is required"* are kept together because splitting them would lose meaning.

---

## 3. How Relationships Were Represented

### Relationship File (`relationships.jsonl`)

Contains 271 relationship edges in the format:

```json
{
  "source": "ctx_dataset",
  "relation": "contains",
  "target": "ctx_subjects",
  "source_reference": "meta/context.yaml",
  "confidence": "explicit"
}
```

### Controlled Vocabulary

| Relation | Meaning | Example |
|----------|---------|---------|
| `defines` | A source defines/introduces a concept | `objects/entities.yaml` → entity definitions |
| `covers` | A modality covers a set of datatypes | `mri` → `[anat, dwi, fmap, func, perf]` |
| `applies_to` | A rule applies to specific file types/conditions | Error codes with selectors |
| `maps_to_metadata` | An entity maps to a JSON metadata field | `ce` entity → `ContrastBolusIngredient` |
| `templates` | A template defines filename structure | `meta/templates.yaml` → derivative templates |
| `contains` | A container contains items | `dataset` → `subjects`, `datatypes` |
| `provides` | A context provides information | `subject` context → `sessions` |
| `triggers` | A check triggers an error/warning | `check → error_condition` |

### Relationship Resolution

The `retriever.py` module provides optional relationship-aware boosting: when a highly relevant record has relationships pointing to it, related records may be considered for inclusion.

---

## 4. What Could Not Be Classified

**Zero unclassified sections.** Every meaningful source section was represented by at least one knowledge record. All files were processed and classified.

Files that were processed but contributed no user-facing knowledge:
- Files containing only whitespace or comments
- Files with nested structures that didn't represent atomic knowledge units

---

## 5. Inference Performed

**The extracted records state nothing the schema does not.** `extract_kb.py`
joins facts that the schema keeps in separate files and renders its expressions
in plain language, but it invents no requirement and no value. When a source file
defines a suffix, the extractor records the suffix, not what datatypes happen to
use it.

Explanatory content is kept strictly separate, in the `ai_enrichment` block, and
is labelled as explanation rather than specification wherever it is shown. See
section 5a.

---

## 5a. Enrichment: explaining a finding rather than restating it

The schema says what a check tests. It does not say why the check exists, what
usually causes it to fail, or what to do next, and those are what somebody
staring at a validator error needs. Enrichment supplies them in a fixed shape:
`description`, `interpretation`, `why_it_matters`, `example_scenarios`,
`common_causes`, `resolution_guidance`, `additional_notes`, `confidence`.

There are two ways to produce it, and they coexist in one file:

| | `build_enrichment.py` | `knowledge_base_enricher.py` |
|---|---|---|
| Content from | `curated_enrichment.py`, written by hand | a language model plus web search |
| Needs | nothing | two API keys, network, credits |
| Deterministic | yes | no |
| Currently covers | all 152 validation issue codes | 159 concept and definition records |

Each enriched record records its origin in `ai_enrichment_metadata.source`, and
neither script overwrites the other's work unless asked with `--overwrite`. Run
`python build_enrichment.py --report` to see coverage without changing anything.

### The rules the authored content follows

These are the difference between an explanation that helps and one that causes
damage, so they are enforced by review rather than by code:

1. **Never invent a requirement.** If the standard recommends something, the text
   says recommends. Turning a recommendation into a requirement sends users off
   to fix a dataset that was never broken.
2. **Never invent a specific value.** Telling somebody their `RepetitionTime`
   "should be 2.0" is a guess about their scanner. The guidance says where to
   find the real value instead.
3. **Distinguish an error from a warning everywhere.** A warning is advice; an
   error means the dataset is not valid. Users routinely treat the two alike.
4. **Prefer the cause a user can act on.** "The file is malformed" is true and
   useless. "Your converter wrote milliseconds where BIDS wants seconds" is the
   same finding in a form somebody can fix.

`curated_enrichment.py` builds roughly a third of its entries through family
helpers, because that many codes are the same finding applied to a different
field: a time value that looks like milliseconds, a declared channel count that
disagrees with `channels.tsv`, an array of per-volume values that is the wrong
length. Writing those out one at a time invites drift between explanations that
ought to be identical.

---

## 6. Conflicts Found

**Zero conflicts detected.** No source files contained contradictory information about the same BIDS concept. This is expected because:
- The YAML schema is maintained coherently by the BIDS standard
- Different version files (e.g., `objects/entities.yaml` vs `rules/entities.yaml`) describe complementary aspects (definition vs. ordering), not conflicting facts

---

## 7. Source and Version Information

### Version Metadata

| Field | Value |
|-------|-------|
| **BIDS Version** | `1.11.2-dev` |
| **Schema Version** | `2.0.0-dev` |

### Released Versions Recorded

The following released BIDS versions are catalogued in the knowledge base:

```
1.11.1, 1.11.0, 1.10.1, 1.10.0, 1.9.0, 1.8.0, 1.7.0, 1.6.0,
1.5.0, 1.4.1, 1.4.0, 1.3.0, 1.2.2, 1.2.1, 1.2.0, 1.1.2,
1.1.1, 1.1.0, 1.0.2, 1.0.1, 1.0.0
```

### Source Tracing

Every knowledge record contains full provenance in its `source` field:

```json
{
  "source": {
    "file": "objects/entities.yaml",
    "path": "/full/path/to/objects/entities.yaml",
    "section": "entities",
    "key": "hemisphere"
  }
}
```

The `sources.jsonl` file maps each source YAML file to the number of knowledge records it contributed.

---

## 8. File Descriptions

### `extract_kb.py` — Extraction Engine

**Purpose**: Reads the raw BIDS schema YAML and produces the JSONL knowledge base.

**Usage** (re-run if the source YAML changes):

```bash
python extract_kb.py
BIDS_RULES_DIR=/path/to/schema python extract_kb.py   # a different checkout
```

**Outputs on each run**:
- `knowledge.jsonl` — Atomic knowledge records
- `relationships.jsonl` — Relationship edges
- `sources.jsonl` — Source inventory
- `processing_report.json` — Processing statistics
- `KB_README.md` — Generated summary

**Architecture**:
```
extract_kb.py ──reads──→ ../BIDS-Rules/
       │
       ├─→ load_schema_objects()       # metadata.yaml + columns.yaml + enums.yaml,
       │                               #   loaded first because rules join against them
       ├─→ extract_version_info()      # BIDS_VERSION, SCHEMA_VERSION, released versions
       ├─→ extract_context()           # meta/context.yaml namespaces
       ├─→ extract_expression_tests()  # meta/expression_tests.yaml
       ├─→ extract_templates()         # meta/templates.yaml raw/deriv/atlas
       ├─→ extract_common_principles() # objects/common_principles.yaml
       ├─→ extract_entities()          # objects/entities.yaml
       ├─→ extract_entity_order()      # rules/entities.yaml
       ├─→ extract_suffixes()          # objects/suffixes.yaml
       ├─→ extract_datatypes()         # objects/datatypes.yaml
       ├─→ extract_modalities()        # objects/modalities.yaml + rules/modalities.yaml
       ├─→ extract_extensions()        # objects/extensions.yaml
       ├─→ extract_metaentities()      # objects/metaentities.yaml
       ├─→ extract_formats()           # objects/formats.yaml
       ├─→ extract_top_level_files()   # objects/files.yaml
       ├─→ extract_directory_rules()   # rules/directories.yaml
       ├─→ extract_errors()            # rules/errors.yaml
       ├─→ extract_file_rules()        # rules/files/{raw,deriv,common}/*.yaml
       ├─→ extract_sidecar_rules()     # rules/sidecars/*.yaml
       │                               #   -> one record per (rule group, field),
       │                               #      and collects FIELD_REQUIREMENTS
       ├─→ extract_tabular_rules()     # rules/tabular_data/*.yaml
       │                               #   -> one record per (table, column),
       │                               #      and collects COLUMN_REQUIREMENTS
       ├─→ extract_metadata_objects()  # objects/metadata.yaml, joined with the above
       ├─→ extract_column_objects()    # objects/columns.yaml, joined with the above
       ├─→ extract_derivative_rules()  # rules/sidecars/derivatives/*.yaml
       ├─→ extract_validation_checks() # rules/checks/*.yaml, one record per issue code
       ├─→ extract_enums()             # objects/enums.yaml
       └─→ generates output JSONL files
```

Ordering matters in two places. The object definitions load first because the
rule extractors join against them, and `extract_metadata_objects` /
`extract_column_objects` run **after** the rule extractors so each definition can
state where the rules make it required as well as what it means.

### `retriever.py` — Knowledge Base Retriever

**Purpose**: Lightweight token-scored retriever designed for integration with the BIDS Manager AI Agent's `Retriever` component.

**Key Features**:
- **Zero external dependencies** (uses only Python stdlib: `json`, `re`, `os`, `pathlib`, `collections`, `typing`)
- Loads the entire knowledge base once at `__init__`, then reuses it for all queries
- Token-scored retrieval with field-aware weighting (identifiers weighted heavier than descriptions)
- **Exact lookup for validator issue codes and for named metadata fields**, which
  bypasses scoring entirely (see below)
- Merges `enriched_knowledge.jsonl` when present, and shows the explanation in
  the formatted output under a heading that marks it as explanation rather than
  specification text
- BIDS-specific identifier boosting (recognizes `bold`, `func`, `task`, `sub`, `ses`, etc.)
- Optional relationship-aware boosting via `relationships.jsonl`
- Handles malformed JSONL lines gracefully (logs warnings, skips bad lines)

**Two exact lookups sit in front of the scorer**, because the two most common
questions an explaining agent receives are the two that token scoring handles
worst:

- `_code_index` maps a validator issue code to its record. A user quoting
  `TSV_COLUMN_MISSING` is quoting one identifier, but scoring sees the ordinary
  words *tsv*, *column* and *missing*, each of which occurs in hundreds of
  records. The result is a confident answer about the wrong rule, which is worse
  than no answer.
- `_subject_index` maps a metadata field or TSV column name to the record that
  **defines** it, so "what does EffectiveEchoSpacing mean" returns the field
  rather than the checks that merely mention it. Only distinctive names are
  indexed, meaning ones with an underscore or an internal capital: BIDS has
  fields called `Type`, `Name`, `Columns` and `Units`, and a sentence containing
  the word "columns" is not a question about the field `Columns`.

Beyond the API the agent already uses, two helpers are available for callers that
already know what they are looking for:

```python
retriever.lookup_code("BOLD_NOT_4D")   # formatted record, or None
retriever.known_codes()                # every code the KB can explain
```

**API** (compatible with existing `Retriever` interface):

```python
from retriever import Retriever

retriever = Retriever("./")  # data_dir where JSONL files live
result = retriever.retrieve("What is the task entity?", top_k=3)
# result is a formatted string → directly passed to LLM by Planner
```

**Output Format**:

```
=== Knowledge Item 1 ===
ID: entity_task
Type: Concept

Source: objects/entities.yaml

BIDS Version: 1.11.2-dev
Schema Version: 2.0.0-dev

Title: Entity: Task (task)
Summary: The 'task' entity is used in BIDS filenames. Type: string, Format: label.
Description: The `task` entity in BIDS is used for: task-<label> identifies the experimental task. It is a string of format label.
Allowed Values: rest, matchingpennies, nback, ...
```

**Scoring Strategy** (applied when no exact lookup matched):

| Match Type | Score |
|-----------|-------|
| Exact query phrase in section | `× 5.0 × section_weight` |
| Token whole-word match in section | `× 2.0 × section_weight × idf` |
| Token prefix match in section | `× 0.5 × section_weight × idf` |
| Known BIDS identifier match | `+ 3.0 bonus` |

**Stop words** are filtered out of query and content before scoring. Common words
(`is`, `the`, `for`, `of`, ...) do not inflate scores. Personal pronouns are stop
words too: without that, the "I" in "how do I fix my dataset" scored a whole-word
match against the schema's `i` enumeration, which is the imaginary part of a
complex image, so the most natural phrasing of a help request returned an
unrelated record.

**Two damping rules** keep a single lucky word from carrying a record, both
needed once explanatory text entered the index. Explanations use ordinary
English, which introduces many words that are rare in the knowledge base while
saying nothing about what a record is for:

- The rarity multiplier is capped at `Scorer.MAX_IDF`. Uncapped, an unrelated
  question containing the word "configure" matched a record whose resolution text
  happened to use it, and scored nearly four times the relevance threshold on that
  one hit.
- A query of at least `Scorer.COVERAGE_MIN_TOKENS` tokens whose match rests on a
  single token is multiplied by `Scorer.SINGLE_MATCH_PENALTY`. A record that
  answers one word of a six-word question has not answered the question. Short
  queries are exempt, since a one-token query is usually an identifier.

Enrichment is indexed as **one** block rather than one per field. Scoring adds a
token's contribution once per block it appears in, and an explanation naturally
repeats its subject across its description, causes and resolution, so separate
blocks multiplied a single word several times over. That was enough to rank an
explained validation check above the definition of the very field the user had
named.

**Postings lists** narrow each query to the records that share a term with it,
rather than scoring all 2,488. A record sharing no term scores zero, so this is a
speed-up and not a change in behaviour, and `PostingsIndexTest` asserts the
ranking is identical to scoring everything. The index also holds three-character
prefixes, because a query token can match as a word prefix; `Scorer.MIN_PREFIX_LEN`
is shared by the index and the scoring rule so the two cannot drift apart.

### Measured behaviour

| | |
|---|---|
| Correct record at rank 1, over all 152 issue codes | 152 / 152 |
| Correct record still rank 1 when the code sits in a sentence | 152 / 152 |
| Answers carrying a resolution | 152 / 152 |
| Load the knowledge base (once, at construction) | ~250 ms |
| Query by issue code | ~120 ms |
| Query in prose | ~90 ms |

### `knowledge.jsonl` — Primary Knowledge Base

- **Format**: One JSON object per line (JSONL)
- **Records**: 2,488 atomic knowledge items, with **unique ids**
- **Each record contains**:
  - `id` — Stable unique identifier
  - `knowledge_type` — Category (Concept, Error, MetadataRule, etc.)
  - `title` — Short human-readable title
  - `summary` — One or two sentence summary
  - `retrieval_text` — Natural-language text for semantic retrieval
  - `source` — Traced to original YAML file/section/key, always POSIX-separated
  - `bids_version`, `schema_version` — For version-awareness
  - `raw_content` — Original YAML fragment for full traceability
  - Optional fields: `allowed_values`, `conditions`, `requirements`, `scope`, `severity`, `expression`, `unit`

Ids are unique by construction: `save_record` disambiguates a collision rather
than allowing one. Collisions happen for reasons invisible in the id itself,
since id generation folds case (the schema has both `MISCChannelCount` and
`MiscChannelCount`) and the same rule name appears in two files. A duplicate is
not cosmetic: the retriever indexes records by id, so the second silently
replaces the first and one rule becomes unanswerable.

For a validation check, `scope.issue_code` carries the code the validator prints
and `severity` carries the level its own issue block declares.

### `enriched_knowledge.jsonl` — Records plus explanations

- **Records**: 311, a subset of `knowledge.jsonl` by `id`
- Adds `ai_enrichment` (the explanation) and `ai_enrichment_metadata` (its origin)
- Merged onto the base records at load. The base record stays authoritative for
  everything else, so an enrichment file generated against an older extraction
  cannot overwrite a corrected rule; it can only fail to have an entry for it.

### `relationships.jsonl` — Graph Edges

- **Records**: 1,026 edges, of which 939 have both endpoints resolvable to records
- **Format**: `{source, relation, target, source_reference, confidence}`
- **Relations**: `defines`, `covers`, `applies_to`, `requires`, `triggers`, `maps_to_metadata`, `templates`, `contains`, `provides`

### `sources.jsonl` — Source Inventory

- **Records**: 110 source files
- **Format**: `{source_path, category, sections, keys, record_count, relationship_count, bids_version, schema_version, status}`
- **Purpose**: Maps each source YAML file to the knowledge records it contributed to

### `processing_report.json` — Audit Report

```json
{
  "files_processed": 110,
  "records_created": 2488,
  "relationships_created": 1026,
  "duplicates_found": 0,
  "conflicts_found": 0,
  "inferred_records": 0,
  "errors": 0,
  "warnings": 0,
  "bids_version": "1.11.2-dev",
  "schema_version": "2.0.0-dev",
  "knowledge_categories": [...],
  ...
}
```

### `KB_README.md` — Human-Readable Summary

A detailed human-readable summary of what was extracted, including coverage metrics, category breakdown, and relationship descriptions.

### `bids_knowledge_extraction.ipynb` — Interactive Notebook

Step-by-step Jupyter notebook documenting the extraction pipeline:
1. Load and parse YAML files
2. Extract atomic knowledge units
3. Classify into knowledge categories
4. Extract metadata rules and validation checks
5. Build relationship graph
6. Handle conflicts and duplicates
7. Generate retrieval text
8. Export JSONL and processing reports

---

## 8. Integration with BIDS Manager AI Agent

### How the Agent Uses These Files

```
User Query (e.g., "Why is my BIDS file invalid?")
    ↓
Planner routes to Retriever
    ↓
    Retriever("outputs/")  <-- data_dir pointing to this folder
    ↓
    Loads knowledge.jsonl on initialization (once)
    ↓
    retrieve("file invalid", top_k=3)
    ↓
    Returns formatted string → passed to LLM
```

### Installation (Drop-In Replacement)

Replace your existing `Retriever` with `retriever.py`:

```python
# OLD (txt/md based)
from old_retriever import Retriever
retriever = Retriever("./docs")

# NEW (JSONL knowledge base based)
from retriever import Retriever
retriever = Retriever("./outputs")  # or wherever your JSONL files live
```

**Public API remains identical**:
```python
retriever = Retriever(data_dir="./outputs")   # constructor
result = retriever.retrieve(query="...")       # same signature
result = retriever.retrieve(query="...", top_k=3)  # same signature
# result is always a string → same downstream compatibility
```

### Dependencies

```
No external packages required.
```

Only Python stdlib: `json`, `re`, `os`, `pathlib`, `collections`, `typing`, `logging`, `math`.

### Adding to Your Project

1. Copy `retriever.py` into your project
2. Copy `knowledge.jsonl`, `enriched_knowledge.jsonl`, `relationships.jsonl`,
   `sources.jsonl` and `processing_report.json` alongside it
3. Initialize with: `retriever = Retriever("./path/to/kb")`

`enriched_knowledge.jsonl` is optional. Without it the retriever behaves exactly
as before and returns the same records, just with no explanation attached, so
omitting it degrades quality rather than breaking anything.

### Extending Later

The retriever is designed for future enhancement:

- **Embedding-based retrieval**: Replace `Scorer` with a vector similarity layer (keep `load_knowledge` / `format` unchanged)
- **More relationship boosting**: The `_maybe_add_related` method is already stubbed out
- **Caching**: The in-memory index can be upgraded to a disk-based index for very large KBs
- **Filtering**: Add a `knowledge_types=["Error", "Check"]` parameter to filter by type

---

## 9. Quick Verification Commands

### Check record counts
```bash
wc -l knowledge.jsonl           # should be 2,488
wc -l enriched_knowledge.jsonl  # should be 311
wc -l relationships.jsonl       # should be 1,026
wc -l sources.jsonl             # should be 110
```

### Check enrichment coverage
```bash
python build_enrichment.py --report
# Lists any validation issue code with no authored explanation.
```

### Test the retriever
```bash
python -m pytest test_retriever.py    # the full suite
python retriever.py                   # 6 sample queries, printed
```

### Re-extract from modified source YAML files
```bash
python extract_kb.py           # reads ../BIDS-Rules/, regenerates all JSONL files
python build_enrichment.py     # re-apply explanations to the new records
```

Set `BIDS_RULES_DIR` to extract from a different schema checkout. Note that
`extract_kb.py` regenerates ids, so `build_enrichment.py` must be re-run
afterwards or enrichment will refer to records that no longer exist. The
retriever reports how many enrichment records were orphaned when it loads.

---

## 10. Notes on Retrieval Quality

### What Works Well

| Query Type | Quality | Example |
|-----------|---------|---------|
| Validator issue code | exact lookup | "T1W_FILE_WITH_TOO_MANY_DIMENSIONS" |
| Named metadata field or column | exact lookup | "what does EffectiveEchoSpacing mean" |
| Entity, suffix, datatype lookup | very good | "What is the run entity?" |
| Symptom described in prose | good | "my fieldmap IntendedFor points at a file that does not exist" |
| Requirement level of a field | good | "is RepetitionTime required for bold files?" |
| Directory structure | fair | "Where do events files go?" |

### What to Improve Next

- **Semantic embeddings.** Retrieval is token-based, so it matches words rather
  than meaning. A vector layer would handle synonyms that a user is likely to
  reach for and the schema never uses: *functional MRI* for `bold`, *gradient
  table* for `.bvec`, *anonymisation* for the privacy checks. The retriever is
  structured so `Scorer` can be replaced without touching loading or formatting.
- **Explanations beyond the validation codes.** All 152 issue codes are covered.
  The 449 metadata fields and 101 columns carry their schema definitions but not
  the "why does this matter" layer, which would help most for the fields users
  most often get wrong: the timing fields, `IntendedFor`, `PhaseEncodingDirection`.
- **Entity to datatype mapping.** Some entity relationships exist in
  `relationships.jsonl` but lack explicit records, and 87 of 1,026 edges still
  point at endpoints that are group placeholders rather than records.
- **Worked examples.** Explanations describe the fix in prose. A correct
  before-and-after snippet for the common cases would be more directly usable.

---

BIDS Version: 1.11.2-dev  
Schema Version: 2.0.0-dev  
Records: 2,488 knowledge, 311 enriched, 1,026 relationships
