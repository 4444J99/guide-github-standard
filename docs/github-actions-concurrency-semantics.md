# GitHub Actions `concurrency` semantics from GitHub Docs

Second GitHub Docs slice that is both extracted and executable, after the
[`GITHUB_TOKEN` permission semantics](github-token-permission-semantics.md). An executable model
(`ges/concurrency_semantics.py`) plus a source-bound ledger
(`evidence/semantics/github-actions-concurrency/`) built with the shared machinery in
`ges/doc_ledger.py`. It does not review the 13,219-file corpus (see
[GitHub Docs source scope](github-docs-source-scope.md)).

Why this slice: the [agent verification receipt](agent-verification-receipts.md) workflow
serialises its job with `concurrency: group: agent-receipt-<pr>, cancel-in-progress: true`. The
model derives what that guarantees (a newer head cancels the in-flight receipt run of the same pull
request and no other) instead of asserting it.

## Status and limits

Every record is `PROPOSED` with no reviewer. Structural validity, source binding and model
conformance do not certify that the prose was read correctly, and the documentation is not a
specification of GitHub's server behaviour. No policy is adopted and no live GitHub behaviour was
observed. Where the prose is silent, hedged or contradictory the model says so (see Ambiguities).

Source: `github/docs` at `56fcfa816f27bca239e5d39fff0d4f74f77ec995`, four files, 17
occurrences, 17 propositions. 15 propositions name a model rule; 2 are preserved without one. The
ledger stores hashes and formalized ASTs, not the prose.

## The model

A run is an opaque id. A group is a (repository, case-folded name) pair holding at most one
`running` run and an ordered list of `pending` runs.

| Rule | Statement | Function |
| --- | --- | --- |
| K1 | The group name is case insensitive (`prod` = `Prod`) | `group_key` |
| K2 | At most one run is running in a group | `submit`, `finish` |
| K3 | A run queued while another is in progress in the same group (in the repository) is pending | `submit` |
| K4 | By default an existing pending run is canceled and replaced by the new one | `submit` |
| K5 | `cancel-in-progress: true` also cancels the running run (and any pending run); the new run starts | `submit` |
| K6 | `queue: max`: up to 100 pending runs; any additional run is canceled | `submit`, `MAX_PENDING` |
| K7 | `queue: max` with `cancel-in-progress: true` is a workflow validation error | `validate_concurrency` |
| K8 | Pending runs start first-in-first-out by the time each began waiting | `finish` |
| K9 | With no `concurrency` key, runs of one workflow, repository or owner may all run together | `submit` with no spec |
| K10 | Group expression contexts: `github`, `inputs`, `vars` at workflow level; plus `needs`, `strategy`, `matrix` at job level | `check_group_expression` |
| K11 | A group is scoped to the repository, not the workflow; workflows sharing a name share the group | `group_key` |
| K12 | `github.head_ref` is defined only on `pull_request`; other events need a fallback such as `github.run_id` | `head_ref_group` |
| K13 | `queue` is `single` (default) or `max`, documented where `actions-nga` applies | `validate_concurrency` |

`cancel-in-progress` must already be an evaluated boolean: the docs give its allowed contexts but
no evaluation rules, so the model does not evaluate expressions. Where the `actions-nga` flag does not
apply, the text states one running and one pending run (the same default), and the model rejects the
`queue` key.

## Conformance

`evidence/semantics/github-actions-concurrency/conformance.json` holds 41 cases: value validation,
expression contexts, `head_ref` fallbacks and run scenarios (submit and finish sequences with the
expected status changes and final group state). `check_conformance` fails when the model disagrees
with a case, when a case cites a rule no source statement supports, and when a rule the sources
support has no case. `tests/test_concurrency_semantics.py` adds random histories that must keep one
running run per group, at most 100 pending runs, one slot per run, and an exact accounting of live
runs.

```
python -m ges concurrency-semantics conformance
python -m ges concurrency-semantics validate
python -m ges concurrency-semantics verify --sources PINNED_DOCS_CHECKOUT
python -m ges concurrency-semantics build  --sources PINNED_DOCS_CHECKOUT [--output DIR]
```

`verify` rebuilds the ledger from the four pinned files and fails on any difference. CI exercises
the same code on a synthetic source; the real files are not vendored.

## Consequence for the receipt workflow

Asserted in tests against the workflow block in `docs/agent-verification-receipts.md`: its group
expression uses only `github`, is valid at job level, and with `cancel-in-progress: true` a run for a
newer head cancels the earlier run of the same pull request while a run for another pull request is
untouched. Cancellation stops the canceled run in stages and does not fence a write request that was
already sent, so a stale receipt run can still write after a newer one has started; the model does not
establish otherwise. The head-SHA binding the action itself enforces is a separate control.

## Ambiguities preserved

- The docs describe FIFO order and in the same note say ordering is not guaranteed. The model
  implements strict FIFO as the stated intent; this is not claimed to be observable.
- The sentence "without defining a particular concurrency group ... will cancel any in-progress run"
  contradicts the example above it, which defines `group: ${{ github.ref }}`. It is preserved
  without a rule.
- The docs are silent on `queue` where `actions-nga` does not apply; the model rejects the key
  rather than assume it is accepted.
- Whether `pull_request_target` and the review events define `github.head_ref` is unstated; the
  model follows the sentence literally.
- The conditional `cancel-in-progress` example (release branches) depends on expression evaluation
  and is preserved without a rule.

## Not covered

Expression evaluation, the `workflow_run` and reusable-workflow concurrency interactions, the
concurrency-groups REST reference, runner-level concurrency limits, reviewer approval of these
propositions, and any comparison with live GitHub behaviour.
