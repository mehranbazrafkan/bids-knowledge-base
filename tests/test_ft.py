"""Checks for the generated fine-tuning dataset (finetuning_dataset.jsonl)."""

from __future__ import annotations

import json
import os
import re

import pytest

from pipeline import config, finetune, util

CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


@pytest.fixture(scope="module")
def ft_lines() -> list[dict]:
    assert os.path.exists(config.FT_PATH), (
        "finetuning_dataset.jsonl is missing - run `python -m pipeline.run --force`"
    )
    records, errors = util.read_jsonl(config.FT_PATH)
    assert not errors, errors[:5]
    assert records
    return records


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    assert os.path.exists(config.FT_MANIFEST_PATH), "ft manifest missing"
    records, errors = util.read_jsonl(config.FT_MANIFEST_PATH)
    assert not errors, errors[:5]
    return records


@pytest.fixture(scope="module")
def kb_issue_codes() -> set[str]:
    records, _ = util.read_jsonl(config.KB_PATH)
    return {
        (r.get("facts") or {}).get("issue_code") or r["title"]
        for r in records
        if r["knowledge_type"] == "ValidationIssue"
    }


def test_lines_are_valid_json_objects(ft_lines):
    with open(config.FT_PATH, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            assert isinstance(obj, dict)
            assert set(obj.keys()) == {"prompt", "completion"}, (
                f"line {lineno} has keys {sorted(obj)}"
            )


def test_prompts_and_completions_are_usable(ft_lines):
    for i, rec in enumerate(ft_lines, 1):
        prompt = rec["prompt"].strip()
        completion = rec["completion"].strip()
        assert prompt and completion, f"empty field on line {i}"
        assert prompt != completion, f"prompt equals completion on line {i}"
        assert len(prompt) >= 10, f"prompt too short on line {i}"
        assert len(completion) >= 30, f"completion too short on line {i}"


def test_every_issue_code_has_at_least_one_example(ft_lines, kb_issue_codes):
    blob = "\n".join(r["prompt"] for r in ft_lines)
    missing = sorted(c for c in kb_issue_codes if not re.search(rf"\b{re.escape(c)}\b", blob))
    assert not missing, missing


def test_all_ten_issue_example_types_present(manifest):
    counts = {t: 0 for t in finetune.ISSUE_EXAMPLE_TYPES}
    for m in manifest:
        if m.get("issue_code") and m.get("example_type") in counts:
            counts[m["example_type"]] += 1
    absent = [t for t, n in counts.items() if n == 0]
    assert not absent, f"issue example types with zero examples: {absent}"


def test_manifest_is_aligned_with_dataset(ft_lines, manifest):
    assert len(manifest) == len(ft_lines)
    ids = [m["example_id"] for m in manifest]
    assert len(set(ids)) == len(ids)
    known = set(finetune.ISSUE_EXAMPLE_TYPES)
    for m in manifest:
        assert m["example_type"] in known, m
        assert m["split"] in {"train", "eval"}, m
        assert m["family"], m


def test_family_never_spans_two_splits(manifest):
    family_split: dict[str, set[str]] = {}
    for m in manifest:
        family_split.setdefault(m["family"], set()).add(m["split"])
    leaked = {f: s for f, s in family_split.items() if len(s) > 1}
    assert not leaked, list(leaked)[:10]
    splits = {m["split"] for m in manifest}
    assert splits == {"train", "eval"}


def test_synthetic_examples_are_flagged_in_prompt(ft_lines, manifest):
    for line, m in zip(ft_lines, manifest):
        if m.get("synthetic"):
            assert "synthetic" in line["prompt"].lower(), (
                f"unflagged synthetic example {m['example_id']}"
            )


def test_no_invented_issue_codes_in_dataset(ft_lines):
    kb_blob = open(config.KB_PATH, encoding="utf-8").read()
    ft_blob = json.dumps(ft_lines, ensure_ascii=False)
    unknown = sorted({t for t in CODE_TOKEN_RE.findall(ft_blob) if t not in kb_blob})
    assert not unknown, unknown[:20]


def test_completions_are_grounded_in_the_kb(ft_lines):
    kb_text = open(config.KB_PATH, encoding="utf-8").read().lower()
    token_re = re.compile(r"[a-z][a-z0-9_'-]{2,}")
    kb_tokens = set(token_re.findall(re.sub(r"\s+", " ", kb_text)))
    weak = []
    for i, rec in enumerate(ft_lines, 1):
        toks = set(token_re.findall(rec["completion"].lower()))
        if not toks:
            continue
        cov = sum(1 for t in toks if t in kb_tokens) / len(toks)
        if cov < 0.6:
            weak.append((i, round(cov, 2)))
    assert len(weak) <= max(5, int(0.02 * len(ft_lines))), weak[:10]


def test_generation_is_deterministic(tmp_path):
    ft2 = str(tmp_path / "ft.jsonl")
    man2 = str(tmp_path / "manifest.jsonl")
    stats = finetune.generate(config.KB_PATH, ft2, man2)
    assert stats["examples_written"] == sum(1 for _ in open(config.FT_PATH, encoding="utf-8"))
    assert open(ft2, "rb").read() == open(config.FT_PATH, "rb").read()
    assert open(man2, "rb").read() == open(config.FT_MANIFEST_PATH, "rb").read()
