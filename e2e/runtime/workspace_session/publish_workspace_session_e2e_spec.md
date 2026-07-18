# `publish_workspace_session` Live-Docker E2E Test Specification

Status: Draft; tests not yet implemented
Date: 2026-07-18
Feature contract: [publish_workspace_session_spec.md](./publish_workspace_session_spec.md)
Suite root: `e2e/runtime/workspace_session/`

## 1. Purpose

This document defines the external live-Docker proof for the proposed public
`publish_workspace_session` operation. It covers the end-user CLI contract,
LayerStack effects, session retention or closure, merge behavior, concurrency,
and leak-free teardown.

It is additive to the historical [test_spec.md](./test_spec.md). That document
continues to describe and prove the existing finalize-policy family; it must not
be overwritten by this proposal.

## 2. Scope and test boundary

### In scope for this external suite

- Public runtime CLI discovery, argument validation, request, and response.
- Successful publish-and-close for a representative multi-kind delta.
- Empty publish-and-close without a new LayerStack revision.
- Active-command refusal and successful retry after the command stops.
- Protected/capture-drop rejection, atomicity, and retained-session recovery.
- Stale-base clean text merge, conflicting text, and ineligible binary merge.
- Existing explicit discard compatibility.
- Duplicate request behavior and same-session terminal-action races.
- Parallel publishes from independent sessions.
- Public-result hygiene, LayerStack revision evidence, file visibility, audit
  ownership, and teardown.

### Owned elsewhere

This repository has no MCP client runner and no supported daemon failpoint for
capture or workspace-destroy failures. The following are mandatory release
coverage, but not ad hoc black-box Docker tests:

- MCP tools list, JSON schema, request forwarding, structured success, and
  structured errors: `crates/sandbox-mcp/tests/server.rs`.
- Capture failure, fail-next-publish storage failure, post-commit destroy
  failure, no-op destroy failure, state restoration, autosquash count/order,
  and non-retryability after commit: product Rust operation/integration tests.
- Terminal action/modal, responsive states, retained conflict, no-op success,
  and partial-success cleanup UI: `web/console/tests/browser/P06TerminalFixture.spec.ts`.

No case may inspect daemon storage directly, scrape logs, or call a private
capture/publish route as its system under test.

## 3. Baseline prerequisite

The checked-in product currently exposes `create_workspace_session` and
`destroy_workspace_session` through its runtime catalog, CLI, and MCP. The
existing external case `WS-05` still asserts that both operations are private.
Before running the new feature gate:

1. reconcile `WS-05` with the actual public catalog;
2. preserve WS-04's proof that destroy is discard-only;
3. do not weaken any implicit-finalizer cases, especially EX-05 and FP-02;
4. treat [test_spec.md](./test_spec.md)'s older visibility statement as
   historical rather than as the new source of truth.

This mismatch is a prerequisite correction, not part of
`publish_workspace_session` behavior.

## 4. Expected folder changes

### This spec-only change

```text
e2e/runtime/workspace_session/
├── publish_workspace_session_spec.md       [add]
└── publish_workspace_session_e2e_spec.md   [add]
```

### Follow-up executable test change

```text
e2e/runtime/workspace_session/
├── helpers.py                              [modify]
├── test_workspace_session.py               [modify: reconcile WS-05]
├── test_publish_workspace_session.py       [add]
├── publish_workspace_session_spec.md       [existing]
├── publish_workspace_session_e2e_spec.md   [existing]
└── test_spec.md                            [existing historical proof]
```

Do not add the new tests to `e2e/metadata/stable-id-ledger.json`. That ledger
maps historical IDs; new declarations use their own unique immutable semantic
IDs.

## 5. Harness contract

### 5.1 Operation under test

Every publish invocation goes through the public runtime CLI wrapper:

```python
def publish_session(sandbox_id, workspace_session_id, *, grace_s=None, timeout=180):
    args = ["--workspace-session-id", workspace_session_id]
    if grace_s is not None:
        args += ["--grace-s", str(grace_s)]
    return runtime(
        sandbox_id,
        "publish_workspace_session",
        *args,
        timeout=timeout,
    )
```

The operation under test must never use `direct_daemon`. Existing trusted
session setup/cleanup may initially reuse `WorkspaceTracker.create_session()`
and its guarded destroy helper so a publish test does not accidentally test two
new public paths at once. Once WS-05 is reconciled, migrating lifecycle setup
to public CLI wrappers is preferred.

### 5.2 Tracker behavior

Add `WorkspaceTracker.publish()` with these rules:

- untrack the session only on a normal success response;
- keep tracking it when `session_retained: true`;
- keep tracking it after a post-commit close failure and clean it with guarded
  destroy;
- if a concurrent loser reports session-not-found, confirm the session is
  absent before untracking it;
- never convert a publish error into a cleanup success.

The existing `cleanup()` remains the last-resort, case-owned discard path. It
must interrupt only commands tracked by the case and destroy only workspace IDs
tracked by the case.

### 5.3 Revision snapshot

Add a small helper that normalizes the public observability LayerStack value:

```python
{
    "manifest_version": stack["manifest_version"],
    "root_hash": stack["root_hash"],
    "layer_ids": [layer["layer_id"] for layer in stack["layers"]],
    "layer_count": len(stack["layers"]),
}
```

Assertions compare before, after, and the publish response. They must not read
`manifest.json` or layer directories from the Docker container.

### 5.4 Waiting and concurrency

- Poll asynchronous command/session state with `time.monotonic()` deadlines.
- Do not use a fixed sleep as proof of completion.
- Use an explicit barrier for races and parallel publishes.
- Record every allowed race outcome, then assert one complete terminal
  disposition, at most one layer, and no leaked session.
- Keep smoke cases independent and under 60 seconds as a family.

### 5.5 Declarations and verdicts

Every test has one `@e2e_test` declaration with:

```python
features=("runtime.workspace_session",)
execution_surface="cli"
owner_id="e2e-core"
```

Use the semantic IDs in the catalog below. `CaseRecorder` records
`correctness` and `teardown`; PWS-04, PWS-11, and PWS-12 also record `timing`.
No assertion checkpoint is satisfied by merely receiving a 2xx response.

## 6. Coverage catalog

| Case | Tier | Immutable declaration ID | Budget | Primary contract |
| --- | --- | --- | ---: | --- |
| PWS-01 | smoke | `runtime.workspace-session.publish.surface` | 4 s | CLI discovery and required arguments |
| PWS-02 | smoke | `runtime.workspace-session.publish.changed` | 8 s | multi-kind commit, response, closure, visibility, audit |
| PWS-03 | smoke | `runtime.workspace-session.publish.no-op` | 5 s | empty no-op closes without LayerStack change |
| PWS-04 | smoke | `runtime.workspace-session.publish.active-command` | 12 s | admission refusal, retention, retry |
| PWS-05 | medium | `runtime.workspace-session.publish.protected-atomic` | 8 s | protected path rejects whole delta and retains session |
| PWS-06 | medium | `runtime.workspace-session.publish.clean-merge` | 10 s | two stale sessions merge disjoint text edits |
| PWS-07 | medium | `runtime.workspace-session.publish.conflict-retry` | 12 s | structured conflict, retained editability, one resolved retry |
| PWS-08 | medium | `runtime.workspace-session.publish.binary-conflict` | 10 s | non-text stale divergence rejects without loss |
| PWS-09 | medium | `runtime.workspace-session.publish.destroy-compat` | 6 s | existing destroy remains discard-only |
| PWS-10 | medium | `runtime.workspace-session.publish.validation-replay` | 8 s | invalid/unknown input and no duplicate publish |
| PWS-11 | hard | `runtime.workspace-session.publish.disposition-race` | 20 s | publish-vs-destroy serialization |
| PWS-12 | hard | `runtime.workspace-session.publish.parallel-disjoint` | 30 s | independent concurrent session publishes |
| PWS-13 | medium | `runtime.workspace-session.publish.special-file` | 8 s | unsupported special file blocks publish and retains all changes |

The budgets are declaration timeouts, not performance targets. A timeout is a
failure and must retain the case artifacts.

## 7. Detailed live scenarios

### PWS-01 — public CLI surface

Arrange:

1. Start one ready sandbox.
2. Capture top-level runtime help and operation-specific help.

Act and assert:

1. Top-level help lists `publish_workspace_session` exactly once.
2. Operation help shows required `--workspace-session-id` and optional
   `--grace-s` with the publish-and-close description.
3. Invoking it without the session ID returns structured `invalid_request`.
4. The validation failure creates no workspace and does not change the
   LayerStack revision.

MCP discovery is not inferred from this CLI proof; it remains separately owned.

### PWS-02 — changed publish commits and closes

Arrange:

1. Create a sandbox from a case-local base containing `edit.txt`, `delete.txt`,
   and `keep.txt`.
2. Create one explicit session.
3. Inside it, update `edit.txt`, delete `delete.txt`, create a directory and
   nested regular file, and create a relative symlink. Leave `keep.txt`
   unchanged.
4. Record the session view, active LayerStack revision, layer IDs, and snapshot.

Act: call public `publish_workspace_session --grace-s 1`.

Assert:

- `workspace_session_id` matches, `publish.no_op` is false,
  `destroyed` is true, and `evicted_upperdir_bytes >= 0`;
- revision fields have correct types, route counts are non-negative, and at
  least one route was accepted;
- manifest version and layer count advance by exactly one, root hash changes,
  and the response revision equals the public observability revision;
- a fresh sessionless read sees the update, absence, new file, and symlink
  target, while the unchanged file remains byte-identical;
- `file_blame` for a changed text line is owned by
  `workspace_session:<published-id>`;
- the old session ID rejects command, file, second publish, and destroy calls;
- recursively serialized output contains none of `manifest`, `layer_paths`,
  mount paths, or host paths;
- teardown reports no case-owned workspace.

### PWS-03 — empty publish is a no-op that closes

Arrange: record the active lease count, create an explicit session, and make no
changes. Then record manifest version, root hash, and ordered layer IDs.

Act: publish the session.

Assert:

- success has `publish.no_op: true`, route counts `0/0`, and `destroyed: true`;
- returned revision equals the current public revision;
- manifest version, root hash, and ordered layer IDs are unchanged;
- the active lease count returns to its pre-session value;
- the old session ID is gone.

This case intentionally differs from historical FP-02's implementation detail
that an implicit empty capture skips the lower publish call. The observable
LayerStack and close result stay compatible.

### PWS-04 — active command refusal, then retry

Arrange:

1. Create a session and write a private sentinel.
2. Start `sleep 30` in the session with `yield_time_ms=0`; track its command ID.
3. Record LayerStack state.

Act: publish while the command is running.

Assert the refusal:

- error kind is `operation_failed`;
- details contain the session ID and exactly the active command ID;
- LayerStack state is byte-for-byte equivalent at the public revision level;
- the sentinel is still readable inside the session and absent globally;
- observability still reports an active, usable session.

Then interrupt the command, wait for its terminal response using a monotonic
deadline, publish again, and assert one commit, global sentinel visibility, and
session closure.

### PWS-05 — protected path rejects atomically

Arrange: in one session write both `safe.txt` and a forbidden LayerStack path
such as `manifest.json`. Record the active revision and layer IDs.

Act: publish.

Assert:

- error details have `stage: "publish"`, `session_retained: true`, exact
  rejected path, and `publish_rejection.reason: "protected_path"`;
- version, root hash, and layer IDs are unchanged;
- neither path is visible globally, proving no partial changeset escaped;
- both changes remain visible inside the session;
- a normal session command and file edit still work after the rejection.

Cleanup with explicit discard and prove `safe.txt` never becomes global.

### PWS-06 — stale-base clean text merge

Use the already-proven deterministic pattern:

```text
base notes.txt:  one\ntwo\n
session A:       ONE\ntwo\n
session B:       one\ntwo\ntail\n
```

Create A and B from the same base, make the two changes, publish A, then publish
B. Assert both return non-no-op success, the manifest advances by exactly two,
both sessions close, and final content is `ONE\ntwo\ntail\n`. Blame must retain
A's ownership on line 1, `original` on line 2, and B's ownership on the tail.

### PWS-07 — overlapping conflict, retain, resolve, retry

Use two sessions from `one\ntwo\n`:

```text
session A: ALPHA\ntwo\n
session B: BRAVO\ntwo\n
```

Publish A, snapshot LayerStack, then publish B.

Assert the first B attempt:

- `operation_failed`, `stage: "publish"`, and `session_retained: true`;
- rejection reason and nested conflict are `source_conflict` on `notes.txt`;
- expected and actual fingerprints are structured and different;
- LayerStack is unchanged from immediately after A;
- global content is A's version while retained B still reads and edits BRAVO.

Resolve B inside the retained session to `ALPHA\ntwo\nB-tail\n`, then retry once.
The retry must merge, create exactly one additional layer, close B, and produce
that exact final content. Total delta from the original base is two layers, not
three.

### PWS-08 — stale binary divergence is not guessed

Seed a small file containing a NUL byte, then create sessions A and B from the
same base. Write different binary bytes at that path in each session using
`exec_command`. Publish A and then B.

Assert A succeeds, B returns a structured `source_conflict` for the binary path,
the revision does not change on B's attempt, global bytes remain A's bytes, and
B remains readable/usable until case-owned discard. No text auto-merge or
replacement decoding is allowed.

### PWS-09 — destroy remains discard-only

Create a session, write a unique sentinel, record LayerStack, and call the
existing public `destroy_workspace_session`. Assert its normal destroy response,
unchanged LayerStack state, absent global sentinel, and stale publish returning
session-not-found. This case guards against implementing publish by changing
destroy semantics.

### PWS-10 — validation, unknown ID, and replay

Use one valid explicit session plus a known-absent ID.

1. Empty `workspace_session_id` returns `invalid_request`.
2. Negative `grace_s` on the valid session returns `invalid_request`; the valid
   session and its private delta remain untouched.
3. Unknown ID returns `operation_failed` with that ID and no LayerStack change.
4. Publish the valid session successfully.
5. Repeat publish with its now-closed ID; receive session-not-found and no
   second revision.

Assert no invalid or replayed call captures, publishes, destroys another
session, or changes the layer list.

### PWS-11 — publish versus discard race

Create one changed session. Release two threads from one barrier: one calls
public publish and one calls public destroy.

Exactly one of these complete outcomes is allowed:

| Winner | Losing result | LayerStack | Global sentinel |
| --- | --- | --- | --- |
| publish | destroy reports session-not-found | exactly `+1` layer | present |
| destroy | publish reports session-not-found | unchanged | absent |

Assert one success, one valid loser, no partial response, no session leak, no
duplicate audit/layer, and continued daemon health through a fresh create and
destroy cycle. Record both raw responses and the barrier-to-terminal duration.

### PWS-12 — parallel disjoint session publishes

Create six sessions from the same base with autosquash configured so this case
cannot cross its trigger threshold. Session `i` writes only
`parallel/session-i.txt`. Release six public publish calls from a barrier.

Assert all six succeed, each closes its own session, the manifest advances by
exactly six, every unique file has exact content, every changed file has its
corresponding `workspace_session:<id>` owner, and no session is leaked. A
serialized commit order is expected; a prescribed thread completion order is
not.

### PWS-13 — unsupported special file blocks the whole publish

Inside one session create `regular.txt` and a FIFO such as `run.fifo`. Publish
and assert:

- `operation_failed`, `session_retained: true`, and no LayerStack change;
- rejection reason `protected_path` with
  `protected_drop.reason: "unsupported_special_file"` and exact path;
- `regular.txt` remains private and the FIFO still exists inside the session;
- neither change is visible globally.

Discard the retained session. This pins the explicit-operation safety policy
and prevents silent loss when closing.

## 8. Cross-case invariants

Every case must enforce the applicable invariants below:

1. A rejected request never advances manifest version, changes root hash, or
   adds a layer ID.
2. A committed non-no-op request advances exactly once.
3. No-op success closes but does not advance LayerStack.
4. Pre-commit failure retains the complete session delta and restores command
   and file admission.
5. Success removes the session from observability and all public session-bound
   operations.
6. Public response revision agrees with public observability.
7. A fresh view, not the publishing session, proves global visibility.
8. Public errors are asserted by structured fields, never message text alone.
9. No result exposes internal manifests, paths, or container storage details.
10. Case cleanup cannot delete a workspace, command, or sandbox it did not
    create.

## 9. Evidence and verdict artifacts

Each `PWS-*` case writes under:

```text
.e2e-state/reports/workspace-session/<RUN_ID>/<CASE_ID>/
```

Required artifacts when applicable:

```text
case.json
request.json
response.json
stack-before.json
stack-after.json
session-before.json
session-after.json
global-verification.json
retained-session-verification.json
race-responses.json
timers.json
teardown-snapshot.json
verdict.json
```

`correctness` includes response, atomicity, revision, content, and lifecycle
assertions. `teardown` proves every case-owned session is absent. `timing` is
used only for bounded command/race completion; it is not a throughput score.

Raw file contents should be bounded to the small deterministic fixtures in this
spec. Never persist secrets, environment dumps, or daemon logs as artifacts.

## 10. Product-only failure and surface matrix

| ID | Owner | Required proof |
| --- | --- | --- |
| FI-01 | runtime operation test | Inject capture failure; state returns to `Active`, delta remains, no publish/destroy. |
| FI-02 | runtime/LayerStack integration | Inject fail-next-publish storage error; no active revision change, session restored and retryable. |
| FI-03 | runtime operation test | Commit then inject destroy failure; partial-success fields are exact, state is `FinalizeFailed`, republish cannot duplicate, guarded destroy recovers. |
| FI-04 | runtime operation test | No-op then inject destroy failure; `publish_completed: true`, `layer_committed: false`, guarded destroy recovers. |
| FI-05 | runtime operation test | Count autosquash notification: one after real commit even if destroy fails; zero for no-op/rejection. |
| FI-06 | runtime operation test | A command admission blocked behind a failed publish gate proceeds only after state returns to `Active`. |
| OBS-01 | runtime/daemon/query tests | Serialize `active`, `finalizing`, and `finalize_failed` through the public workspace snapshot; preserve `finalize_failed` until guarded destroy. |
| MCP-01 | sandbox-mcp test | Tool list and fixture include the operation once; schema has two required IDs, optional numeric grace, no extra properties. |
| MCP-02 | sandbox-mcp test | Valid request forwards exact sandbox-scoped args and returns structured success. |
| MCP-03 | sandbox-mcp test | Validation does not dispatch; runtime rejection returns `isError: true` with unmodified structured details. |
| UI-01 | Console browser test | Desktop rail and narrow drawer expose accessible publish action and active-command disabled state. |
| UI-02 | Console browser test | Commit/no-op success removes the row, refreshes snapshot/LayerStack, and selects Quick run when needed. |
| UI-03 | Console browser test | Conflict retains the row and selection with path/retry/discard guidance. |
| UI-04 | Console browser test | Post-commit close failure says published/cleanup-required and exposes only discard recovery before and after page reload. |

These cases may not be marked as externally covered merely because a happy-path
CLI case passed.

## 11. Implementation sequence

1. Add `publish_session`, revision helpers, tracker semantics, and PWS metadata
   without changing existing case behavior.
2. Reconcile the stale WS-05 visibility assertion against the current product
   catalog.
3. Implement PWS-01 through PWS-04 and run only the smoke slice.
4. Implement rejection cases PWS-05, PWS-07, PWS-08, and PWS-13 one feature at
   a time; retain full artifacts on each failure.
5. Implement merge and compatibility cases PWS-06, PWS-09, and PWS-10.
6. Implement PWS-11, verify its complete allowed-outcome table, then implement
   PWS-12.
7. Run the new file, then the complete workspace-session family, then the wider
   runtime regression gate.

Do not weaken exact field, revision, retention, or teardown assertions to make
an intermittent run pass.

## 12. Live-Docker runbook

Before each real test command, append the intended slice, product revision,
gateway build state, and expected proof to `.e2e-state/TEST-REPORT.md`. After
the command, append the result and artifact path; never rewrite prior entries.

From the external E2E root:

```sh
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/e2e
```

Use the repository's real gateway and canonical roots:

```sh
python3 -m pytest runtime/workspace_session/test_publish_workspace_session.py \
  -m smoke \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Then run the focused file:

```sh
python3 -m pytest runtime/workspace_session/test_publish_workspace_session.py \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

After all focused cases pass:

```sh
python3 -m pytest runtime/workspace_session \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Only then run the broader runtime tree. Rebuild the Docker gateway binary when
the product runtime changed; stale containers are not acceptable proof.

## 13. Entry and exit gates

### Entry gates

- Product catalog, runtime dispatch, CLI projection, and gateway binary include
  `publish_workspace_session`.
- The Docker gateway is healthy and rebuilt from the product revision under
  test.
- WS-05 matches the current public lifecycle surface.
- Product fault-injection tests for pre- and post-commit failure are green.
- Public observability contract tests preserve the finalization state through
  runtime, daemon adapter, query JSON, and Console typing.
- MCP schema/dispatch tests and Console fixture tests are green.

### Exit gates

- PWS-01 through PWS-13 pass with one verdict per executed case.
- Focused smoke, focused full file, and the full workspace-session family pass.
- Existing WS, EX, and FP assertions remain unchanged in strength.
- Every rejected case proves unchanged LayerStack and a retained or absent
  session exactly as specified.
- Every success case proves fresh-view visibility and old-session closure.
- Every case has a passing teardown axis with no tracked workspace leak.
- No case depends on daemon log text, direct storage inspection, an arbitrary
  sleep, or a private publish call.
- `.e2e-state/TEST-REPORT.md` contains append-only command/result records and
  points to the retained verdict artifacts.

## 14. Requirement traceability

| Feature acceptance | Primary live proof | Additional owner |
| --- | --- | --- |
| AC-01 | PWS-01 | MCP-01, MCP-02 |
| AC-02 | PWS-02 | runtime operation tests |
| AC-03 | PWS-03 | FI-04, FI-05 |
| AC-04 | PWS-02, PWS-03 | MCP-02 |
| AC-05 | PWS-04 | FI-06 |
| AC-06 | PWS-05, PWS-13 | LayerStack/unit tests |
| AC-07 | PWS-06, PWS-07, PWS-08 | LayerStack merge tests |
| AC-08 | PWS-04, PWS-05, PWS-07, PWS-08, PWS-13 | FI-01, FI-02 |
| AC-09 | PWS-07 | runtime operation tests |
| AC-10 | — | FI-03, FI-04, OBS-01, UI-04 |
| AC-11 | PWS-11, PWS-12 | runtime gate tests |
| AC-12 | PWS-09 plus existing EX/FP family | existing unit/E2E suites |
| AC-13 | PWS-02 | FI-05, MCP-03 |
| AC-14 | — | UI-01 through UI-04 |

An em dash means the behavior cannot be made deterministic at the external
boundary with the current supported harness. Its named product test is a
release requirement, not an optional skip.
