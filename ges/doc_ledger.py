"""Shared machinery for source-bound GitHub Docs semantic ledgers.

Compiles authored span annotations against pinned `github/docs` bytes into
`ges.semantic-occurrence.v1` / `ges.semantic-proposition.v1` records and checks an
executable model against conformance cases. Used by `token_semantics` and
`concurrency_semantics`. Every record is PROPOSED; nothing here certifies that the
documentation was read correctly. Standard library only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .semantics import canonical_bytes, stable_id, validate_record


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def build_ledger(annotations: dict, sources: Path, *, accounting_schema: str, extraction_version: str,
                 scope: str) -> dict[str, bytes]:
    """Compile occurrences, propositions and accounting from annotations and pinned bytes."""
    repository, commit = annotations['repository'], annotations['commit']
    occurrences, propositions = [], []
    modelled: set[str] = set()
    contents: dict[str, tuple[str, list[bytes]]] = {}
    for entry in annotations['entries']:
        path = entry['path']
        _require(not path.startswith('/') and '..' not in path.split('/') and '\\' not in path,
                 'Unsafe source path')
        if path not in contents:
            raw = (sources / path).read_bytes()
            contents[path] = (sha(raw), raw.splitlines(keepends=True))
        content_digest, lines = contents[path]
        start, end = entry['start_line'], entry['end_line']
        _require(1 <= start <= end <= len(lines), f'{path}: span {start}-{end} outside file')
        span = b''.join(lines[start - 1:end])
        _require(entry['expect'] in span.decode('utf-8'),
                 f'{path}:{start}-{end}: anchor text not found in span (source drift)')
        _require(entry['content_sha256'] == content_digest,
                 f'{path}: pinned content digest differs from annotation')
        occurrence = {
            'schema': 'ges.semantic-occurrence.v1',
            'source': {'repository': repository, 'commit': commit, 'path': path,
                       'content_sha256': content_digest, 'start_line': start, 'end_line': end,
                       'span_sha256': sha(span)},
            'heading_ancestry': entry['heading_ancestry'],
            'include_chain': entry['include_chain'],
            'page_applications': [],
            'conditions': entry['conditions'],
            'products': ['GitHub Actions'],
            'versions': entry['versions'],
            'authority_class': 'PLATFORM',
            'semantic_kind': entry['semantic_kind'],
            'semantic_ast': entry['propositions'][0]['semantic_ast'],
            'formalization': ' '.join(p['formalization'] for p in entry['propositions']),
            'extraction': {'method': 'authored-span-annotations', 'version': extraction_version},
            'review': {'status': 'PROPOSED', 'reviewer': None, 'evidence_reference': None},
        }
        occurrence['id'] = stable_id(occurrence)
        validate_record(occurrence)
        occurrences.append(occurrence)
        for item in entry['propositions']:
            proposition = {
                'schema': 'ges.semantic-proposition.v1',
                'semantic_ast': item['semantic_ast'],
                'occurrence_ids': [occurrence['id']],
                'preserved_differences': item.get('preserved_differences', []),
                'authority_order': ['PLATFORM'],
                'applicability': entry['conditions'] + entry['versions'],
                'ambiguities': item.get('ambiguities', []),
                'primary_review': {'status': 'PROPOSED', 'reviewer': None, 'evidence_reference': None},
                'omission_review': {'status': 'PROPOSED', 'reviewer': None, 'evidence_reference': None},
            }
            proposition['id'] = stable_id(proposition)
            validate_record(proposition)
            propositions.append(proposition)
            modelled.update(item.get('rules', []))
    _require(len({p['id'] for p in propositions}) == len(propositions),
             'Two annotated statements have an identical semantic AST')
    accounting = {
        'schema': accounting_schema,
        'repository': repository,
        'commit': commit,
        'files': len(contents),
        'occurrences': len(occurrences),
        'propositions': len(propositions),
        'propositions_with_model_rule': sum(
            1 for entry in annotations['entries'] for p in entry['propositions'] if p.get('rules')),
        'propositions_preserved_without_model_rule': sum(
            1 for entry in annotations['entries'] for p in entry['propositions'] if not p.get('rules')),
        'rules_supported': sorted(modelled),
        'review_status': 'PROPOSED',
        'semantic_truth_certified': False,
        'policy_adopted': False,
        'scope': scope,
    }

    def jsonl(records: list[dict]) -> bytes:
        return b''.join(canonical_bytes(r) + b'\n' for r in records)

    return {'occurrences.jsonl': jsonl(occurrences), 'propositions.jsonl': jsonl(propositions),
            'accounting.json': json.dumps(accounting, indent=2, sort_keys=True).encode('utf-8') + b'\n'}


def check_conformance(annotations: dict, conformance: dict, *, schema: str, run_case) -> list[str]:
    """Run every conformance case through the model; return all problems found."""
    problems: list[str] = []
    supported = {rule for entry in annotations['entries']
                 for p in entry['propositions'] for rule in p.get('rules', [])}
    if conformance.get('schema') != schema:
        return ['conformance: unsupported schema']
    covered: set[str] = set()
    for case in conformance['cases']:
        name = case['id']
        rules = set(case['rules'])
        covered |= rules
        for rule in sorted(rules - supported):
            problems.append(f'{name}: cites rule {rule} that no annotated source statement supports')
        actual = run_case(case)
        if actual != case['expect']:
            problems.append(f'{name}: expected {case["expect"]!r}, model produced {actual!r}')
    for rule in sorted(supported - covered):
        problems.append(f'rule {rule} is supported by source statements but has no conformance case')
    return problems
