# Make the knowledge base able to explain a validation finding

## Summary

The knowledge base is read by an AI agent whose job is to explain a BIDS
validation finding and say how to resolve it. Measured against that job, it could
not do it: the error codes a validator prints were not findable, every required
metadata field was recorded as optional, 54 of 131 checks reported the wrong
severity, and the two schema files that define what every metadata field and TSV
column actually means were not extracted at all.

This change fixes the extraction, teaches the retriever to find a pasted issue
code deterministically, and adds an authored explanation for all 152 validation
issue codes in the schema.

| | before | after |
|---|---|---|
| Knowledge records | 1,272 | 2,488 |
| Relationship edges | 271 | 1,026 |
| Records carrying an explanation | 159 | 311 |
| Validation issue codes with an explanation | 0 | 152 of 152 |
| Checks reporting the wrong severity | 54 of 131 | 0 |
| Sidecar-field records marked required | 0 of 184 | 161 |
| TSV-column records marked required | 0 of 180 | 65 |
| Duplicate record ids | 18 | 0 |
| Correct record at rank 1 for a pasted issue code | not measured | 152 of 152 |
| Tests | 15 passing, 19 failing | 38 passing, 48 subtests |

---

## 1. Why the previous knowledge base could not answer a validation question

Each of these was verified against the generated output before anything was
changed.

### 1.1 The error codes were unreachable

A user asking about a failed validation pastes the code the validator printed and
nothing else. None of the 131 `Check` records carried its `issue.code` anywhere
the retriever weighted highly: the code sat inside nested `raw_content`, scored
at 0.8 against an id weighted 4.0. Retrieval on real codes returned confidently
wrong records:

| query | returned | correct |
|---|---|---|
| `T1W_FILE_WITH_TOO_MANY_DIMENSIONS` | `tabcol_ieegelectrodes_dimension` | no |
| `TSV_COLUMN_MISSING` | `err_hedmissingvalueinsidecar` | no |
| `MISSING_REQUIRED_ENTITY` | `err_missingsession` | no |

Only the 22 legacy `err_*` records resolved, and only because their titles
happened to contain the code.

### 1.2 Severity was wrong on 54 of 131 checks

`extract_validation_checks` read `check_data.get("level")`, but the severity
lives one level down in `check_data["issue"]["level"]`. The lookup therefore
always missed and fell back to its default of `error`. `PDT2Volumes` is a
warning in the schema and was recorded as an error.

This is the most damaging defect in the set, because the agent would tell a user
their valid dataset was invalid, or the reverse.

### 1.3 `objects/metadata.yaml` and `objects/columns.yaml` were never extracted

These two files are the specification's dictionary: 449 metadata field
definitions and 101 column definitions, with descriptions, types, units and
allowed values. Neither appeared in the knowledge base. Every validation message
about a sidecar field or a TSV column names something defined there, so the
knowledge base could not say what any of them meant.

### 1.4 The sidecar extractor named rule groups as if they were fields

The top-level keys of `rules/sidecars/*.yaml` are rule GROUPS carrying
`selectors` and `fields`. The extractor treated each group name as a field name,
so it produced a record titled `Metadata Field: MRIFuncRepetitionTime`, which is
not a metadata field, while `RepetitionTime`, `EchoTime` and `TaskName` got no
record at all. The requirement level was never read either, so all 184 records
reported their field as optional.

### 1.5 Tabular requirement levels were parsed as descriptions

A column is written either as a bare level (`age: recommended`) or as a mapping.
The bare form was read as `{"description": "recommended"}`, so the word became
the column's documentation and the level was lost. `participant_id`, which the
standard requires, was recorded as optional.

### 1.6 The enrichment file was never read

`retriever.py` loaded `knowledge.jsonl` only. Neither `build_search_blocks` nor
`Formatter` knew about `ai_enrichment`, so all 159 enrichment records produced by
`knowledge_base_enricher.py` contributed nothing at query time.

---

## 2. Extraction

### Checks are now keyed on the code the validator prints

`extract_validation_checks` reads `issue.code`, `issue.message` and
`issue.level`, and puts the code in the record id, title, summary and retrieval
text. The selectors and check expressions are rendered in plain language, so the
record says when the check applies and what it requires, not only that it exists.

Before, for `T1wFileWithTooManyDimensions`:

> This BIDS validation check verifies T1wFileWithTooManyDimensions: . Failure
> produces a error.

After:

> Validation issue code T1W_FILE_WITH_TOO_MANY_DIMENSIONS (error). `_T1w.nii[.gz]`
> files must have exactly three dimensions. When the file's suffix is T1w and the
> NIfTI header could be read, it requires that the image has exactly 3
> dimensions. Failing this check is reported as an ERROR, which makes the dataset
> invalid.

Two further corrections in the same pass:

- **`$ref` inheritance is resolved.** Six deprecation checks inherit their whole
  `issue` block from a sibling via `$ref`. Unresolved, they had no code, no
  message and no severity, so they defaulted to `error` when all six are
  warnings.
- **One code is one record.** `INTENDED_FOR` was reachable from four separate
  checks and `ELEKTA_NEUROMAG_DEPRECATED` from six. Each code now produces a
  single record that describes every route to it, rather than several records
  competing for the same identifier.

### Sidecar rules emit one record per field, with its level and its scope

One record per `(rule group, field)` pair keyed on the real field name, carrying
the requirement level and the selectors that scope it, joined to the field's
definition from `objects/metadata.yaml`. The group keeps a record of its own,
because "which fields does a BOLD file need" is a question about the group.

The requirement level is phrased so the consequence is explicit, since users
routinely treat a warning and an error alike:

> required: REQUIRED (its absence is a validation error)
> recommended: RECOMMENDED (its absence is a validation warning, not an error)

### Tabular rules read both column forms

Bare-string and mapping forms both parse, levels are read correctly, and columns
are joined to their definitions in `objects/columns.yaml`. Tables also gain a
record of their own covering required columns, fixed column order, uniqueness
and the additional-columns policy, which is what several validator messages are
about.

### The two object files are extracted, and joined to the rules

`extract_metadata_objects` and `extract_column_objects` run after the rule
extractors, so each definition can state both what it means and where it is
required. The definition and the requirement arrive in one record:

> RepetitionTime (Repetition Time) is a BIDS JSON sidecar metadata field. The
> time in seconds between the beginning of an acquisition of one volume and the
> beginning of acquisition of the volume following it (TR). [...] Its value is a
> number, in s, greater than 0. The RepetitionTime field is REQUIRED when the
> file is in the func datatype directory and the file's suffix is bold and the
> sidecar does not define VolumeTiming.

### Supporting fixes

- **Record ids are unique.** 18 ids collided, silently shadowing each other in
  the retriever's index. `make_id` folds case, so `MISCChannelCount` and
  `MiscChannelCount` became one string; the schema also suffixes context
  variants (`EchoTime__fmap`), and the same rule name appears in two files.
  `unique_id` now disambiguates deterministically and `save_record` enforces it.
- **The extractor runs.** `SRC_DIR` pointed at the output directory rather than
  at the schema, so the committed script could not be executed. It now resolves
  `../BIDS-Rules` and honours `BIDS_RULES_DIR`.
- **Source paths are POSIX.** Backslashes from the original Windows run split
  single source files into two inventory entries.
- **Markdown descriptions are flattened** for reading, with nested parentheses in
  links handled (DICOM tag URLs end in `(0020,0110)`, which a non-greedy pattern
  truncates).

---

## 3. Retrieval

- **Exact issue-code index.** A code splits into ordinary words that occur
  throughout the knowledge base, so token scoring routes it badly:
  `TSV_COLUMN_MISSING` becomes `tsv`, `column`, `missing`, which every tabular
  record contains. Codes are indexed exactly and resolved before scoring. Mixed
  case is accepted, because the schema defines `M0Type_SET_INCORRECTLY`.
  `lookup_code` and `known_codes` are exposed for callers that already know the
  code.
- **Enrichment is loaded, searched and shown.** `enriched_knowledge.jsonl` is
  merged onto the base records by id. Only the `ai_enrichment` block is taken, so
  an enrichment file generated against an older extraction cannot overwrite a
  corrected rule. The explanation is rendered under a heading that labels it as
  explanation and not specification text, so a model reading it does not quote it
  as though the standard said it.
- **Pronouns are stopwords.** A user asking for help writes "how do I fix my
  dataset". Without this, the single-letter pronoun `i` scored a whole-word match
  against the schema's `i` enum, the imaginary part of a complex image, so the
  most natural phrasing of a help request returned an unrelated record.
- **Postings lists.** Scoring was linear in the size of the knowledge base, so a
  query that can only match a few dozen records paid to be compared against all
  2,488. Candidates are narrowed to records sharing a term with the query, which
  is exactly the set that can score above zero. Three-character prefixes are
  indexed too, because a query token can match as a word prefix;
  `Scorer.MIN_PREFIX_LEN` is shared by the index and the scoring rule so they
  cannot drift apart.

| | before | after |
|---|---|---|
| Query by issue code | 278 ms | 117 ms |
| Query in prose | 219 ms | 93 ms |

The public API is unchanged: `Retriever(data_dir).retrieve(query, top_k) -> str`.

---

## 4. Explanations for all 152 issue codes

The schema says what a check tests. It does not say why the check exists, what
usually causes it to fail, or what to do next, which are the three things someone
reading a validation error needs. `curated_enrichment.py` supplies them for every
issue code: description, interpretation, why it matters, common causes, worked
examples and resolution guidance.

The content obeys four rules, in this order:

1. Never invent a requirement. Where the standard recommends, the text says
   recommends.
2. Never invent a specific value. Guidance says where to find the real value
   rather than guessing at a user's scanner.
3. Distinguish an error from a warning everywhere.
4. Prefer the cause a user can act on.

Roughly a third of the codes are the same finding applied to a different field,
and those are generated from one helper per family so the explanations cannot
drift apart: implausible timing values that are probably milliseconds, channel
counts disagreeing with `channels.tsv`, per-volume arrays of the wrong length,
mutually exclusive timing fields, gzip header privacy leaks, and the
`template_x/y/z` columns. The rest are written individually.

`build_enrichment.py` applies them offline, with no API key and no network
access, and coexists with `knowledge_base_enricher.py`: both write the same
`ai_enrichment` key, each record records which route produced it, and existing
entries are kept unless `--overwrite` is passed. The 159 model-generated
enrichments are preserved.

Example, `BOLD_NOT_4D`:

> **What this means:** A file with the bold suffix is not four-dimensional. A
> BOLD run is a time series, so it must have three spatial dimensions plus time.
>
> **Common causes:** The conversion split a run into one file per volume instead
> of stacking them. The file really is a single volume, such as a single-band
> reference image, and has been given the bold suffix. A run was aborted after
> one volume. A preprocessing step that averages across time wrote its output
> back into the raw dataset.
>
> **How to resolve:** Check how many volumes the image has. If the run was split
> into single-volume files, re-run the conversion so the volumes are stacked. If
> the file is a reference image rather than a time series, give it the suffix
> that describes what it is. If it is a derived image such as a temporal mean,
> move it into `derivatives/`.

---

## 5. Tests

`test_retriever.py` went from 15 passing and 19 failing to 38 passing with 48
subtests.

The 19 failures were the tests asserting the old behaviour, including one that
asserted `Funding`, `EthicsApprovals` and `Acknowledgements` had **no** record
and must return the no-match fallback. Those fields now have proper definitions,
so the expectations were updated to assert the field's own record rather than
some rule that happens to mention it in passing. The suite keeps its original
and good framing of driving queries from a real validation report.

New coverage:

- **`IssueCodeLookupTest`** every code in the knowledge base resolves to its own
  record, not a sample. A sample is how the mixed-case bug survived a first pass.
  Plus codes inside a sentence, and prefix shadowing.
- **`EnrichmentTest`** enrichment is merged, reaches the formatted output,
  carries its "not normative specification text" label, and no issue-code record
  lacks an explanation.
- **`PostingsIndexTest`** the index returns a ranking identical to scoring every
  record. A faster retriever that quietly returns different results is worse than
  a slow one, so the equivalence is asserted rather than assumed.
- **`KnowledgeBaseIntegrityTest`** ids are unique, severity matches the schema,
  source paths are POSIX, required fields are marked required.

---

## 6. Deliberately out of scope

- **Tool-specific knowledge.** The knowledge base describes the BIDS standard
  only. It carries nothing about any particular tool's interface, so the same
  explanation serves a user at a command line, in a graphical converter or in a
  notebook. Codes emitted by a specific validator rather than defined in the
  schema, such as `INVALID_LOCATION` or `TSV_COLUMN_MISSING`, are therefore not
  present.
- **Model-driven enrichment of the remaining records.** The 1,000-odd
  `Concept`, `Enum` and `FileSpecification` records carry extracted
  specification text but no authored explanation. `knowledge_base_enricher.py`
  remains available for that and needs `ACADEMIC_CLOUD_API_KEY` and
  `TAVILY_API_KEY`.
- **Embedding-based retrieval.** Still token scoring. The exact-code index
  removes the case where that mattered most, but synonym matching ("bold" and
  "functional MRI") would need embeddings.
- **The pre-existing en dashes** in `main-process/README.md`, `KB_README.md` and
  the `KB_README` generator template were left as they were.

---

## 7. Verifying this change

```bash
cd main-process
python extract_kb.py        # 2,488 records, 1,026 relationships, 110 sources
python build_enrichment.py  # 152 curated applied, 159 model enrichments preserved
python -m pytest test_retriever.py
```

Both scripts are idempotent: running them twice produces byte-identical
`knowledge.jsonl`, `relationships.jsonl`, `sources.jsonl` and
`enriched_knowledge.jsonl`. Verified, not assumed.

A spot check of the behaviour this change exists for:

```python
from retriever import Retriever
kb = Retriever(".")

kb.retrieve("T1W_FILE_WITH_TOO_MANY_DIMENSIONS")   # the code, pasted
kb.retrieve("is RepetitionTime required for bold files?")
kb.retrieve("my fieldmap IntendedFor points at a file that does not exist")
```

## 8. Files

| File | Change |
|---|---|
| `main-process/extract_kb.py` | Rewrote the check, sidecar and tabular extractors; added the metadata-object and column-object extractors; schema `$ref` resolution; unique ids; POSIX paths; plain-language expression rendering |
| `main-process/retriever.py` | Exact issue-code index; enrichment load, search and render; postings lists; pronoun stopwords |
| `main-process/curated_enrichment.py` | New. Authored explanations for all 152 issue codes |
| `main-process/build_enrichment.py` | New. Offline enrichment builder, additive to the model-driven one |
| `main-process/test_retriever.py` | Updated expectations; four new test classes |
| `README.md`, `main-process/README.md` | Rewritten to describe the current pipeline |
| `main-process/*.jsonl`, `processing_report.json`, `KB_README.md` | Regenerated |
