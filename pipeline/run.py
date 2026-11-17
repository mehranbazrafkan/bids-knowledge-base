"""Pipeline orchestrator: runs the nine stages in order with per-stage
resumability (artifacts/pipeline_state.json).

Usage:
    python -m pipeline.run                # run all stages, skipping done ones
    python -m pipeline.run --force        # rerun everything
    python -m pipeline.run --stages 4,5   # rerun selected stages (1-based)
"""

from __future__ import annotations

import argparse
import sys

from . import acquire, build, config, dedupe, discovery, extract, normalize, parse, util

STATE = util.PipelineState()


def stage1_discovery(force: bool) -> dict:
    inv = discovery.run()
    summary = {
        sid: {
            "status": inv["sources"][sid].get("status"),
            "revision": (inv["sources"][sid].get("revision") or {}).get("commit"),
            "file_total": inv["sources"][sid].get("file_total"),
            "page_count": inv["sources"][sid].get("page_count"),
        }
        for sid in inv["sources"]
    }
    STATE.mark("1_discovery", "done", summary=summary)
    return summary


def stage2_acquire(force: bool) -> dict:
    rep = acquire.run(refresh=force)
    summary = {
        "docs_pages_fetched": sum(
            1 for p in rep["docs"].get("pages", []) if p.get("status") == 200 or p.get("provenance") == "cache"
        ),
        "docs_failures": len(rep["docs"].get("failures", [])),
        "repos": {r["source_id"]: r.get("commit") for r in rep["sources"]["repos"]},
        "repo_failures": len(rep["sources"].get("failures", [])),
    }
    STATE.mark("2_acquire", "done", **summary)
    return summary


def _load_revisions() -> dict:
    inv = util.read_json(config.INVENTORY_PATH)
    acq_path = config.ARTIFACTS_DIR + "/acquisition_report.json"
    revisions = {}
    for sid in config.SOURCE_IDS:
        rev = (inv["sources"].get(sid) or {}).get("revision") or {}
        revisions[sid] = rev
    try:
        acq = util.read_json(acq_path)
        for r in acq.get("sources", {}).get("repos", []):
            revisions[r["source_id"]] = {**revisions.get(r["source_id"], {}), **r}
    except (OSError, ValueError):
        pass
    return revisions


def stage3_parse(force: bool) -> dict:
    spec = parse.parse_spec_schema()
    v_issues = parse.parse_validator_issue_list()
    v_sites = parse.parse_validator_code_sites()
    ex_scan = parse.scan_examples()
    sc_parsed = parse.parse_schema_repo()
    report = {
        "spec_schema_files_parsed": len(spec["files_parsed"]),
        "spec_schema_failures": spec["failures"],
        "spec_bids_version": spec["bids_version"],
        "spec_schema_version": spec["schema_version"],
        "spec_rule_files": len(spec["rules"]),
        "spec_object_files": len(spec["objects"]),
        "validator_issue_definitions": len(v_issues),
        "validator_issue_codes_in_src": len(v_sites),
        "example_datasets": len(ex_scan["datasets"]),
        "example_failures": ex_scan["failures"],
        "schema_repo_snapshots": sorted(sc_parsed["versions"].keys()),
        "schema_repo_failures": sc_parsed["failures"],
    }
    util.write_json(config.ARTIFACTS_DIR + "/parse_report.json", report)
    STATE.mark(
        "3_parse",
        "done",
        spec_schema_files_parsed=report["spec_schema_files_parsed"],
        spec_rule_files=report["spec_rule_files"],
        validator_issue_definitions=report["validator_issue_definitions"],
        example_datasets=report["example_datasets"],
        schema_repo_snapshots=report["schema_repo_snapshots"],
    )
    return report


def stage4_extract(force: bool) -> dict:
    revisions = _load_revisions()
    records: list[dict] = []
    failures: dict[str, list] = {}
    per_source_counts: dict[str, int] = {}

    # --- source A: published documentation ---
    acq_report = util.read_json(config.ARTIFACTS_DIR + "/acquisition_report.json")
    docs_records, docs_index = extract.extract_docs(acq_report.get("docs", {}), revisions)
    records.extend(docs_records)
    per_source_counts["bids-specification-docs"] = len(docs_records)
    failures["bids-specification-docs"] = docs_index.get("failures", [])

    # --- source B: specification repository ---
    spec = parse.parse_spec_schema()
    spec_issue_codes = set()
    for r in extract.extract_spec_schema(spec, revisions):
        records.append(r)
        f = r.get("facts") or {}
        if r["knowledge_type"] == "ValidationIssue" and f.get("issue_code"):
            spec_issue_codes.add(f["issue_code"])
    n_before = len(records)
    records.extend(extract.extract_modality_rules(spec, revisions))
    records.extend(extract.extract_objects(spec, revisions))
    records.extend(extract.extract_meta(spec, revisions))
    records.extend(extract.extract_changelog(revisions))
    md_records, md_stats = extract.extract_spec_markdown(revisions, docs_index)
    records.extend(md_records)
    per_source_counts["bids-specification-repo"] = len(records) - len(docs_records)

    # --- source C: validator ---
    v_issues = parse.parse_validator_issue_list()
    v_sites = parse.parse_validator_code_sites()
    v_records = extract.extract_validator(v_issues, v_sites, {}, revisions, spec_issue_codes)
    v_records += extract.extract_validator_docs(revisions)
    v_records += extract.extract_validator_version(revisions)
    v_records += extract.extract_validator_fixtures(revisions)
    records.extend(v_records)
    per_source_counts["bids-validator"] = len(v_records)

    # --- source D: examples ---
    ex_scan = parse.scan_examples()
    ex_records = extract.extract_examples(ex_scan, revisions)
    records.extend(ex_records)
    per_source_counts["bids-examples"] = len(ex_records)
    failures["bids-examples"] = ex_scan["failures"]

    # --- source E: schema repository ---
    sc_parsed = parse.parse_schema_repo()
    sc_records = extract.extract_schema_repo(sc_parsed, revisions)
    sc_records += extract.extract_schema_repo_concepts(revisions)
    records.extend(sc_records)
    per_source_counts["bids-schema"] = len(sc_records)
    failures["bids-schema"] = sc_parsed["failures"]

    n = util.write_jsonl(config.ARTIFACTS_DIR + "/raw_records.jsonl", records)
    summary = {
        "raw_records": n,
        "per_source_counts": per_source_counts,
        "spec_markdown": md_stats,
        "failures": {k: len(v) for k, v in failures.items()},
        "failures_detail": failures,
    }
    util.write_json(config.ARTIFACTS_DIR + "/extract_report.json", summary)
    STATE.mark("4_extract", "done", raw_records=n, per_source=per_source_counts)
    return summary


def stage5_normalize(force: bool) -> dict:
    raw, errors = util.read_jsonl(config.ARTIFACTS_DIR + "/raw_records.jsonl")
    if errors:
        raise RuntimeError(f"raw record parse errors: {errors[:5]}")
    records = normalize.normalize(raw)
    n = util.write_jsonl(config.ARTIFACTS_DIR + "/normalized_records.jsonl", records)
    summary = {"normalized_records": n, "input": len(raw)}
    STATE.mark("5_normalize", "done", **summary)
    return summary


def stage6_dedupe(force: bool) -> dict:
    records, errors = util.read_jsonl(config.ARTIFACTS_DIR + "/normalized_records.jsonl")
    if errors:
        raise RuntimeError(f"normalized record parse errors: {errors[:5]}")
    final, stats = dedupe.dedupe_and_link(records)
    util.write_jsonl(config.ARTIFACTS_DIR + "/final_records.jsonl", final)
    util.write_json(config.ARTIFACTS_DIR + "/dedupe_stats.json", stats)
    STATE.mark("6_dedupe", "done", **{k: v for k, v in stats.items() if isinstance(v, int)})
    return stats


def stage7_kb(force: bool) -> dict:
    records, errors = util.read_jsonl(config.ARTIFACTS_DIR + "/final_records.jsonl")
    if errors:
        raise RuntimeError(f"final record parse errors: {errors[:5]}")
    stats = build.generate(records)
    STATE.mark("7_knowledge_base", "done", records=stats["records_written"],
               unique_issue_codes=stats["unique_issue_codes"], distinct_rules=stats["distinct_rules"])
    return stats


def stage8_ft(force: bool) -> dict:
    from . import finetune  # imported lazily (stage written later in development)

    stats = finetune.generate(config.KB_PATH, config.FT_PATH, config.FT_MANIFEST_PATH)
    STATE.mark("8_finetuning", "done", examples=stats.get("examples_written"))
    return stats


def stage9_audit(force: bool) -> dict:
    from . import audit  # imported lazily

    report = audit.run_all()
    STATE.mark("9_validation", "done", ok=report.get("ok"))
    return report


STAGES = [
    ("1", stage1_discovery),
    ("2", stage2_acquire),
    ("3", stage3_parse),
    ("4", stage4_extract),
    ("5", stage5_normalize),
    ("6", stage6_dedupe),
    ("7", stage7_kb),
    ("8", stage8_ft),
    ("9", stage9_audit),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BIDS knowledge-base pipeline")
    ap.add_argument("--force", action="store_true", help="rerun every stage")
    ap.add_argument("--stages", default="", help="comma-separated stage numbers to (re)run, e.g. 4,5,6")
    args = ap.parse_args(argv)

    only = {s.strip() for s in args.stages.split(",") if s.strip()} if args.stages else None
    if args.force:
        STATE.reset()

    results = {}
    for num, fn in STAGES:
        key = _stage_key(num, fn)
        if only is not None and num not in only:
            continue
        if only is None and STATE.done(key):
            print(f"[skip] stage {num} already done")
            continue
        print(f"[run ] stage {num}: {fn.__name__}")
        try:
            res = fn(args.force)
        except Exception as exc:  # noqa: BLE001
            STATE.mark(key, "failed", error=f"{type(exc).__name__}: {exc}")
            print(f"[fail] stage {num}: {type(exc).__name__}: {exc}")
            raise
        results[num] = res
        print(f"[ok  ] stage {num}")

    # Print a compact final summary from artifacts (actual files).
    try:
        kb_stats = util.read_json(config.ARTIFACTS_DIR + "/kb_stats.json")
        print(f"knowledge_base.jsonl records: {kb_stats['records_written']}")
        print(f"unique issue codes: {kb_stats['unique_issue_codes']}; distinct rules: {kb_stats['distinct_rules']}")
    except (OSError, ValueError):
        pass
    try:
        ft_stats = util.read_json(config.ARTIFACTS_DIR + "/ft_stats.json")
        print(f"finetuning_dataset.jsonl examples: {ft_stats.get('examples_written')}")
    except (OSError, ValueError):
        pass
    try:
        report = util.read_json(config.VALIDATION_REPORT_PATH)
        print(f"validation ok: {report.get('ok')}")
    except (OSError, ValueError):
        pass
    return 0


def _stage_key(num: str, fn) -> str:
    return {
        "1": "1_discovery", "2": "2_acquire", "3": "3_parse", "4": "4_extract",
        "5": "5_normalize", "6": "6_dedupe", "7": "7_knowledge_base",
        "8": "8_finetuning", "9": "9_validation",
    }[num]


if __name__ == "__main__":
    sys.exit(main())
