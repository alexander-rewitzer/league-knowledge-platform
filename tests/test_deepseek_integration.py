"""Synthetic HTTP exchanges for independent preparation and validation adapters."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from league_knowledge_platform import *
from league_knowledge_platform.gold_integration_input import IntegrationInspection
from league_knowledge_platform.gold_integration_codec import evaluation_document, plan_document
from gold_integration_fixtures import BASE, META, source, plan, evaluation
from test_gold_governance_models import SUPPORT
from selection_http_fixtures import SelectionHTTPAdapter, envelope, requests


def proposal():
    return {'proposal':{'operations':[{'operation_id':'op-1','kind':'NO_CHANGE',
        'source_item_ids':['source-item-000001'],'rationale':'Already represented.',
        'payload':{'target_knowledge_ids':[BASE.documents[0].units[0].knowledge_id],'provenance_additions':[]}}],
        'layouts':[]}}


@unittest.skipIf(requests is None,'Install requirements-transcripts.txt for offline HTTP fixtures')
class DeepSeekIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        gold=GoldConfig(Path(self.directory.name)/'gold');ref=initialize_gold_store(gold)
        publish_gold_snapshot(stage_gold_snapshot(BASE,config=gold,base=ref),config=gold,expected_base=ref)
        self.inspection=IntegrationInspection(gold,IntegrationConfig())
        self.inspection.query('search_gold',query='conditions',limit=2,status='ACTIVE')
        self.request=IntegrationAgentRequest(source(),self.inspection,IntegrationConfig())

    def adapter(self,role,script,**kwargs):
        http=SelectionHTTPAdapter(script)
        adapter=role(api_key='offline-fixture',http_adapter=http,**kwargs)
        self.addCleanup(adapter.close)
        return adapter,http

    def test_agent_strict_projection_and_context_no_secret_or_whole_corpus(self):
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(proposal()))])
        result=adapter.propose(self.request)
        self.assertEqual(result.source_reference,self.request.source.reference)
        self.assertEqual(result.base_reference,self.inspection.reference)
        body=json.loads(http.calls[0][0].body);payload=json.loads(body['messages'][1]['content'])
        self.assertEqual(payload['source']['items'][0]['item_id'],'source-item-000001')
        self.assertEqual(len(payload['governance']),7)
        self.assertIn('GoldMergePayload',payload['contracts'])
        self.assertEqual(len(payload['inspected_gold']),1)
        self.assertNotIn('offline-fixture',json.dumps(payload))
        self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(body['temperature'],0)
        self.assertEqual(http.max_retries.total,0)
        self.assertFalse(adapter._session.trust_env)

    def test_agent_can_iterate_readonly_queries_then_return_complete_proposal(self):
        query={'queries':[{'method':'get_gold_unit','arguments':{'knowledge_id':BASE.documents[0].units[0].knowledge_id}}]}
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(query)),(200,envelope(proposal()))])
        result=adapter.propose(self.request)
        self.assertEqual(len(http.calls),2)
        payload=json.loads(json.loads(http.calls[1][0].body)['messages'][1]['content'])
        self.assertEqual(len(payload['inspected_gold']),2)
        self.assertEqual(result.operations[0].kind,GoldOperationKind.NO_CHANGE)

    def test_new_global_ids_are_application_allocated_and_alias_content_is_literal(self):
        value=proposal()['proposal'];value['operations'][0].update(kind='CREATE')
        support={'source_knowledge_artifact':asdict(self.request.source.reference),'source_item_id':'source-item-000001'}
        value['operations'][0]['payload']={'new_unit':{'unit':{'knowledge_id':'new-unit-1',
            'content':'Literal new-unit-1 is not a provenance token.', 'provenance':[support], 'status':'ACTIVE',
            'relations':[],'merged_into':None},'placement':{'document_id':'new-document-1','topic_id':'new-topic-1','position':0}}}
        value['layouts']=[{'document_id':'new-document-1','title':'New domain','entity':{'namespace':'concepts','entity_key':'tempo','display_name':'Tempo'},
            'domain_key':'decisions','topics':[{'topic_id':'new-topic-1','title':'Reset','position':0,'units':[{'knowledge_id':'new-unit-1','position':0}]}]}]
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope({'proposal':value}))]*2)
        first=adapter.propose(self.request);second=adapter.propose(self.request)
        self.assertEqual(first.operations[0].payload.new_unit.unit.knowledge_id,second.operations[0].payload.new_unit.unit.knowledge_id)
        self.assertTrue(first.operations[0].payload.new_unit.unit.knowledge_id.startswith('gku-'))
        self.assertIn('new-unit-1',first.operations[0].payload.new_unit.unit.content)

    def test_refinement_contains_original_items_entire_previous_plan_and_feedback(self):
        previous=replace(plan(),base_reference=self.inspection.reference)
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(proposal()))])
        adapter.propose(replace(self.request,previous_plan=previous,feedback=evaluation('KNOWLEDGE_PRESERVATION')))
        payload=json.loads(json.loads(http.calls[0][0].body)['messages'][1]['content'])
        self.assertEqual(payload['refinement']['previous_complete_plan'],plan_document(previous))
        self.assertEqual(payload['refinement']['feedback']['gates']['KNOWLEDGE_PRESERVATION']['verdict'],'FAIL')

    def test_validator_independent_gates_no_agent_metadata_and_no_rewrite(self):
        adapter,http=self.adapter(DeepSeekIntegrationValidator,[(200,envelope(evaluation_document(evaluation())))])
        request=IntegrationValidatorRequest(self.request.source,replace(plan(),base_reference=self.inspection.reference),self.inspection.observations,16000)
        result=adapter.evaluate(request)
        self.assertEqual(result.decision,'PASS')
        payload=json.loads(json.loads(http.calls[0][0].body)['messages'][1]['content'])
        self.assertEqual(len(payload['complete_plan']['operations']),1)
        self.assertNotIn('metadata',payload['complete_plan'])
        self.assertEqual(len(result.gates),8)

    def test_strict_output_prohibits_overrides_missing_gates_unknown_fields_or_operations(self):
        bad=[]
        for extra in ('overall','score','integration_id','events'):
            v=evaluation_document(evaluation());v[extra]='PASS';bad.append(v)
        v=evaluation_document(evaluation());del v['gates']['TARGET_CORRECTNESS'];bad.append(v)
        request=IntegrationValidatorRequest(self.request.source,plan(),(),100)
        for value in bad:
            adapter,http=self.adapter(DeepSeekIntegrationValidator,[(200,envelope(value))])
            with self.assertRaises(IntegrationOperationalError):adapter.evaluate(request)
            self.assertEqual(len(http.calls),1)
        for kind in ('DELETE','REMOVE','DROP','PURGE'):
            v=proposal();v['proposal']['operations'][0]['kind']=kind
            adapter,_=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(v))])
            with self.assertRaises(IntegrationOperationalError):adapter.propose(self.request)

    def test_provider_failures_abort_once_without_retry_and_sanitize_messages(self):
        cases=[(401,{},'AUTHENTICATION'),(403,{},'AUTHORIZATION'),(429,{},'RATE_LIMIT'),
            (503,{},'SERVICE'),(302,{},'PROVIDER_CONTRACT'),
            (400,{'error':{'code':'context_length_exceeded'}},'CONTEXT_LIMIT'),
            (200,envelope({},finish='length'),'OUTPUT_TRUNCATED')]
        for role in (DeepSeekIntegrationAgent,DeepSeekIntegrationValidator):
            request=self.request if role is DeepSeekIntegrationAgent else IntegrationValidatorRequest(self.request.source,plan(),(),100)
            for status,body,code in cases:
                adapter,http=self.adapter(role,[(status,body),(200,envelope({}))])
                with self.assertRaises(IntegrationOperationalError) as caught:
                    (adapter.propose if role is DeepSeekIntegrationAgent else adapter.evaluate)(request)
                self.assertEqual(caught.exception.code,code);self.assertEqual(len(http.calls),1)
            for error,code in ((requests.exceptions.Timeout('secret'),'TIMEOUT'),(requests.exceptions.ConnectionError('secret'),'NETWORK')):
                adapter,http=self.adapter(role,[error])
                with self.assertRaises(IntegrationOperationalError) as caught:
                    (adapter.propose if role is DeepSeekIntegrationAgent else adapter.evaluate)(request)
                self.assertEqual(caught.exception.code,code);self.assertNotIn('secret',str(caught.exception))

    def test_malformed_json_duplicate_keys_finish_and_resource_failures(self):
        for text in ('null','{','{"proposal":{},"proposal":{}}','{"proposal":NaN}'):
            body=envelope({});body['choices'][0]['message']['content']=text
            adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,body)])
            with self.assertRaises(IntegrationOperationalError):adapter.propose(self.request)
        for finish in ('aborted','insufficient_system_resource','content_filter'):
            adapter,_=self.adapter(DeepSeekIntegrationAgent,[(200,envelope({},finish=finish))])
            with self.assertRaises(IntegrationOperationalError):adapter.propose(self.request)

    def test_bounded_query_exchanges_and_whitelist_never_call_writer(self):
        query={'queries':[{'method':'search_gold','arguments':{'query':'reset'}}]}
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(query))]*2)
        with self.assertRaises(IntegrationOperationalError) as caught:
            adapter.propose(replace(self.request,config=IntegrationConfig(max_agent_exchanges=2)))
        self.assertEqual(caught.exception.code,'INSPECTION_EXCHANGES_EXHAUSTED')
        self.assertEqual(len(http.calls),2)
        query={'queries':[{'method':'publish_gold_snapshot','arguments':{}}]}
        adapter,_=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(query))])
        with self.assertRaises(IntegrationOperationalError):adapter.propose(self.request)

    def test_config_runtime_key_cleanup_and_response_capacity(self):
        for kwargs in ({'model':''},{'connect_timeout_seconds':True},{'read_timeout_seconds':float('inf')},{'max_response_bytes':0}):
            with self.assertRaises(GoldContractError):DeepSeekIntegrationConfig(**kwargs)
        adapter,_=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(proposal()))],config=DeepSeekIntegrationConfig(max_response_bytes=10))
        with self.assertRaises(IntegrationOperationalError):adapter.propose(self.request)
        http=SelectionHTTPAdapter([(200,envelope(proposal()))])
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'fixture-runtime','DEEPSEEK_MODEL':'runtime-model'}):
            with DeepSeekIntegrationAgent(http_adapter=http) as adapter:
                self.assertEqual(adapter.propose(self.request).metadata.requested_model,'runtime-model')
            self.assertIsNone(adapter._api_key);self.assertTrue(http.closed)
            with self.assertRaises(GoldContractError):adapter.propose(self.request)

