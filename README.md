# bids-knowledge-base

A knowledge base about the Brain Imaging Data Structure, built so that an AI
agent can explain a validation finding and say how to resolve it.

## Layout

| Path | What it is |
|---|---|
| `BIDS-Rules/` | The raw BIDS schema, unmodified. BIDS 1.11.2-dev, schema 2.0.0-dev. |
| `main-process/` | The pipeline that turns the schema into a retrievable knowledge base, and the retriever the agent uses. |

Start with `main-process/README.md`.

## What it contains

- **2,488 knowledge records** extracted from 110 schema files, covering every
  entity, suffix, datatype, metadata field, TSV column, file rule and validation
  check the specification defines.
- **152 validation issue codes**, each with the code the validator prints, the
  conditions that trigger it, its real severity, and an authored explanation of
  what it means, what usually causes it, and how to resolve it.
- **1,026 relationship edges** between records.
- A **retriever with no dependencies beyond the Python standard library**.

## Quick start

```bash
cd main-process
python -m pytest test_retriever.py
```

```python
from retriever import Retriever

kb = Retriever("main-process")

kb.retrieve("T1W_FILE_WITH_TOO_MANY_DIMENSIONS")     # a validator issue code
kb.retrieve("what does EffectiveEchoSpacing mean")   # a metadata field
kb.retrieve("my fieldmap IntendedFor points at a file that does not exist")
```

Every call returns a formatted string, which is what the agent passes to its
model.

## Rebuilding

```bash
cd main-process
python extract_kb.py        # schema -> knowledge.jsonl (+ relationships, sources)
python build_enrichment.py  # apply the authored explanations
```

Both are idempotent and need no network access.

## Scope

The knowledge base describes **the BIDS standard only**. It deliberately carries
nothing about any particular tool's interface, so the same explanation serves a
user working from a command line, a graphical converter or a notebook.

Explanatory content is kept separate from extracted specification content, in an
`ai_enrichment` block, and is labelled as explanation wherever it is shown. The
authored text never converts a recommendation into a requirement and never
invents a specific value for a user's dataset.
