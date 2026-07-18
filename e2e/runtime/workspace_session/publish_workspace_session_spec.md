# Explicit Workspace-Session Publish Specification

Status: Draft
Date: 2026-07-18
Target operation: `publish_workspace_session`
Target product: `ephemeral-sandbox`
Companion coverage: [publish_workspace_session_e2e_spec.md](./publish_workspace_session_e2e_spec.md)

## 1. Decision

Add a public runtime operation named `publish_workspace_session`. It captures
the unpublished changes of one explicit `no_op` workspace session, validates
and merges them against the current LayerStack head, and then closes the
session.

This is deliberately a terminal action:

```text
publish_workspace_session = publish safely + close
destroy_workspace_session = discard + close
```

The operation does not keep the session open after a successful commit or a
successful no-op. A pre-commit failure keeps the session open so the caller can
inspect, repair, retry, or discard it. A failure after LayerStack commit is
reported as partial success and leaves only `destroy_workspace_session` as the
recovery action.

## 2. Context and current gap

An explicit session is created with `finalize_policy: "no_op"`. Commands and
file operations can accumulate private changes there, while
`destroy_workspace_session` intentionally discards those changes. A bare
`exec_command` has a separate implicit `publish_then_destroy` lifecycle, but
that lifecycle is not an acceptable substitute for publishing a long-lived
explicit session.

The current product already contains the required lower-level primitives:

- workspace capture returns a base manifest, base revision, changes, and
  protected drops;
- LayerStack serializes writers and validates the whole changeset atomically;
- stale plain-text writes can use a safe three-way merge;
- overlapping, non-text, structural, and otherwise ineligible divergence is
  rejected as a source conflict;
- workspace sessions already have `Active`, `Finalizing`, and
  `FinalizeFailed` lifecycle states plus a guarded discard path.

The new operation composes those primitives with explicit-session failure
semantics. It must not reuse the implicit finalizer's policy of destroying the
session after a capture or publish failure.

### Baseline correction

The current product catalog and CLI projection expose both
`create_workspace_session` and `destroy_workspace_session` publicly. The older
[test_spec.md](./test_spec.md) and its WS-05 case describe an earlier boundary
where the operations were internal-only. Implementation of this proposal must
reconcile that historical test before treating the workspace-session E2E family
as a release gate.

## 3. Goals

1. Give CLI, MCP, and Console users one explicit way to persist a session's
   complete unpublished delta to LayerStack.
2. Preserve concurrent LayerStack changes whenever the existing safe merge can
   do so without guessing.
3. Never partially publish a rejected changeset.
4. Make it unambiguous whether a failed request committed a layer and whether
   the session remains recoverable.
5. Keep the existing discard and implicit-finalization behavior backward
   compatible.
6. Avoid exposing host paths, manifests, or other runtime-internal storage
   details.

## 4. Non-goals

- A `--force` option or last-writer-wins conflict resolution.
- Publishing while keeping the same workspace session open.
- Letting callers choose `publish_then_destroy` when creating an explicit
  session.
- Changing `destroy_workspace_session` into a publish operation.
- Changing the implicit bare-`exec_command` finalizer.
- A cross-sandbox merge, branch model, commit message, or LayerStack history UI.
- Silent omission of changes that the LayerStack cannot represent.

## 5. Terminology

| Term | Meaning |
| --- | --- |
| explicit session | A caller-created session with `finalize_policy: "no_op"`. |
| base | LayerStack revision and manifest mounted when the session was created or last remounted. |
| active head | The current LayerStack revision when publish acquires the writer lock. |
| capture | Conversion of the session upperdir into a LayerStack changeset plus protected drops. |
| pre-commit failure | Any validation, admission, capture, merge, or storage error before a new active revision exists. |
| post-commit failure | Failure to close the workspace after LayerStack has accepted the publish result. |
| no-op publish | A valid publish that creates no new layer and returns the current active revision. |

## 6. Public operation contract

### 6.1 Catalog entry

| Field | Value |
| --- | --- |
| name | `publish_workspace_session` |
| family | `workspace_session` |
| execution owner | runtime |
| summary | Publish an explicit workspace session and close it. |
| related | `create_workspace_session`, `destroy_workspace_session`, `exec_command` |

Required operation description:

> Capture the unpublished changes of an explicit workspace session, merge them
> safely into the current LayerStack when possible, and close the session.
> Rejected or failed pre-commit publishes retain the session.

### 6.2 Arguments

| Argument | Type | Required | Validation | Meaning |
| --- | --- | --- | --- | --- |
| `workspace_session_id` | string | yes | non-empty | Explicit session to publish and close. |
| `grace_s` | number | no | finite and `>= 0` | Optional grace period used only by the close step. |

The runtime transport continues to require `sandbox_id`. Argument validation
must finish before session admission, capture, LayerStack publication, or
destroy is attempted.

### 6.3 CLI

Usage:

```sh
sandbox-runtime-cli --sandbox-id ID \
  publish_workspace_session \
  --workspace-session-id ID \
  [--grace-s SECONDS]
```

Example:

```sh
sandbox-runtime-cli --sandbox-id eos-373f2c73-9f70-4d04-ac65-6c8f3979eb86 \
  publish_workspace_session \
  --workspace-session-id ws-1 \
  --grace-s 1
```

The command emits the common structured JSON response. Human-readable log text
is not part of the contract.

### 6.4 MCP

The MCP tool is generated from the same runtime operation catalog; it is not a
second handwritten operation. Its input schema is equivalent to:

```json
{
  "type": "object",
  "properties": {
    "sandbox_id": {
      "type": "string",
      "description": "Sandbox containing the workspace session."
    },
    "workspace_session_id": {
      "type": "string",
      "description": "Explicit workspace session to publish and close."
    },
    "grace_s": {
      "type": "number",
      "description": "Optional non-negative close grace period in seconds."
    }
  },
  "required": ["sandbox_id", "workspace_session_id"],
  "additionalProperties": false
}
```

Successful MCP calls return the same structured value as the CLI. Failed calls
set MCP `isError: true` and carry the normal `{error: {kind, message, details}}`
value as structured content.

The Phase Zero `runtime-tools-list.json` compatibility fixture remains
unchanged: it pins historical tools, not the complete current list. MCP tests
extend the current expected-name list and add direct schema/dispatch assertions
for the new tool while continuing to compare every historical tool exactly.

## 7. Success response

### 7.1 Committed changes

```json
{
  "workspace_session_id": "ws-1",
  "publish": {
    "no_op": false,
    "revision": {
      "manifest_version": 42,
      "root_hash": "example-root-hash",
      "layer_count": 7
    },
    "route_summary": {
      "source_count": 3,
      "ignored_count": 0
    }
  },
  "destroyed": true,
  "evicted_upperdir_bytes": 4096
}
```

Required invariants:

- exactly one atomic layer is committed for a non-empty accepted changeset;
- the returned revision is the active revision produced by that publish;
- `destroyed` is the literal boolean `true`;
- the workspace session no longer accepts command, file, publish, or destroy
  operations;
- the publish audit owner is `workspace_session:<workspace_session_id>`;
- `manifest`, `layer_paths`, mount paths, and host filesystem paths are never
  returned.

### 7.2 Empty capture

The operation still calls LayerStack publication with the captured base and an
empty changeset. On success it returns:
- `publish.no_op: true`;
- the current active revision;
- zero source and ignored routes;
- `destroyed: true` and the evicted upperdir byte count.

No manifest version, root hash, or layer list change occurs, and autosquash is
not notified. The session is still closed.

## 8. Error contract

All failures use the normal response envelope:

```json
{
  "error": {
    "kind": "operation_failed",
    "message": "human-readable summary",
    "details": {}
  }
}
```

### 8.1 Invalid input and unknown session

- Missing, empty, or wrongly typed `workspace_session_id`, and non-finite or
  negative `grace_s`, return `invalid_request` with no side effects.
- An unknown or already-closed session returns `operation_failed` with
  `details.workspace_session_id` and no side effects.

### 8.2 Active commands

Publish acquires the existing per-session admission gate. If the command
ledger is non-empty, it returns:

```json
{
  "error": {
    "kind": "operation_failed",
    "message": "workspace session has active command sessions",
    "details": {
      "workspace_session_id": "ws-1",
      "active_command_session_ids": ["cmd-1"]
    }
  }
}
```

No capture, publish, or destroy occurs. The session remains `Active`.

### 8.3 Pre-commit failure

Capture errors, LayerStack validation or storage errors, protected changes,
and merge conflicts return `operation_failed` with these stable details:

```json
{
  "error": {
    "kind": "operation_failed",
    "message": "workspace session publish was rejected",
    "details": {
      "workspace_session_id": "ws-1",
      "stage": "publish",
      "session_retained": true,
      "publish_rejection": {
        "path": "notes.txt",
        "reason": "source_conflict",
        "source_conflict": {
          "path": "notes.txt",
          "expected": {"kind": "file", "digest": "...", "executable": false},
          "actual": {"kind": "file", "digest": "...", "executable": false}
        },
        "protected_drop": null,
        "message": null
      }
    }
  }
}
```

`stage` is `capture` or `publish`. `publish_rejection` is present only for a
classified LayerStack rejection and preserves this shape:

| Field | Allowed values |
| --- | --- |
| `reason` | `invalid_base_revision`, `protected_path`, `source_conflict`, `opaque_dir_protected_descendant`, `opaque_dir_mixed_routes`, `opaque_dir_expansion_limit`, `route_preparation_failed` |
| `protected_drop.reason` | `unsupported_special_file`, `invalid_layer_path`, `command_scratch_path` |

Every pre-commit error restores the session from `Finalizing` to `Active`. The
active LayerStack revision and visible content are unchanged, and the whole
private delta remains readable and editable inside the session.

Explicit publish treats every capture drop, including an unsupported special
file, as blocking. This deliberate operation-layer preflight prevents a
successful response from silently losing a session change. The implicit
bare-command finalizer retains its existing policy.

### 8.4 Post-commit close failure

Once LayerStack has committed, the operation cannot roll the layer back. A
close failure returns a partial-success error:

```json
{
  "error": {
    "kind": "operation_failed",
    "message": "workspace session published but could not be closed",
    "details": {
      "workspace_session_id": "ws-1",
      "stage": "destroy",
      "publish_completed": true,
      "layer_committed": true,
      "publish": {
        "no_op": false,
        "revision": {
          "manifest_version": 42,
          "root_hash": "example-root-hash",
          "layer_count": 7
        },
        "route_summary": {
          "source_count": 3,
          "ignored_count": 0
        }
      },
      "destroyed": false,
      "session_state": "finalize_failed",
      "recovery_operation": "destroy_workspace_session"
    }
  }
}
```

For a no-op whose close fails, `publish_completed` remains true while
`layer_committed` is false. In both forms:

- the session does not admit command, file, or another publish operation;
- retrying publish must not create a duplicate layer;
- guarded `destroy_workspace_session` remains available for cleanup;
- autosquash is notified exactly once if and only if a layer was committed.

Public workspace observability must report
`finalization_state: "finalize_failed"` until guarded destroy succeeds. This
field is separate from the existing activity-oriented `lifecycle_state`, so a
Console refresh cannot accidentally re-enable command or publish actions after
the partial-success response is no longer in local component state.

## 9. Lifecycle algorithm

The operation runs under the existing per-session gate and must not hold the
sessions-map mutex across workspace or LayerStack I/O.

```text
1. Validate input.
2. Acquire the session admission gate.
3. Resolve the session and reject a non-empty command ledger.
4. Change Active -> Finalizing and take an I/O-safe handler snapshot.
5. Capture the complete upperdir delta.
6. Reject every capture drop, or publish the complete changeset to LayerStack.
7. On any pre-commit failure: Finalizing -> Active; return session_retained.
8. On commit or valid no-op: destroy the captured session snapshot.
9. If a layer committed, notify autosquash once after the destroy attempt.
10a. Destroy success: remove the session and return success.
10b. Destroy failure: Finalizing -> FinalizeFailed; return partial success.
```

State transitions:

```text
Active -- publish admitted --> Finalizing
Finalizing -- pre-commit failure --> Active
Finalizing -- publish/no-op + destroy success --> removed
Finalizing -- publish/no-op + destroy failure --> FinalizeFailed
FinalizeFailed -- destroy_workspace_session --> removed
```

## 10. Merge, atomicity, and concurrency

- LayerStack's writer lock establishes commit order.
- Every captured change is planned and resolved before any part of the new
  changeset becomes active.
- Disjoint or byte-identical stale plain-text edits may merge cleanly.
- Overlapping divergent text edits return `source_conflict`.
- Concurrent divergence involving binary, invalid UTF-8, files larger than the
  merge limit, delete-vs-edit, symlink, directory, or other structural changes
  is not guessed; it returns `source_conflict`.
- A protected path or capture drop rejects the complete changeset, including
  otherwise safe paths.
- Publish, destroy, and new command admission for the same session serialize
  on the session gate. Exactly one terminal disposition wins.
- Concurrent publishes from different sessions serialize at LayerStack and
  independently revalidate against the active head.

## 11. Existing-operation compatibility

| Surface | Required behavior after this change |
| --- | --- |
| `create_workspace_session` | Still creates only explicit `no_op` sessions. No publish policy argument is added. |
| `destroy_workspace_session` | Still discards unpublished changes and closes. It never publishes. |
| bare `exec_command` | Still creates an implicit `publish_then_destroy` session and retains its current failure policy. |
| session-targeted commands/files | Continue to retain private changes until explicit publish or discard. |
| LayerStack publish | Keeps current atomic validation, merge, audit, and writer serialization. |
| autosquash | Receives one notification for a real committed layer and none for rejection or no-op. |

## 12. Console behavior

The terminal page at `/sandboxes/:sandboxId/terminal` is owned by the separate
`ephemeral-sandbox-console` repository. The concrete local URL from this
proposal maps to the same parameterized route. Each persisted explicit session
gets one lifecycle control in `SessionSidebar`.

### 12.1 Session-row lifecycle control

- Keep one 44-pixel row action rather than crowding the 16-rem desktop rail
  with separate publish and trash icons.
- Accessible name: `Close workspace session <id>`.
- The icon and tooltip communicate session completion rather than immediate
  deletion; activating it opens the decision dialog and performs no mutation.
- Disable it while the session has active commands, with the existing "stop
  active commands first" explanation.

### 12.2 Confirmation and progress

The modal is titled `Close workspace session` and states:

> Choose what happens to this session's unpublished changes. Publishing merges
> them into the latest LayerStack snapshot when safe, then closes `<id>`.

The choices are:

- primary `Publish to LayerStack & close`, invoking
  `publish_workspace_session`;
- danger `Discard & close`, invoking the unchanged
  `destroy_workspace_session`;
- `Cancel`, which performs no operation.

Confirmation by retyping the ID is not required because the dialog makes the
publish/discard choice explicit before either request. While a request is
running, all modal and row lifecycle controls for that session are disabled and
the active action reads `Publishing…` or `Discarding…`.

### 12.3 Result handling

- Commit success: select `Quick run` if the published session was selected,
  refresh the workspace snapshot and LayerStack queries, remove the row, and
  show the committed revision.
- No-op success: remove the row and show `No changes to publish; session
  closed`.
- Active-command or pre-commit error: keep the row and current selection;
  display the structured path/reason and explain that the session was retained.
- Source conflict: offer guidance to inspect/edit the retained session and
  retry publish, or choose discard in the same dialog.
- Post-commit close failure: show `Published; cleanup required`, refresh
  LayerStack, disable command/publish actions for the failed session, and leave
  only discard recovery.
- Reloading the page preserves that cleanup-only state from the public
  workspace snapshot's `finalization_state` field.
- Narrow-screen drawer and desktop rail expose the same action and states.

## 13. Observability and security

- Trace capture, publish, and destroy as separate stages under one operation
  request correlation.
- Record the session ID, stage, `no_op`, revision, route counts, committed flag,
  and cleanup outcome; do not record file contents.
- Add `finalization_state` to public workspace snapshots with the stable values
  `active`, `finalizing`, and `finalize_failed`. Existing `lifecycle_state`
  remains backward compatible and retains its current activity meaning.
- Treat the new operation as a manager mutation. Normal publish/no-op success
  advances sandbox activity revision, and a post-commit partial-success error
  also advances it because the layer or session finalization state changed.
  Rejected pre-commit requests do not advance it.
- Publish audit ownership is `workspace_session:<id>` and is appended only
  after commit.
- The operation is sandbox-scoped and cannot address a session in another
  sandbox.
- Public results and errors never disclose the base manifest, active manifest,
  layer paths, mount paths, cgroup paths, or host filesystem paths.

## 14. Expected implementation footprint

The exact split may follow local module conventions, but the expected tracked
surface is:

```text
ephemeral-sandbox/
├── crates/sandbox-operations/catalog/src/
│   ├── runtime.rs                                  [modify: export + route]
│   └── runtime/workspace_session.rs                [modify: spec + args]
├── crates/sandbox-operations/catalog/tests/
│   ├── integrity.rs                                [modify: routed catalog]
│   └── runtime.rs                                  [modify: names + args]
├── crates/sandbox-cli/
│   ├── src/projection/runtime.rs                   [modify: CLI projection]
│   └── tests/
│       ├── runtime.rs                              [modify: help + request]
│       └── fixtures/runtime-help.txt               [modify: additive help]
├── crates/sandbox-runtime/operation/src/
│   ├── observability.rs                            [modify: expose finalization state]
│   ├── workspace_session/error.rs                   [modify: publish errors]
│   ├── workspace_session/service.rs                 [modify: exports]
│   ├── workspace_session/service/model.rs           [modify: result model]
│   ├── workspace_session/service/snapshot.rs        [modify: snapshot state]
│   ├── workspace_session/service/impls/
│   │   ├── mod.rs                                   [modify]
│   │   └── publish_session.rs                       [add]
│   └── operations/registry/
│       ├── workspace_session_operations.rs          [modify: parse/dispatch/JSON]
│       └── command_operations.rs                    [modify or refactor shared rejection JSON]
├── crates/sandbox-runtime/operation/tests/          [modify: lifecycle, publish, fault, snapshot]
├── crates/sandbox-manager/
│   ├── src/router/forward.rs                     [modify: mutation accounting]
│   └── tests/manager_router.rs                   [modify: activity revision]
├── crates/sandbox-daemon/
│   ├── src/observability/adapter.rs              [modify: map state]
│   └── tests/unit/observability.rs               [modify: state contract]
├── crates/sandbox-observability/query/src/
│   ├── ports.rs                                    [modify: query model]
│   └── response.rs                                 [modify: public JSON]
├── crates/sandbox-observability/query/tests/query.rs [modify: public JSON tests]
└── crates/sandbox-mcp/tests/server.rs                [modify: list/schema/dispatch/errors]

ephemeral-sandbox-console/
├── Cargo.toml                                         [modify: pin implemented core revision]
├── Cargo.lock                                         [modify: resolve same revision]
├── server/tests/console/catalog.rs                    [modify: additive operation]
└── web/
    ├── src/api/observability.ts                       [modify: finalization state]
    ├── src/api/types.ts                               [modify: publish result/error types]
    ├── src/pages/sandbox/terminal/
    │   ├── SessionSidebar.tsx                     [modify: lifecycle flow]
    │   └── CloseWorkspaceSessionDialog.tsx        [add: publish/discard choice]
    └── tests/browser/P06TerminalFixture.spec.ts       [modify: behavior + visual coverage]
```

No low-level LayerStack change or MCP-only implementation module is expected:
the current atomic publisher, catalog projection, and shared gateway request
builder carry the operation through. Phase Zero compatibility catalog/tool
fixtures remain unchanged; only additive current-surface expectations change.

## 15. Acceptance criteria

| ID | Criterion |
| --- | --- |
| AC-01 | CLI help/catalog and MCP tools list expose `publish_workspace_session` with the defined arguments. |
| AC-02 | A valid non-empty session delta commits exactly one atomic layer and closes the session. |
| AC-03 | A valid empty delta returns the current revision as `no_op` and closes without changing LayerStack. |
| AC-04 | The response contains only the public result fields and matches the active LayerStack revision. |
| AC-05 | Active commands reject publish before capture and identify every active command ID. |
| AC-06 | Protected paths and capture drops reject the complete delta and retain the session. |
| AC-07 | Disjoint stale text changes merge; overlapping or ineligible divergence conflicts without partial commit. |
| AC-08 | Every pre-commit failure restores an editable `Active` session and leaves LayerStack unchanged. |
| AC-09 | Conflict resolution followed by one retry can commit once and close the retained session. |
| AC-10 | A post-commit destroy failure reports partial success, exposes durable `finalize_failed` observability, prevents republish, and allows guarded discard recovery. |
| AC-11 | Same-session publish/destroy/command races serialize to one valid outcome with no leak or duplicate layer. |
| AC-12 | `destroy_workspace_session` remains discard-only and implicit command finalization remains unchanged. |
| AC-13 | Audit ownership, autosquash notification count, manager activity revision, and sensitive-field hygiene match this spec. |
| AC-14 | Console desktop and narrow layouts expose publish, retained-error, success/no-op, and cleanup-required states accessibly. |

The feature is release-ready only when the companion E2E exit gates and the
product-level fault-injection/MCP/UI ownership matrix are satisfied.
