"""Real source adapters with synthetic HTTP and unchanged upstream artifacts/state."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import DeepSeekSourceAuditAdapter, DeepSeekSourceValidatorAdapter, process_source_knowledge
from source_audit_fixtures import SelectionHTTPAdapter, audit_document, envelope, requests, validator_document, workspace


@unittest.skipIf(requests is None, 'Install requirements-selection.txt for offline HTTP tests')
class SourceAuditIntegrationTests(unittest.TestCase):
    def test_reject_complete_refinement_pass_durable_reload_and_zero_call_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, inputs = workspace(directory)
            before = {p: p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            agent_http = SelectionHTTPAdapter([(200, envelope(audit_document(('Incomplete statement', ('input-000001', 'input-000002'))))),
                                               (200, envelope(audit_document()))])
            judge_http = SelectionHTTPAdapter([(200, envelope(validator_document('NO_KNOWLEDGE_LOSS'))), (200, envelope(validator_document()))])
            with (
                DeepSeekSourceAuditAdapter(api_key='offline-key', http_adapter=agent_http) as agent,
                DeepSeekSourceValidatorAdapter(api_key='offline-key', http_adapter=judge_http) as validator,
                patch('sqlite3.connect', side_effect=AssertionError('No SQL')),
                patch('league_knowledge_platform.source_catalog.SQLiteSourceCatalog.register_integrated', side_effect=AssertionError('No catalog')),
            ):
                result = process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
            self.assertEqual(result.operation_rounds_used, 2)
            self.assertEqual((len(agent_http.calls), len(judge_http.calls)), (2, 2))
            self.assertEqual(len(result.artifact.candidate.items[0].derived_from), len(inputs.items))
            for path, content in before.items(): self.assertEqual(path.read_bytes(), content)
            data = json.loads(json.loads(agent_http.calls[1][0].body)['messages'][1]['content'])
            self.assertEqual(data['refinement']['previous_candidate']['items'][0]['statement'], 'Incomplete statement')
            canonical = result.path.read_bytes()
            reused = process_source_knowledge('A', **configs)
            self.assertEqual(reused.operation_rounds_used, 0)
            self.assertEqual(result.path.read_bytes(), canonical)

    def test_empty_input_only_validator_http_and_full_context_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            configs, _ = workspace(directory, empty=True)
            agent_http, judge_http = SelectionHTTPAdapter([]), SelectionHTTPAdapter([(200, envelope(validator_document()))])
            with DeepSeekSourceAuditAdapter(api_key='offline-key', http_adapter=agent_http) as agent, \
                    DeepSeekSourceValidatorAdapter(api_key='offline-key', http_adapter=judge_http) as validator:
                result = process_source_knowledge('A', audit_capability=agent, validator_capability=validator, **configs)
                self.assertIsNone(agent._session)
            self.assertEqual((len(agent_http.calls), len(judge_http.calls)), (0, 1))
            payload = json.loads(json.loads(judge_http.calls[0][0].body)['messages'][1]['content'])
            self.assertEqual(payload['inputs'], [])
            self.assertEqual(len(payload['content_sections']), 2)
            self.assertEqual(result.artifact.outcome.value, 'NO_SOURCE_KNOWLEDGE')


if __name__ == '__main__':
    unittest.main()
