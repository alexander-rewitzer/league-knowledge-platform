"""Existing extraction plus real Judge/refinement adapters, all HTTP synthetic."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    DeepSeekExtractionAdapter, DeepSeekSectionJudgeAdapter, ExtractionCapabilityError,
    SectionValidationOperationalError, process_section_knowledge,
)
from section_validation_fixtures import (
    FakeExtractor, FakeJudge, document, envelope, evaluation, judge_document, requests,
    response, SelectionHTTPAdapter, workspace,
)


class SectionValidationIntegrationTests(unittest.TestCase):
    @unittest.skipIf(requests is None, 'Install requirements-selection.txt for offline HTTP tests')
    def test_real_adapters_reject_refine_pass_then_checkpoint_reuse_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            upstream = {p: p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            extraction_http = SelectionHTTPAdapter([(200, envelope(document('Incomplete rule'))),
                                                     (200, envelope(document('Usually wait until the interrupt is unavailable because otherwise it can stop the approach.')))])
            judge_http = SelectionHTTPAdapter([(200, envelope(judge_document('CONTEXT_PRESERVATION'))),
                                               (200, envelope(judge_document()))])
            with DeepSeekExtractionAdapter(api_key='offline-key', http_adapter=extraction_http) as extractor, \
                    DeepSeekSectionJudgeAdapter(api_key='offline-key', http_adapter=judge_http) as judge, \
                    patch('sqlite3.connect', side_effect=AssertionError('No SQL')), \
                    patch('league_knowledge_platform.source_catalog.SQLiteSourceCatalog.register_integrated', side_effect=AssertionError('No catalog')):
                result = process_section_knowledge('A', unit.section_id, extraction_capability=extractor, judge_capability=judge, **configs)
            self.assertEqual(result.checkpoint.semantic_rounds_used, 2)
            self.assertEqual(len(extraction_http.calls), 2)
            self.assertEqual(len(judge_http.calls), 2)
            self.assertEqual(result.checkpoint.accepted_proposal.items[0].evidence[0].section_id, unit.section_id)
            for path, content in upstream.items():
                self.assertEqual(path.read_bytes(), content)
            after = result.path.read_bytes()
            reused = process_section_knowledge('A', unit.section_id, **configs)
            self.assertEqual(reused.checkpoint, result.checkpoint)
            self.assertEqual(reused.operation_rounds_used, 0)
            self.assertEqual(result.path.read_bytes(), after)
            self.assertEqual(len(extraction_http.calls), 2)

    def test_invalid_extraction_response_during_refinement_preserves_completed_count(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, unit = workspace(directory)
            extractor = FakeExtractor([response('Original'), response('Duplicate', 'Duplicate')])
            with self.assertRaises(SectionValidationOperationalError) as caught:
                process_section_knowledge('A', unit.section_id, extraction_capability=extractor,
                                          judge_capability=FakeJudge([evaluation('DUPLICATION')]), **configs)
            self.assertEqual(caught.exception.semantic_rounds_used, 1)
            self.assertEqual(caught.exception.phase, 'REFINEMENT')
            self.assertFalse(list(Path(directory).glob('validated/**/*.json')))


if __name__ == '__main__':
    unittest.main()
