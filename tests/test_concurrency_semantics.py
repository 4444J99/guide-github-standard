import copy
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ges import concurrency_semantics as cs  # noqa: E402
from ges.semantics import validate_record  # noqa: E402

CONFORMANCE = json.loads(cs.CONFORMANCE.read_text(encoding='utf-8'))
ANNOTATIONS = json.loads(cs.ANNOTATIONS.read_text(encoding='utf-8'))
NGA = frozenset({cs.QUEUE_FLAG})


class Conformance(unittest.TestCase):
    def test_committed_cases_all_pass_and_cover_every_supported_rule(self):
        self.assertEqual(cs.check_conformance(ANNOTATIONS, CONFORMANCE), [])

    def test_changed_expectation_is_reported(self):
        broken = copy.deepcopy(CONFORMANCE)
        broken['cases'][0]['expect'] = {'error': 'something else'}
        problems = cs.check_conformance(ANNOTATIONS, broken)
        self.assertEqual(len(problems), 1)
        self.assertIn(broken['cases'][0]['id'], problems[0])

    def test_uncited_source_rule_and_unsupported_cited_rule_are_reported(self):
        trimmed = copy.deepcopy(CONFORMANCE)
        trimmed['cases'] = [c for c in trimmed['cases'] if 'K7' not in c['rules']]
        self.assertTrue(any('rule K7' in p for p in cs.check_conformance(ANNOTATIONS, trimmed)))
        extra = copy.deepcopy(CONFORMANCE)
        extra['cases'][0]['rules'] = ['K99']
        self.assertTrue(any('K99' in p for p in cs.check_conformance(ANNOTATIONS, extra)))

    def test_schema_is_checked(self):
        self.assertEqual(cs.check_conformance(ANNOTATIONS, {'schema': 'x', 'cases': []}),
                         ['conformance: unsupported schema'])

    def test_every_modelled_rule_is_in_the_ledger_accounting(self):
        accounting = json.loads((cs.LEDGER_DIR / 'accounting.json').read_text(encoding='utf-8'))
        self.assertEqual(accounting['rules_supported'],
                         sorted({f'K{n}' for n in range(1, 14)}))


class Model(unittest.TestCase):
    def test_group_key_folds_case_and_is_repository_scoped(self):
        self.assertEqual(cs.group_key('o/a', 'Prod'), cs.group_key('o/a', 'prod'))
        self.assertNotEqual(cs.group_key('o/a', 'prod'), cs.group_key('o/b', 'prod'))
        with self.assertRaises(ValueError):
            cs.group_key('o/a', '  ')

    def test_unknown_key_and_missing_group_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unsupported concurrency key'):
            cs.validate_concurrency({'group': 'g', 'cancel': True})
        with self.assertRaisesRegex(ValueError, 'requires a group'):
            cs.validate_concurrency({'cancel-in-progress': True})

    def test_expression_parser_ignores_functions_literals_and_property_tails(self):
        cs.check_group_expression("${{ format('env.{0}', github.run_id) }}", 'workflow')
        cs.check_group_expression('${{ github.event.pull_request.head.repo.fork }}', 'workflow')
        with self.assertRaises(ValueError):
            cs.check_group_expression("${{ contains(steps.x.outputs.y, 'a') }}", 'job')
        with self.assertRaises(ValueError):
            cs.check_group_expression('x', 'step')

    def test_validation_and_submission_reject_the_same_invalid_groups(self):
        for group in (None, '', '   ', 5):
            with self.assertRaises(ValueError):
                cs.validate_concurrency({'group': group})
            with self.assertRaises(ValueError):
                cs.submit(cs.new_world(), 'r1', {'group': group})

    def test_unterminated_expression_and_escaped_quotes(self):
        with self.assertRaisesRegex(ValueError, 'unterminated'):
            cs.check_group_expression('${{ github.ref', 'workflow')
        with self.assertRaises(ValueError):
            cs.check_group_expression("${{ format('it''s }}', needs.a.x) }}", 'workflow')
        cs.check_group_expression("${{ format('it''s }}', github.ref) }}", 'workflow')

    def test_queue_overflow_is_exactly_one_hundred(self):
        world, spec = cs.new_world(), {'group': 'g', 'queue': 'max'}
        for n in range(1 + cs.MAX_PENDING):
            world, _ = cs.submit(world, f'r{n}', spec, flags=NGA)
        self.assertEqual(len(cs.snapshot(world)['r/r#g']['pending']), cs.MAX_PENDING)
        world, effects = cs.submit(world, 'extra', spec, flags=NGA)
        self.assertEqual(effects, [['extra', 'canceled']])

    def test_submit_and_finish_do_not_mutate_their_input(self):
        world, _ = cs.submit(cs.new_world(), 'r1', 'g')
        before = copy.deepcopy(world)
        cs.submit(world, 'r2', 'g')
        cs.finish(world, 'r1')
        self.assertEqual(world, before)

    def test_random_histories_keep_the_documented_invariants(self):
        rng = random.Random(20261114)
        for _ in range(200):
            world, live, counter = cs.new_world(), set(), 0
            for _ in range(60):
                if rng.random() < 0.7 or not live:
                    counter += 1
                    spec = {'group': rng.choice(['A', 'a', 'b']),
                            'cancel-in-progress': rng.random() < 0.3}
                    if not spec['cancel-in-progress'] and rng.random() < 0.5:
                        spec['queue'] = 'max'
                    run = f'r{counter}'
                    world, effects = cs.submit(world, run, spec, repository=rng.choice(['o/a', 'o/b']), flags=NGA)
                    for name, status in effects:
                        (live.add if status in ('running', 'pending') else live.discard)(name)
                else:
                    running = [(key, s['running'][0]) for key, s in world.items() if s['running']]
                    if running:
                        key, run = rng.choice(running)
                        world, effects = cs.finish(world, run, repository=key.split('\u0000')[0])
                        for name, status in effects:
                            (live.add if status in ('running', 'pending') else live.discard)(name)
                seen = []
                for key, state in world.items():
                    self.assertLessEqual(len(state['running']), 1)
                    self.assertLessEqual(len(state['pending']), cs.MAX_PENDING)
                    if state['pending']:
                        self.assertTrue(state['running'], 'a pending run needs a running run ahead of it')
                    seen += state['running'] + state['pending']
                self.assertEqual(len(seen), len(set(seen)), 'a run is in at most one slot')
                self.assertEqual(set(seen), live)


class ReceiptWorkflowConsequences(unittest.TestCase):
    """The workflow in docs/agent-verification-receipts.md, read through the model."""

    def caller(self):
        text = (ROOT / 'docs' / 'agent-verification-receipts.md').read_text(encoding='utf-8')
        block = re.search(r'## Action\n\n```yaml\n(.*?)```', text, re.S).group(1)
        return yaml.safe_load(block)

    def test_documented_group_expression_is_valid_at_job_level_and_cancels(self):
        job = self.caller()['jobs']['receipt']
        cs.check_group_expression(job['concurrency']['group'], 'job')
        cs.check_group_expression(job['concurrency']['group'], 'workflow')
        normalized = cs.validate_concurrency(job['concurrency'], NGA)
        self.assertTrue(normalized['cancel_in_progress'])
        self.assertEqual(normalized['queue'], 'single')

    def test_a_newer_head_cancels_the_in_flight_receipt_run_of_the_same_pull_request_only(self):
        spec = {'group': 'agent-receipt-7', 'cancel-in-progress': True}
        world, _ = cs.submit(cs.new_world(), 'head-a', spec)
        world, effects = cs.submit(world, 'head-b', spec)
        self.assertEqual(effects, [['head-a', 'canceled'], ['head-b', 'running']])
        world, effects = cs.submit(world, 'other-pr', {**spec, 'group': 'agent-receipt-8'})
        self.assertEqual(effects, [['other-pr', 'running']])


class LedgerBuild(unittest.TestCase):
    def committed(self):
        return [json.loads(line) for name in ('occurrences.jsonl', 'propositions.jsonl')
                for line in (cs.LEDGER_DIR / name).read_text(encoding='utf-8').splitlines()]

    def test_committed_records_are_valid_proposed_and_account_for_annotations(self):
        records = self.committed()
        for record in records:
            validate_record(record)
        occurrences = [r for r in records if r['schema'] == 'ges.semantic-occurrence.v1']
        propositions = [r for r in records if r['schema'] == 'ges.semantic-proposition.v1']
        self.assertEqual(len(occurrences), len(ANNOTATIONS['entries']))
        self.assertEqual(len(propositions), sum(len(e['propositions']) for e in ANNOTATIONS['entries']))
        for record in occurrences:
            self.assertEqual(record['review'], {'status': 'PROPOSED', 'reviewer': None, 'evidence_reference': None})
            self.assertEqual(record['source']['commit'], ANNOTATIONS['commit'])
        for record in propositions:
            self.assertEqual(record['primary_review']['status'], 'PROPOSED')
        accounting = json.loads((cs.LEDGER_DIR / 'accounting.json').read_text(encoding='utf-8'))
        self.assertFalse(accounting['semantic_truth_certified'])
        self.assertFalse(accounting['policy_adopted'])
        self.assertEqual(accounting['occurrences'], len(occurrences))
        self.assertEqual(accounting['propositions'], len(propositions))
        self.assertEqual(accounting['propositions_preserved_without_model_rule'], 2)

    def test_every_ambiguity_is_kept_in_a_proposition(self):
        ambiguities = [a for r in self.committed() if r['schema'] == 'ges.semantic-proposition.v1'
                       for a in r['ambiguities']]
        self.assertTrue(any('contradicts the example' in a for a in ambiguities))
        self.assertTrue(any('strict FIFO' in a for a in ambiguities))
        self.assertTrue(any('only when cancel-in-progress is true' in a for a in ambiguities))

    def synthetic(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        text = 'alpha\nbeta rule here\ngamma\n'
        (Path(directory.name) / 'a.md').write_bytes(text.encode())
        entry = copy.deepcopy(ANNOTATIONS['entries'][0])
        entry.update({'path': 'a.md', 'start_line': 2, 'end_line': 2, 'expect': 'rule here',
                      'content_sha256': hashlib.sha256(text.encode()).hexdigest()})
        return Path(directory.name), {**ANNOTATIONS, 'entries': [entry]}

    def test_source_drift_and_range_drift_are_rejected(self):
        sources, annotations = self.synthetic()
        cs.build_ledger(annotations, sources)
        (sources / 'a.md').write_bytes(b'alpha\nbeta rule here\ngamma changed\n')
        with self.assertRaisesRegex(ValueError, 'content digest differs'):
            cs.build_ledger(annotations, sources)
        sources, annotations = self.synthetic()
        annotations['entries'][0]['start_line'] = annotations['entries'][0]['end_line'] = 3
        with self.assertRaisesRegex(ValueError, 'anchor text not found'):
            cs.build_ledger(annotations, sources)

    def test_ledger_carries_no_template_markup(self):
        for record in self.committed():
            self.assertNotIn('{%', json.dumps(record))


class Cli(unittest.TestCase):
    def test_conformance_and_validate_commands_exit_zero(self):
        for command in ('conformance', 'validate'):
            result = subprocess.run([sys.executable, '-m', 'ges', 'concurrency-semantics', command],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_verify_with_missing_sources_fails_visibly(self):
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run([sys.executable, '-m', 'ges', 'concurrency-semantics', 'verify', '--sources', empty],
                                    cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
