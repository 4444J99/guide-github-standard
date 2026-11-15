"""GitHub Actions `concurrency` semantics from pinned GitHub Docs.

Two halves that must agree, as for `token_semantics`:

* an executable model (`validate_concurrency`, `check_group_expression`, `submit`,
  `finish`, `head_ref_group`) whose every rule has an id (`K*`);
* a source-bound ledger (`evidence/semantics/github-actions-concurrency/`) of
  `ges.semantic-occurrence.v1` / `ges.semantic-proposition.v1` records, each bound to an
  exact line span of a file at the pinned `github/docs` commit, naming the rules it
  supports.

Everything is PROPOSED: the records carry no reviewer. Structural validity and model
conformance do not certify that the documentation was interpreted correctly, and the
documentation is not a specification of GitHub's server behaviour. The model treats a
run as an opaque id and a group as a (repository, case-folded name) pair; it does not
evaluate expressions, schedule real runners or observe GitHub. Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

from . import doc_ledger
from .semantics import validate_record

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / 'evidence' / 'semantics' / 'github-actions-concurrency'
ANNOTATIONS = LEDGER_DIR / 'annotations.json'
CONFORMANCE = LEDGER_DIR / 'conformance.json'
ANNOTATIONS_SCHEMA = 'ges.concurrency-semantics-annotations.v1'
CONFORMANCE_SCHEMA = 'ges.concurrency-semantics-conformance.v1'

# The docs' own `ifversion` flag that gates the `queue` property and the "at most one
# pending" wording.
QUEUE_FLAG = 'actions-nga'
QUEUE_VALUES = ('single', 'max')
MAX_PENDING = 100  # K6: "Up to 100 jobs or workflow runs can be pending"

WORKFLOW_CONTEXTS = frozenset({'github', 'inputs', 'vars'})
JOB_CONTEXTS = WORKFLOW_CONTEXTS | {'needs', 'strategy', 'matrix'}
# Contexts named by the Actions expression reference; any of these outside the allowed set
# is an error. A name that is none of these is a function or a literal, not a context.
KNOWN_CONTEXTS = frozenset({'github', 'env', 'vars', 'job', 'jobs', 'steps', 'runner', 'secrets',
                            'strategy', 'matrix', 'needs', 'inputs'})

_EXPRESSION = re.compile(r'\$\{\{(.*?)\}\}', re.S)
_STRING = re.compile(r"'(?:[^']|'')*'")
_NAME = re.compile(r'(?<![\w.\-])([A-Za-z_][\w\-]*)(?=\s*[.\[])')


def group_key(repository: str, group: str) -> str:
    """K1, K11: a group is per repository and its name is case insensitive."""
    if not isinstance(group, str) or not group.strip():
        raise ValueError('concurrency group must be a non-empty string')
    return f'{repository}\u0000{group.casefold()}'


def validate_concurrency(spec: object, flags: frozenset[str] = frozenset()) -> dict:
    """Normalise a `concurrency` value: a group string or a mapping.

    `cancel-in-progress` must already be evaluated to a boolean; evaluating a conditional
    expression is outside this model (the docs give no evaluation semantics for it).
    """
    if isinstance(spec, str):
        spec = {'group': spec}
    if not isinstance(spec, dict) or 'group' not in spec:
        raise ValueError('concurrency requires a group')
    unknown = sorted(set(spec) - {'group', 'cancel-in-progress', 'queue'})
    if unknown:
        raise ValueError(f'unsupported concurrency key {unknown[0]}')
    cancel = spec.get('cancel-in-progress', False)
    if not isinstance(cancel, bool):
        raise ValueError('cancel-in-progress must be evaluated to a boolean first')
    if 'queue' in spec:
        if QUEUE_FLAG not in flags:
            raise ValueError(f'queue is documented only where {QUEUE_FLAG} applies')
        if spec['queue'] not in QUEUE_VALUES:
            raise ValueError(f"queue must be one of {'|'.join(QUEUE_VALUES)}")
    queue = spec.get('queue', 'single')
    if queue == 'max' and cancel:
        raise ValueError('queue: max with cancel-in-progress: true is a workflow validation error')
    return {'group': spec['group'], 'cancel_in_progress': cancel, 'queue': queue}


def check_group_expression(expression: str, level: str) -> None:
    """K10: contexts a group expression may use (`workflow` or `job` level)."""
    if level not in ('workflow', 'job'):
        raise ValueError('level must be workflow or job')
    allowed = WORKFLOW_CONTEXTS if level == 'workflow' else JOB_CONTEXTS
    for body in _EXPRESSION.findall(expression):
        for name in _NAME.findall(_STRING.sub("''", body)):
            if name in KNOWN_CONTEXTS and name not in allowed:
                raise ValueError(f'context {name} is not allowed in a {level}-level concurrency group')


def head_ref_group(event: str, head_ref: str | None, fallback: str | None) -> str:
    """K12: `github.head_ref` is defined only on `pull_request`; other events need a fallback."""
    if event == 'pull_request' and head_ref:
        return head_ref
    if fallback is None:
        raise ValueError(f'github.head_ref is undefined for event {event}; provide a fallback')
    return fallback


def new_world() -> dict:
    return {}


def submit(world: dict, run: str, spec: object, *, repository: str = 'r/r',
           flags: frozenset[str] = frozenset()) -> tuple[dict, list[list[str]]]:
    """Queue a run; return the new world and the status changes it caused.

    A `spec` of None is a run with no concurrency key (K9): unconstrained, absent from the
    world. Statuses are `running`, `pending` and `canceled`.
    """
    effects: list[list[str]] = []
    if spec is None:
        return world, [[run, 'running']]
    config = validate_concurrency(spec, flags)
    world = copy.deepcopy(world)
    key = group_key(repository, config['group'])
    state = world.setdefault(key, {'running': [], 'pending': []})
    if not state['running']:  # K2: the group is free
        state['running'].append(run)
        effects.append([run, 'running'])
    elif config['cancel_in_progress']:  # K5: cancel the running run and any pending one
        for victim in state['running'] + state['pending']:
            effects.append([victim, 'canceled'])
        state['running'], state['pending'] = [run], []
        effects.append([run, 'running'])
    elif config['queue'] == 'max':  # K6: up to MAX_PENDING wait in FIFO order
        if len(state['pending']) >= MAX_PENDING:
            effects.append([run, 'canceled'])
        else:
            state['pending'].append(run)
            effects.append([run, 'pending'])
    else:  # K3, K4: the new run is pending and replaces any earlier pending run
        for victim in state['pending']:
            effects.append([victim, 'canceled'])
        state['pending'] = [run]
        effects.append([run, 'pending'])
    return world, effects


def finish(world: dict, run: str, *, repository: str = 'r/r') -> tuple[dict, list[list[str]]]:
    """The running run of its group completes; K8: the longest-waiting pending run starts."""
    world = copy.deepcopy(world)
    for key, state in world.items():
        if not key.startswith(repository + '\u0000'):
            continue
        if state['running'] == [run]:
            state['running'] = []
            if state['pending']:
                state['running'] = [state['pending'].pop(0)]
                return world, [[run, 'completed'], [state['running'][0], 'running']]
            return world, [[run, 'completed']]
    raise ValueError(f'run {run} is not running in any concurrency group of {repository}')


def snapshot(world: dict) -> dict:
    """World keyed by `repository#group`, dropping empty groups."""
    return {key.replace('\u0000', '#'): state for key, state in sorted(world.items())
            if state['running'] or state['pending']}


# --- source-bound ledger ---------------------------------------------------------------

def _annotations() -> dict:
    data = doc_ledger._load(ANNOTATIONS)
    doc_ledger._require(data.get('schema') == ANNOTATIONS_SCHEMA, 'Unsupported annotations schema')
    return data


def build_ledger(annotations: dict, sources: Path) -> dict[str, bytes]:
    """Compile occurrences, propositions and accounting from annotations and pinned bytes."""
    return doc_ledger.build_ledger(
        annotations, sources, accounting_schema='ges.concurrency-semantics-accounting.v1',
        extraction_version='concurrency-semantics.v1',
        scope=('Selected GitHub Actions `concurrency` passages only; '
               'not a review of the GitHub Docs corpus.'))


def check_conformance(annotations: dict, conformance: dict) -> list[str]:
    """Run every conformance case through the model; return all problems found."""
    return doc_ledger.check_conformance(annotations, conformance, schema=CONFORMANCE_SCHEMA,
                                        run_case=run_case)


def run_case(case: dict) -> dict:
    """Run one conformance case; a ValueError from the model is the observable result."""
    try:
        return _run_case(case)
    except ValueError as exc:
        return {'error': str(exc)}


def _run_case(case: dict) -> dict:
    kind, given = case['kind'], case['given']
    flags = frozenset(given.get('flags', []))
    if kind == 'validate':
        return {'normalized': validate_concurrency(given['spec'], flags)}
    if kind == 'expression':
        check_group_expression(given['expression'], given['level'])
        return {'allowed': True}
    if kind == 'head-ref':
        return {'group': head_ref_group(given['event'], given.get('head_ref'), given.get('fallback'))}
    if kind == 'scenario':
        world, log = new_world(), []
        for step in given['steps']:
            repository = step.get('repository', 'r/r')
            if step['op'] == 'submit':
                world, effects = submit(world, step['run'], step.get('spec'), repository=repository, flags=flags)
            elif step['op'] == 'finish':
                world, effects = finish(world, step['run'], repository=repository)
            else:
                raise ValueError(f"unknown step {step['op']}")
            log.extend(effects)
        return {'effects': log, 'groups': snapshot(world)}
    raise ValueError(f'unknown case kind {kind}')


def verify_sources(sources: Path) -> list[str]:
    """Rebuild the ledger from pinned source bytes and compare with the committed files."""
    problems = []
    try:
        built = build_ledger(_annotations(), sources)
    except (OSError, ValueError, KeyError) as exc:
        return [str(exc)]
    for name, raw in built.items():
        if (LEDGER_DIR / name).read_bytes() != raw:
            problems.append(f'{name}: differs from a rebuild against the pinned sources')
    return problems


def configure_parser(subparsers) -> None:
    parser = subparsers.add_parser('concurrency-semantics', help=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='concurrency_command', required=True)
    command = commands.add_parser('build', help='Compile the ledger from pinned github/docs files')
    command.add_argument('--sources', type=Path, required=True,
                         help='Directory laid out like the github/docs checkout at the pinned commit')
    command.add_argument('--output', type=Path, default=LEDGER_DIR)
    command = commands.add_parser('verify', help='Check the committed ledger against pinned sources')
    command.add_argument('--sources', type=Path, required=True)
    commands.add_parser('conformance', help='Check the executable model against conformance cases')
    commands.add_parser('validate', help='Validate committed record structure and identity')


def run(args) -> int:
    try:
        if args.concurrency_command == 'build':
            built = build_ledger(_annotations(), args.sources)
            args.output.mkdir(parents=True, exist_ok=True)
            for name, raw in built.items():
                (args.output / name).write_bytes(raw)
            print(json.dumps({'written': sorted(built), 'review_status': 'PROPOSED'}))
            return 0
        if args.concurrency_command == 'verify':
            problems = verify_sources(args.sources)
        elif args.concurrency_command == 'conformance':
            problems = check_conformance(_annotations(), doc_ledger._load(CONFORMANCE))
        else:
            problems = []
            for name in ('occurrences.jsonl', 'propositions.jsonl'):
                for number, line in enumerate((LEDGER_DIR / name).read_text(encoding='utf-8').splitlines(), 1):
                    try:
                        validate_record(json.loads(line))
                    except (ValueError, KeyError) as exc:
                        problems.append(f'{name}:{number}: {exc}')
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            print(json.dumps({'ok': True, 'command': args.concurrency_command,
                              'semantic_truth_certified': False}))
        return 2 if problems else 0
    except (OSError, ValueError, KeyError) as exc:
        print('concurrency-semantics: ' + str(exc), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    configure_parser(sub)
    return run(parser.parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    sys.exit(main())
