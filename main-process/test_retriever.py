#!/usr/bin/env python3
"""
Tests for the BIDS Knowledge Base Retriever (retriever.py).

Test scenarios are derived from real BIDS validation warnings/errors
produced for a dataset_description.json, e.g.:

  {"severity": "err",  "rule_id": "JSON_SCHEMA_VALIDATION_ERROR",
   "message": "Authors must be array", "field": "Authors"}
  {"severity": "warn", "rule_id": "JSON_KEY_RECOMMENDED",
   "message": "missing recommended field 'HEDVersion'", "field": "HEDVersion"}
  {"severity": "warn", "rule_id": "bidsmgr.todo_placeholder",
   "message": "field 'License' contains a TODO placeholder", "field": "License"}

For each warning/error the retriever must return the relevant BIDS
knowledge item(s) (by their knowledge.jsonl id), and for fields that have
no knowledge-base record it must degrade gracefully (clean "no relevant
knowledge" message) instead of returning unrelated noise or crashing.

Run with:
    python test_retriever.py
or:
    python -m unittest test_retriever -v
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from retriever import Retriever

KB_DIR = Path(__file__).parent
KB = Retriever(str(KB_DIR))

NO_MATCH_PREFIX = "No relevant BIDS knowledge found"


def item_ids(result: str) -> list:
    """Extract the knowledge item IDs present in a formatted result."""
    return re.findall(r"^ID: (\S+)", result, flags=re.MULTILINE)


def num_items(result: str) -> int:
    return len(re.findall(r"^=== Knowledge Item ", result, flags=re.MULTILINE))


# ---------------------------------------------------------------------------
# Validation report entries (subset actually produced for dataset_description.json)
#
# A finding always names a field, and the answer a user wants is what that field
# is and what it should contain. The knowledge base therefore has to carry a
# record per metadata field, not only per rule. These cases assert exactly that:
# the field's own definition must come back, rather than some rule that happens
# to mention the field in passing.
# ---------------------------------------------------------------------------

# rule_id / field / short query -> expected knowledge record id
EXPECTED_KNOWLEDGE = [
    # JSON_SCHEMA_VALIDATION_ERROR: field must be an array
    ("JSON_SCHEMA_VALIDATION_ERROR", "Authors", "Authors must be array",
     "meta_authors"),
    ("JSON_SCHEMA_VALIDATION_ERROR", "ReferencesAndLinks", "ReferencesAndLinks must be array",
     "meta_referencesandlinks"),
    ("JSON_SCHEMA_VALIDATION_ERROR", "Funding", "Funding must be array",
     "meta_funding"),
    ("JSON_SCHEMA_VALIDATION_ERROR", "EthicsApprovals", "EthicsApprovals must be array",
     "meta_ethicsapprovals"),
    # JSON_KEY_RECOMMENDED: recommended field missing
    ("JSON_KEY_RECOMMENDED", "HEDVersion", "missing recommended field HEDVersion",
     "meta_hedversion"),
    ("JSON_KEY_RECOMMENDED", "SourceDatasets", "missing recommended field SourceDatasets",
     "meta_sourcedatasets"),
    # bidsmgr.todo_placeholder: field contains a TODO placeholder
    ("bidsmgr.todo_placeholder", "License", "field License contains a TODO placeholder",
     "meta_license"),
    ("bidsmgr.todo_placeholder", "HowToAcknowledge",
     "field HowToAcknowledge contains a TODO placeholder",
     "meta_howtoacknowledge"),
    ("bidsmgr.todo_placeholder", "Acknowledgements",
     "field Acknowledgements contains a TODO placeholder",
     "meta_acknowledgements"),
]

# Queries about subjects the BIDS specification does not cover. The retriever
# must say so rather than returning its least-bad match, because a confident
# answer to a question the knowledge base cannot answer is worse than no answer.
OUT_OF_SCOPE_QUERIES = [
    ("unrelated science", "quantum chromodynamics lattice gauge theory"),
    ("unrelated software", "how do I configure a kubernetes ingress controller"),
]


class RetrieverKnowledgeCoverageTest(unittest.TestCase):
    """Each warning/error must surface the matching BIDS knowledge item."""

    def test_covered_warnings_and_errors(self):
        for rule_id, field, query, expected_id in EXPECTED_KNOWLEDGE:
            with self.subTest(rule_id=rule_id, field=field):
                result = KB.retrieve(query, top_k=3)
                self.assertIn(
                    f"ID: {expected_id}",
                    result,
                    f"query {query!r} should retrieve {expected_id}, "
                    f"got: {item_ids(result)}",
                )

    def test_out_of_scope_queries_return_clean_fallback(self):
        for label, query in OUT_OF_SCOPE_QUERIES:
            with self.subTest(label=label):
                result = KB.retrieve(query, top_k=3)
                self.assertTrue(
                    result.startswith(NO_MATCH_PREFIX),
                    f"query {query!r} should return the no-match fallback, got: "
                    f"{item_ids(result)}",
                )

    def test_json_schema_validation_error_rule(self):
        result = KB.retrieve("JSON_SCHEMA_VALIDATION_ERROR", top_k=2)
        self.assertIn("ID: err_json_schema_validation_error", result)

    def test_hed_version_rule_code(self):
        result = KB.retrieve("HED_VERSION_NOT_DEFINED", top_k=2)
        self.assertIn("ID: err_hed_version_not_defined", result)


# The 10 demonstration queries, one per validation warning/error, phrased
# the way a Planner would ask the retriever. `None` means the knowledge
# base has no record for that field, so the retriever must return the
# clean "no relevant knowledge" fallback.
DEMO_QUERIES = [
    ("warn JSON_KEY_RECOMMENDED HEDVersion",
     "What is the HEDVersion field used for?",
     ["meta_hedversion"]),
    ("warn JSON_KEY_RECOMMENDED SourceDatasets",
     "What is SourceDatasets?",
     ["meta_sourcedatasets"]),
    ("err JSON_SCHEMA_VALIDATION_ERROR Authors",
     "What is the Authors field?",
     ["meta_authors"]),
    ("err JSON_SCHEMA_VALIDATION_ERROR Funding",
     "What is the Funding field?",
     ["meta_funding"]),
    ("err JSON_SCHEMA_VALIDATION_ERROR EthicsApprovals",
     "What is EthicsApprovals?",
     ["meta_ethicsapprovals"]),
    ("err JSON_SCHEMA_VALIDATION_ERROR ReferencesAndLinks",
     "What is ReferencesAndLinks?",
     ["meta_referencesandlinks"]),
    ("warn bidsmgr.todo_placeholder License",
     "What is the License field?",
     ["meta_license"]),
    ("warn bidsmgr.todo_placeholder Authors",
     "What should the Authors field contain?",
     ["meta_authors"]),
    ("warn bidsmgr.todo_placeholder Acknowledgements",
     "What is Acknowledgements?",
     ["meta_acknowledgements"]),
    ("warn bidsmgr.todo_placeholder HowToAcknowledge",
     "What is HowToAcknowledge?",
     ["meta_howtoacknowledge"]),
]


class DemoQueriesTest(unittest.TestCase):
    """The 10 queries derived from the dataset_description.json report."""

    def test_ten_queries_from_validation_report(self):
        for label, query, expected_ids in DEMO_QUERIES:
            with self.subTest(label=label, query=query):
                result = KB.retrieve(query, top_k=3)
                if expected_ids is None:
                    self.assertTrue(
                        result.startswith(NO_MATCH_PREFIX),
                        f"query {query!r} should fall back, got: {item_ids(result)}",
                    )
                else:
                    for expected_id in expected_ids:
                        self.assertIn(
                            f"ID: {expected_id}",
                            result,
                            f"query {query!r} should retrieve {expected_id}, got: {item_ids(result)}",
                        )


class IssueCodeLookupTest(unittest.TestCase):
    """A pasted validator issue code must reach its own record, exactly.

    This is the single most common query an agent explaining a validation report
    will ever receive, and it is the one token scoring handles worst: a code
    splits into ordinary words that occur in hundreds of records, so scoring
    alone returns a confident answer about a different rule.
    """

    # One code per source, chosen because their word forms collide with a lot of
    # other content and so would be misrouted by scoring alone.
    SAMPLE_CODES = [
        "T1W_FILE_WITH_TOO_MANY_DIMENSIONS",
        "BOLD_NOT_4D",
        "EVENTS_TSV_MISSING",
        "DWI_MISSING_BVEC",
        "INTENDED_FOR",
        "MISSING_SESSION",
        "NIFTI_TOO_SMALL",
        "REPETITION_TIME_MISMATCH",
        "SLICETIMING_VALUES_GREATER_THAN_REPETITION_TIME",
        "ELEKTA_NEUROMAG_DEPRECATED",
    ]

    def test_every_sample_code_resolves_to_its_own_record(self):
        for code in self.SAMPLE_CODES:
            with self.subTest(code=code):
                result = KB.retrieve(code, top_k=1)
                self.assertIn(
                    f"Title: ", result,
                    f"code {code} returned no record",
                )
                self.assertIn(
                    code, result,
                    f"code {code} did not return the record carrying that code, "
                    f"got: {item_ids(result)}",
                )

    def test_code_embedded_in_a_sentence_is_found(self):
        result = KB.retrieve(
            "my validator says BOLD_NOT_4D, what do I do about it?", top_k=1
        )
        self.assertIn("BOLD_NOT_4D", result)

    def test_lookup_code_helper(self):
        found = KB.lookup_code("BOLD_NOT_4D")
        self.assertIsNotNone(found)
        self.assertIn("BOLD_NOT_4D", found)
        self.assertIsNone(KB.lookup_code("NOT_A_REAL_BIDS_CODE"))

    def test_known_codes_is_non_trivial(self):
        codes = KB.known_codes()
        self.assertGreater(len(codes), 100)
        self.assertEqual(codes, sorted(codes))

    def test_every_code_in_the_knowledge_base_resolves_to_itself(self):
        # The whole set, not a sample. Pasting a code is the commonest query an
        # explaining agent receives, so a code that does not resolve is a
        # question the agent answers with the wrong rule.
        wrong = []
        for code in KB.known_codes():
            ids = item_ids(KB.retrieve(code, top_k=1))
            expected = KB.records[KB._code_index[code]].id
            if not ids or ids[0] != expected:
                wrong.append((code, ids))
        self.assertEqual(wrong, [], f"codes not resolving to their own record: {wrong[:5]}")

    def test_mixed_case_code_is_recognised(self):
        # Not every code is upper case: the schema defines M0Type_SET_INCORRECTLY.
        # An upper-case-only pattern skipped it, and scoring then preferred the
        # longer codes sharing its prefix.
        result = KB.retrieve("M0Type_SET_INCORRECTLY", top_k=1)
        self.assertIn("ID: check_m0type_set_incorrectly\n", result + "\n")

    def test_code_prefix_does_not_shadow_the_exact_code(self):
        # Several codes are prefixes of longer ones. The exact code must win.
        for shorter in ("M0Type_SET_INCORRECTLY", "PET_FRAME_CONSISTENCY"):
            with self.subTest(code=shorter):
                ids = item_ids(KB.retrieve(shorter, top_k=1))
                expected = KB.records[KB._code_index[shorter.upper()]].id
                self.assertEqual(ids[:1], [expected])


class FieldDefinitionLookupTest(unittest.TestCase):
    """Naming a metadata field or column must return that field's definition.

    A validation finding always names a field, so "what is <field>" is the second
    most common question an explaining agent receives. Without a direct lookup,
    the validation checks that merely mention a field outrank the field's own
    definition, because they repeat its name in more places.
    """

    NAMED_FIELDS = [
        ("what does EffectiveEchoSpacing mean", "meta_effectiveechospacing"),
        ("what is RepetitionTime", "meta_repetitiontime"),
        ("what is SliceTiming", "meta_slicetiming"),
        ("PowerLineFrequency for EEG", "meta_powerlinefrequency"),
        ("what is participant_id", "column_participant_id"),
        ("explain IntendedFor", "meta_intendedfor"),
    ]

    def test_named_field_returns_its_definition(self):
        for query, expected_id in self.NAMED_FIELDS:
            with self.subTest(query=query):
                result = KB.retrieve(query, top_k=2)
                self.assertIn(
                    f"ID: {expected_id}", result,
                    f"query {query!r} should surface {expected_id}, "
                    f"got: {item_ids(result)}",
                )

    def test_ordinary_words_that_are_field_names_do_not_hijack(self):
        # BIDS defines fields called Type, Name, Columns and Units. A sentence
        # containing one of those words is not a question about that field.
        result = KB.retrieve("which columns are required in participants.tsv", top_k=2)
        self.assertNotIn("ID: meta_columns", result)
        result = KB.retrieve("channels.tsv type column allowed values", top_k=2)
        self.assertNotIn("ID: meta_type", result)

    def test_symptom_queries_still_reach_the_checks(self):
        # Promotion must not displace the checks when a user describes a failure
        # rather than asking for a definition.
        result = KB.retrieve("how do I fix a missing events file", top_k=2)
        self.assertIn("ID: check_events_tsv_missing", result)


class EnrichmentTest(unittest.TestCase):
    """Explanations must reach the formatted output, or they serve no purpose."""

    def test_enrichment_is_merged_onto_records(self):
        enriched = [r for r in KB.records if r.enrichment]
        self.assertGreater(
            len(enriched), 100,
            "no enrichment was merged; check enriched_knowledge.jsonl",
        )

    def test_enrichment_appears_in_formatted_output(self):
        result = KB.retrieve("BOLD_NOT_4D", top_k=1)
        self.assertIn("How to resolve:", result)
        self.assertIn("Common causes:", result)

    def test_enrichment_is_labelled_as_explanation(self):
        # The explanatory text is not specification text, and the output has to
        # say so, or a model reading it will quote it as though the standard did.
        result = KB.retrieve("BOLD_NOT_4D", top_k=1)
        self.assertIn("not normative specification text", result)

    def test_every_issue_code_record_carries_an_explanation(self):
        missing = [
            record.id
            for record in KB.records
            if (record.scope or {}).get("issue_code") and not record.enrichment
        ]
        self.assertEqual(
            missing, [],
            f"{len(missing)} validation issue record(s) have no explanation",
        )


class PostingsIndexTest(unittest.TestCase):
    """The postings index must make scoring faster without changing it.

    Scoring is linear in the size of the knowledge base, which is the one real
    cost of holding more records. The index narrows each query to the records
    that share a term with it. That is only legitimate if it is invisible in the
    results, so the equivalence is asserted rather than assumed.
    """

    QUERIES = [
        "how do I fix a missing events file",
        "why is my bold not 4d",
        "what does EffectiveEchoSpacing mean",
        "which columns are required in participants.tsv",
        "is RepetitionTime required for bold files",
        "the validator says my T1w has too many dimensions",
        "channels.tsv type column allowed values",
        "quantum chromodynamics lattice gauge theory",
        # A token that matches only as a prefix, which is the case the index
        # would break if its prefix keys and the scorer's rule disagreed.
        "diffus gradient direction",
    ]

    @staticmethod
    def _score_every_record(kb, query):
        """Reference implementation: score all records, ignoring the index."""
        from retriever import Scorer
        scored = [
            (Scorer.score(query, kb.records[i], kb.blocks[i], kb.identities[i], kb._idf), i)
            for i in range(len(kb.records))
        ]
        scored = [(s, i) for s, i in scored if s > 0]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored

    def test_index_returns_the_same_ranking_as_scoring_everything(self):
        for query in self.QUERIES:
            with self.subTest(query=query):
                fast = [i for _s, i in KB._score_all(query)]
                slow = [i for _s, i in self._score_every_record(KB, query)]
                self.assertEqual(
                    fast, slow,
                    f"the postings index changed the ranking for {query!r}",
                )

    def test_prefix_keys_match_the_scorer_rule(self):
        from retriever import Scorer
        self.assertEqual(
            Scorer.MIN_PREFIX_LEN, 3,
            "the scorer's prefix rule and the index's prefix keys are the same "
            "length by construction; changing one requires changing the other",
        )


class KnowledgeBaseIntegrityTest(unittest.TestCase):
    """Properties the generated knowledge base must hold."""

    def test_record_ids_are_unique(self):
        seen = {}
        duplicates = []
        for record in KB.records:
            if record.id in seen:
                duplicates.append(record.id)
            seen[record.id] = True
        self.assertEqual(
            duplicates, [],
            f"duplicate record ids silently shadow each other: {duplicates[:5]}",
        )

    def test_check_severity_matches_the_schema(self):
        # The severity a record reports must be the one its source declares.
        # Reporting an error as a warning tells a user their invalid dataset is
        # merely untidy.
        wrong = []
        for record in KB.records:
            raw = record.raw_content
            if not isinstance(raw, dict):
                continue
            issue = raw.get("issue")
            if isinstance(issue, dict) and issue.get("level"):
                if issue["level"] != record.severity:
                    wrong.append((record.id, issue["level"], record.severity))
        self.assertEqual(wrong, [], f"severity misreported: {wrong[:5]}")

    def test_source_paths_are_posix(self):
        # A backslash in a source path splits one source file into two entries
        # in the inventory, depending on the machine the extraction ran on.
        offenders = [
            record.id for record in KB.records
            if "\\" in str(record.source.get("file", ""))
        ]
        self.assertEqual(offenders, [], f"non-POSIX source paths: {offenders[:5]}")

    def test_required_metadata_fields_are_marked_required(self):
        # Every metadata field was once reported as optional, because the
        # requirement level lives in the rules and was never read.
        required = [
            record for record in KB.records
            if record.knowledge_type == "MetadataRule"
            and isinstance(record.requirements, dict)
            and record.requirements.get("level") == "required"
        ]
        self.assertGreater(
            len(required), 50,
            "almost nothing is marked required; the level is not being read",
        )

    def test_known_required_sidecar_field_is_findable(self):
        result = KB.retrieve("is RepetitionTime required for bold files?", top_k=3)
        self.assertIn("RepetitionTime", result)
        self.assertIn("required", result.lower())


class RetrieverBehaviourTest(unittest.TestCase):
    """General behaviour of the public retrieve() API."""

    def test_public_api_signature(self):
        self.assertIsInstance(KB.retrieve("task entity"), str)
        self.assertIsInstance(KB.retrieve("task entity", top_k=4), str)

    def test_top_k_respected(self):
        result = KB.retrieve("What is the task entity?", top_k=3)
        self.assertEqual(num_items(result), 3)
        result = KB.retrieve("What is the task entity?", top_k=1)
        self.assertEqual(num_items(result), 1)

    def test_top_k_default_is_two(self):
        result = KB.retrieve("What is the task entity?")
        self.assertEqual(num_items(result), 2)

    def test_empty_query(self):
        result = KB.retrieve("   ")
        self.assertEqual(result, "An empty query was provided.")

    def test_caching_across_calls(self):
        first = KB.retrieve("What is the task entity?", top_k=2)
        second = KB.retrieve("What is the task entity?", top_k=2)
        self.assertEqual(first, second)
        self.assertEqual(KB.get_record_count(), len(KB.records))

    def test_relationship_aware_boost(self):
        # The 'covers' edges (mod_mri -> dt_*) should surface the MRI
        # modality record for this query.
        result = KB.retrieve("what datatypes does MRI cover?", top_k=3)
        self.assertIn("ID: rule_modality_mri", result)

    def test_repeated_calls_do_not_reload(self):
        # Construction should be the only place files are read; repeated
        # calls must not re-read/re-parse anything.
        before = KB.get_record_count()
        for _ in range(5):
            KB.retrieve("bold", top_k=1)
        self.assertEqual(KB.get_record_count(), before)


class RetrieverErrorHandlingTest(unittest.TestCase):
    """Graceful handling of missing/malformed knowledge base files."""

    def _write(self, directory: str, filename: str, lines) -> Path:
        path = Path(directory) / filename
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_missing_knowledge_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            retriever = Retriever(tmp)
            result = retriever.retrieve("bold")
            self.assertIn("not found in", result)
            self.assertIn("knowledge.jsonl", result)
            self.assertEqual(retriever.get_record_count(), 0)

    def test_processing_report_alone_is_not_knowledge(self):
        # A directory containing only processing_report.json must NOT be
        # treated as BIDS knowledge.
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "processing_report.json",
                        [json.dumps({"records_created": 5})])
            retriever = Retriever(tmp)
            result = retriever.retrieve("bold")
            self.assertIn("not found in", result)
            self.assertIn("knowledge.jsonl", result)
            self.assertEqual(retriever.get_record_count(), 0)

    def test_empty_knowledge_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "knowledge.jsonl", [""])
            retriever = Retriever(tmp)
            result = retriever.retrieve("bold")
            self.assertEqual(result, "The BIDS knowledge base is empty or could not be loaded.")

    def test_malformed_lines_are_skipped(self):
        valid = {
            "id": "test_bold",
            "knowledge_type": "Concept",
            "title": "Test Bold Record",
            "summary": "About the bold suffix.",
            "retrieval_text": "The bold suffix is used in func files.",
            "scope": {},
            "source": {"file": "test.yaml"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "knowledge.jsonl", [
                "this is not valid json {",
                json.dumps(valid),
                "",
                "[1, 2, 3]",
            ])
            retriever = Retriever(tmp)
            self.assertEqual(retriever.get_record_count(), 1)
            result = retriever.retrieve("bold")
            self.assertIsInstance(result, str)
            self.assertNotIn("Traceback", result)

    def test_missing_relationships_and_sources_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "knowledge.jsonl", [json.dumps({
                "id": "test_bold",
                "knowledge_type": "Concept",
                "title": "Test Bold Record",
                "summary": "About the bold suffix.",
                "retrieval_text": "The bold suffix is used in func files.",
                "scope": {},
                "source": {"file": "test.yaml"},
            })])
            retriever = Retriever(tmp)  # no relationships/sources files
            result = retriever.retrieve("bold")
            self.assertIn("ID: test_bold", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
