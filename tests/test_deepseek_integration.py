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
from test_gold_inspection import inspection_snapshot
from test_gold_governance_models import SUPPORT
from selection_http_fixtures import SelectionHTTPAdapter, envelope, requests


def proposal():
    return {'proposal':{'operations':[{'kind':'NO_CHANGE',
        'source_item_ids':['source-item-000001'],'rationale':'Already represented.',
        'payload':{'target_knowledge_ids':[BASE.documents[0].units[0].knowledge_id],'provenance_additions':[]}}],
        'layouts':[]}}


def structure_query(**arguments):
    return {'queries': [{'method': 'inspect_gold_structure', 'arguments': arguments}]}


def create_draft(incoming, groups):
    operations, placements = [], []
    for position, item_ids in enumerate(groups):
        alias = f'new-unit-{position + 1}'
        operations.append({
            'kind': 'CREATE', 'source_item_ids': item_ids,
            'rationale': 'New topic knowledge.', 'payload': {'new_unit': {
                'unit': {'knowledge_id': alias, 'content': f'Literal {alias} in source content.',
                         'provenance': [{'source_knowledge_artifact': asdict(incoming.reference),
                                         'source_item_id': item_id} for item_id in item_ids],
                         'status': 'ACTIVE', 'relations': [], 'merged_into': None},
                'placement': {'document_id': 'new-document-1', 'topic_id': 'new-topic-1', 'position': position},
            }},
        })
        placements.append({'knowledge_id': alias, 'position': position})
    return {'proposal': {'operations': operations, 'layouts': [{
        'document_id': 'new-document-1', 'title': 'New domain', 'domain_key': 'decisions',
        'entity': {'namespace': 'concepts', 'entity_key': 'tempo', 'display_name': 'Tempo'},
        'topics': [{'topic_id': 'new-topic-1', 'title': 'Reset', 'position': 0, 'units': placements}],
    }]}}


def request_payload(http, index):
    return json.loads(json.loads(http.calls[index][0].body)['messages'][1]['content'])


@unittest.skipIf(requests is None,'Install requirements-transcripts.txt for offline HTTP fixtures')
class DeepSeekIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.gold=GoldConfig(Path(self.directory.name)/'gold');ref=initialize_gold_store(self.gold)
        publish_gold_snapshot(stage_gold_snapshot(BASE,config=self.gold,base=ref),config=self.gold,expected_base=ref)
        self.inspection=IntegrationInspection(self.gold,IntegrationConfig())
        self.inspection.query('inspect_gold_structure')
        self.request=IntegrationAgentRequest(source(),self.inspection,IntegrationConfig())

    def adapter(self,role,script,**kwargs):
        http=SelectionHTTPAdapter(script)
        adapter=role(api_key='offline-fixture',http_adapter=http,**kwargs)
        self.addCleanup(adapter.close)
        return adapter,http

    def topic_collision(self):
        if not hasattr(self, 'collision_gold'):
            self.collision_gold = GoldConfig(Path(self.directory.name) / 'topic-collision')
            before = initialize_gold_store(self.collision_gold)
            staged = stage_gold_snapshot(inspection_snapshot(), config=self.collision_gold, base=before)
            publish_gold_snapshot(staged, config=self.collision_gold, expected_base=before)
        gold = self.collision_gold
        inspection = IntegrationInspection(gold, IntegrationConfig())
        first, second, _ = inspection.snapshot.documents
        other_topic = next(topic for topic in first.topics if topic.topic_id != second.topics[0].topic_id)
        known_unit = other_topic.units[0].knowledge_id
        inspection.query('search_gold', query=known_unit)
        inspection.query('inspect_gold_structure', domain_key=second.domain_key)
        return replace(self.request, inspection=inspection), first, second.topics[0].topic_id

    def test_agent_strict_projection_and_context_no_secret_or_whole_corpus(self):
        adapter,http=self.adapter(DeepSeekIntegrationAgent,[(200,envelope(proposal()))])
        result=adapter.propose(self.request)
        self.assertEqual(result.source_reference,self.request.source.reference)
        self.assertEqual(result.base_reference,self.inspection.reference)
        self.assertEqual(result.operations[0].operation_id, 'op-1')
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

    def test_structure_query_malformed_or_invented_identity_stops_before_validator(self):
        gold = GoldConfig(Path(self.directory.name) / 'empty-gold')
        before = initialize_gold_store(gold)
        incoming = source(*(f'source-item-{i:06d}' for i in range(1, 168)))
        for supplied, expected in (
            ('new-document-1', 'INSPECTION_INVALID_IDENTITY'),
            ('gdoc-' + 'f' * 32, 'INSPECTION_UNKNOWN_ID'),
        ):
            with self.subTest(supplied=supplied):
                bad = structure_query(document_id=supplied)
                bad['queries'].extend(structure_query()['queries'])
                agent, http = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(bad))])
                validator, validation_http = self.adapter(DeepSeekIntegrationValidator, [])
                with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=incoming):
                    with self.assertRaises(IntegrationOperationalError) as caught:
                        prepare_gold_integration(incoming.reference.source_id, gold_config=gold, agent=agent,
                                                 validator=validator)
                self.assertEqual((caught.exception.code, caught.exception.phase, caught.exception.rounds),
                                 (expected, 'AGENT', ()))
                self.assertNotIn(supplied, str(caught.exception))
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(validation_http.calls, [])
                self.assertEqual(load_gold_snapshot(gold).reference, before)

    def test_structure_discovery_supplies_real_ids_before_targeted_filter(self):
        inspection = IntegrationInspection(self.gold, IntegrationConfig())
        document_id = BASE.documents[0].document_id
        agent, http = self.adapter(DeepSeekIntegrationAgent, [
            (200, envelope(structure_query(document_id=None))),
            (200, envelope(structure_query(document_id=document_id))),
            (200, envelope(proposal())),
        ])
        result = agent.propose(replace(self.request, inspection=inspection))
        initial = request_payload(http, 0)
        self.assertTrue(initial['gold_has_documents'])
        self.assertEqual(initial['contracts']['queries']['inspect_gold_structure']['required'], {})
        observed = request_payload(http, 1)['inspected_gold'][0]['result']['value']
        discovered = observed['entities'][0]['documents'][0]
        self.assertEqual(discovered['document_id'], document_id)
        self.assertEqual(request_payload(http, 2)['inspected_gold'][-1]['arguments'], {'document_id': document_id})
        self.assertEqual(result.operations[0].payload.target_knowledge_ids, (BASE.documents[0].units[0].knowledge_id,))

    def test_existing_id_cannot_be_used_before_observation_is_delivered(self):
        document_id = BASE.documents[0].document_id
        batch = structure_query()
        batch['queries'].extend(structure_query(document_id=document_id)['queries'])
        inspection = IntegrationInspection(self.gold, IntegrationConfig())
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(batch))])
        with self.assertRaises(IntegrationOperationalError) as caught:
            agent.propose(replace(self.request, inspection=inspection))
        self.assertEqual(caught.exception.code, 'INSPECTION_UNKNOWN_ID')
        self.assertEqual(len(inspection.observations), 1)

    def test_invalid_query_arguments_remain_strict(self):
        for method, arguments in (
            ('search_gold', {'query': 'reset', 'status': 'unknown'}),
            ('search_gold', {'query': 'reset', 'limit': 101}),
            ('inspect_gold_structure', {'domain_key': 'Invalid Key'}),
            ('inspect_gold_structure', {'topic_id': None}),
            ('get_gold_unit', {}),
        ):
            with self.subTest(method=method, arguments=arguments):
                query = {'queries': [{'method': method, 'arguments': arguments}]}
                agent, http = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(query))])
                with self.assertRaises(IntegrationOperationalError) as caught:
                    agent.propose(self.request)
                self.assertEqual(caught.exception.code, 'INSPECTION_INVALID_ARGUMENTS')
                self.assertEqual(len(http.calls), 1)

    def test_topic_lookups_and_combined_filters_require_the_observed_document_pair(self):
        request, first, shared_topic_id = self.topic_collision()
        pair = {'document_id': first.document_id, 'topic_id': shared_topic_id}
        for method, extra in (('get_gold_topic', {}), ('search_gold', {'query': 'charm'})):
            with self.subTest(method=method):
                query = {'queries': [{'method': method, 'arguments': {**pair, **extra}}]}
                agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(query))])
                with self.assertRaises(IntegrationOperationalError) as caught:
                    agent.propose(request)
                self.assertEqual(caught.exception.code, 'INSPECTION_UNKNOWN_ID')
        cross_document = {'queries': [{'method': 'search_gold', 'arguments': {'query': 'charm', 'topic_id': shared_topic_id}}]}
        targeted = {'queries': [{'method': 'get_gold_topic', 'arguments': pair}]}
        agent, http = self.adapter(DeepSeekIntegrationAgent, [
            (200, envelope(cross_document)), (200, envelope(targeted)), (200, envelope(proposal())),
        ])
        agent.propose(request)
        result = request_payload(http, 2)['inspected_gold'][-1]['result']['value']
        self.assertEqual((result['document_id'], result['topic']['topic_id']), (first.document_id, shared_topic_id))

    def test_proposal_layout_and_placement_require_the_observed_topic_pair(self):
        request, first, shared_topic_id = self.topic_collision()
        draft = create_draft(request.source, [['source-item-000001']])
        layout = draft['proposal']['layouts'][0]
        layout['document_id'] = first.document_id
        layout['topics'][0]['topic_id'] = shared_topic_id
        intent = draft['proposal']['operations'][0]['payload']['new_unit']['placement']
        intent.update(document_id=first.document_id, topic_id=shared_topic_id)
        for layouts in ([layout], []):
            with self.subTest(layout_included=bool(layouts)):
                draft['proposal']['layouts'] = layouts
                agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(draft))])
                with self.assertRaises(IntegrationOperationalError) as caught:
                    agent.propose(request)
                self.assertEqual(caught.exception.code, 'INVALID_OUTPUT')
        draft['proposal']['layouts'] = [layout]
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [
            (200, envelope(structure_query(document_id=first.document_id))), (200, envelope(draft)),
        ])
        result = agent.propose(request)
        placement = result.operations[0].payload.new_unit.placement
        self.assertEqual((placement.document_id, placement.topic_id), (first.document_id, shared_topic_id))

    def test_full_document_observation_authorizes_its_topic_for_lookup_and_placement(self):
        _, first, shared_topic_id = self.topic_collision()
        pair = {'document_id': first.document_id, 'topic_id': shared_topic_id}
        draft = create_draft(self.request.source, [['source-item-000001']])
        proposed = draft['proposal']
        proposed['layouts'][0]['document_id'] = first.document_id
        proposed['layouts'][0]['topics'][0]['topic_id'] = shared_topic_id
        proposed['operations'][0]['payload']['new_unit']['placement'].update(pair)
        full_document = {'queries': [{'method': 'get_gold_document', 'arguments': {'document_id': first.document_id}}]}
        targeted = {'queries': [{'method': 'get_gold_topic', 'arguments': pair}]}
        for inspect_topic in (False, True):
            with self.subTest(inspect_topic=inspect_topic):
                request, _, _ = self.topic_collision()
                script = [full_document, targeted, draft] if inspect_topic else [full_document, draft]
                agent, http = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(reply)) for reply in script])
                outcome = agent.propose(request)
                placement = outcome.operations[0].payload.new_unit.placement
                self.assertEqual((placement.document_id, placement.topic_id), (first.document_id, shared_topic_id))
                self.assertEqual(len(http.calls), len(script))

    def test_proposal_cannot_reference_invented_or_unobserved_gold_identity(self):
        undisclosed = IntegrationInspection(self.gold, IntegrationConfig())
        for target, inspection in ((BASE.documents[0].units[0].knowledge_id, undisclosed),
                                   ('gku-' + 'f' * 32, self.inspection), ('new-unit-1', self.inspection)):
            with self.subTest(target=target):
                draft = proposal()
                draft['proposal']['operations'][0]['payload']['target_knowledge_ids'] = [target]
                agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(draft))])
                with self.assertRaises(IntegrationOperationalError):
                    agent.propose(replace(self.request, inspection=inspection))
                self.assertEqual(inspection.declarations, ())

    def test_new_global_ids_are_application_allocated_and_alias_content_is_literal(self):
        draft = create_draft(self.request.source, [['source-item-000001']])
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(draft))] * 2)
        first = agent.propose(self.request)
        second = agent.propose(replace(self.request, previous_plan=first, feedback=evaluation('KNOWLEDGE_PRESERVATION')))
        self.assertEqual(plan_document(first), plan_document(second))
        new_unit = first.operations[0].payload.new_unit
        self.assertRegex(new_unit.unit.knowledge_id, r'^gku-[0-9a-f]{32}$')
        self.assertRegex(new_unit.placement.document_id, r'^gdoc-[0-9a-f]{32}$')
        self.assertRegex(new_unit.placement.topic_id, r'^topic-[0-9a-f]{32}$')
        self.assertIn('new-unit-1', new_unit.unit.content)

    def test_create_rejects_canonical_draft_id_and_undeclared_placement_alias(self):
        for field, identity in (('knowledge_id', 'gku-' + 'f' * 32), ('document_id', 'new-document-2'),
                                ('topic_id', 'new-topic-2')):
            with self.subTest(field=field):
                draft = create_draft(self.request.source, [['source-item-000001']])
                intent = draft['proposal']['operations'][0]['payload']['new_unit']
                intent['unit' if field == 'knowledge_id' else 'placement'][field] = identity
                agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(draft))])
                with self.assertRaises(IntegrationOperationalError):
                    agent.propose(self.request)
                self.assertNotIn(identity, {alias for _, alias, _ in self.inspection.declarations})

    def test_proposal_can_cite_existing_provenance_after_inspection_discloses_it(self):
        original = BASE.documents[0].units[0]
        support = original.provenance[0]
        draft = create_draft(self.request.source, [['source-item-000001']])
        new_unit = draft['proposal']['operations'][0]['payload']['new_unit']['unit']
        new_unit['provenance'].append(asdict(support))
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(draft))])
        with self.assertRaises(IntegrationOperationalError):
            agent.propose(self.request)
        query = {'queries': [{'method': 'get_gold_provenance', 'arguments': {'knowledge_id': original.knowledge_id}}]}
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(query)), (200, envelope(draft))])
        plan = agent.propose(self.request)
        self.assertIn(support, plan.operations[0].payload.new_unit.unit.provenance)

    def test_conflict_resolves_new_claim_alias_without_replacing_observed_claim_identity(self):
        incoming = self.request.source
        draft = create_draft(incoming, [['source-item-000001']])
        entry = draft['proposal']['operations'][0]
        existing_id = BASE.documents[0].units[0].knowledge_id
        entry['kind'] = 'FLAG_CONFLICT'
        entry['payload'] = {'new_claims': [entry['payload']['new_unit']],
                            'claim_knowledge_ids': [existing_id, 'new-unit-1'],
                            'contradictions': [{'source_knowledge_id': 'new-unit-1', 'relation': {
                                'kind': 'contradicts', 'target_knowledge_id': existing_id}}],
                            'provenance_additions': [], 'conflict_reason': 'Incompatible conditions.'}
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(structure_query())), (200, envelope(draft))])
        validator, _ = self.adapter(DeepSeekIntegrationValidator, [(200, envelope(evaluation_document(evaluation())))])
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=incoming):
            result = prepare_gold_integration(incoming.reference.source_id, gold_config=self.gold,
                                              agent=agent, validator=validator)
        payload = result.plan.operations[0].payload
        new_id = payload.new_claims[0].unit.knowledge_id
        self.assertRegex(new_id, r'^gku-[0-9a-f]{32}$')
        self.assertEqual(set(payload.claim_knowledge_ids), {new_id, existing_id})
        self.assertEqual(payload.contradictions[0].source_knowledge_id, new_id)
        self.assertEqual(payload.contradictions[0].relation.target_knowledge_id, existing_id)

    def test_agent_and_validator_process_a_complete_167_item_plan(self):
        item_ids = [f'source-item-{i:06d}' for i in range(1, 168)]
        incoming = source(*item_ids)
        gold = GoldConfig(Path(self.directory.name) / 'empty-gold')
        before = initialize_gold_store(gold)
        draft = create_draft(incoming, [item_ids[start:start + 14] for start in range(0, len(item_ids), 14)])
        agent, http = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(structure_query())), (200, envelope(draft))])
        validator, validation_http = self.adapter(DeepSeekIntegrationValidator,
                                                  [(200, envelope(evaluation_document(evaluation())))])
        with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=incoming):
            result = prepare_gold_integration(incoming.reference.source_id, gold_config=gold,
                                              agent=agent, validator=validator)
        self.assertFalse(request_payload(http, 0)['gold_has_documents'])
        for payload in (request_payload(http, 0), request_payload(http, 1), request_payload(validation_http, 0)):
            self.assertEqual([item['item_id'] for item in payload['source']['items']], item_ids)
        sent_plan = request_payload(validation_http, 0)['complete_plan']
        self.assertEqual(sent_plan, json.loads(json.dumps(plan_document(result.plan))))
        self.assertCountEqual([ref['source_item_id'] for operation in sent_plan['operations']
                               for ref in operation['source_refs']], item_ids)
        self.assertEqual(result.source_item_ids, tuple(item_ids))
        self.assertEqual(result.outcome, 'PREPARED_CHANGE')
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(len(result.rounds[0].evaluation.gates), 8)
        self.assertEqual(load_gold_snapshot(gold).reference, before)

    def test_missing_duplicate_and_unknown_source_accounting_stops_before_validator(self):
        incoming = source('source-item-000001', 'source-item-000002')
        cases = ((['source-item-000001'], 'SOURCE_ITEM_COVERAGE', 'PLAN'),
                 (['source-item-000001'] * 2, 'INVALID_OUTPUT', 'AGENT'),
                 (['source-item-000001', 'source-item-000003'], 'INVALID_OUTPUT', 'AGENT'))
        for group, code, phase in cases:
            with self.subTest(group=group):
                agent, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(create_draft(incoming, [group])))])
                validator, validation_http = self.adapter(DeepSeekIntegrationValidator, [])
                with patch('league_knowledge_platform.gold_integration.load_integration_source', return_value=incoming):
                    with self.assertRaises(IntegrationOperationalError) as caught:
                        prepare_gold_integration(incoming.reference.source_id, gold_config=self.gold,
                                                 agent=agent, validator=validator)
                self.assertEqual((caught.exception.code, caught.exception.phase, caught.exception.rounds), (code, phase, ()))
                self.assertEqual(validation_http.calls, [])

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

    def test_validator_can_only_reference_supplied_gold_ids(self):
        request = IntegrationValidatorRequest(self.request.source, plan(), (), 100)
        disclosed, undisclosed = (unit.knowledge_id for unit in BASE.documents[0].units)
        for identity in (disclosed, undisclosed, 'gku-' + 'f' * 32):
            with self.subTest(identity=identity):
                response = evaluation_document(evaluation('TARGET_CORRECTNESS', affected_gold_ids=(identity,)))
                adapter, _ = self.adapter(DeepSeekIntegrationValidator, [(200, envelope(response))])
                if identity == disclosed:
                    self.assertEqual(adapter.evaluate(request).decision, 'REJECT')
                else:
                    with self.assertRaises(IntegrationOperationalError) as caught:
                        adapter.evaluate(request)
                    self.assertEqual(caught.exception.code, 'FEEDBACK_REFERENCE')

    def test_validator_can_reference_observed_and_new_gold_structure(self):
        agent, _ = self.adapter(DeepSeekIntegrationAgent, [
            (200, envelope(create_draft(self.request.source, [['source-item-000001']]))),
        ])
        complete = agent.propose(self.request)
        layout = complete.proposed_layout[0]
        request = IntegrationValidatorRequest(self.request.source, complete, self.inspection.observations, 100)
        identities = (BASE.documents[0].document_id, layout.document_id, layout.topics[0].topic_id)
        result = evaluation_document(evaluation('STRUCTURAL_VALIDITY', affected_gold_ids=identities))
        validator, _ = self.adapter(DeepSeekIntegrationValidator, [(200, envelope(result))])
        self.assertEqual(validator.evaluate(request).decision, 'REJECT')

    def test_validator_can_reference_both_link_endpoints_from_the_plan(self):
        link = plan(5)
        edge = link.operations[0].payload.relation
        request = IntegrationValidatorRequest(self.request.source, link, (), 100)
        identities = (edge.source_knowledge_id, edge.relation.target_knowledge_id)
        result = evaluation_document(evaluation('TARGET_CORRECTNESS', affected_gold_ids=identities))
        validator, _ = self.adapter(DeepSeekIntegrationValidator, [(200, envelope(result))])
        self.assertEqual(validator.evaluate(request).decision, 'REJECT')

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
        v = proposal()
        v['proposal']['operations'][0]['operation_id'] = 'model-invented'
        adapter, _ = self.adapter(DeepSeekIntegrationAgent, [(200, envelope(v))])
        with self.assertRaises(IntegrationOperationalError):
            adapter.propose(self.request)

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
