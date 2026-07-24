# Live E2E Test Report

### Iteration 1 - proc namespace contract

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T162950+0800-observability-cgroup-proc-contract PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_contract.py::test_proc_topology_contract -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T162950+0800-observability-cgroup-proc-contract/pytest.log
```

**Good** - Public CLI transport, sandbox creation, and run-scoped teardown completed; artifact: `e2e/.artifacts/20260717T162950+0800-observability-cgroup-proc-contract/pytest.log` (1 case, 2.65s).

**Defect** - `observability.cgroup.proc-contract` failed because the manager returned its synthetic schema-v2 unavailable placeholder instead of daemon topology.

**Fix** - Trace and correct explicit sandbox cgroup routing/merge, rebuild the affected host binary, then rerun only this case.

---

### Iteration 19 - daemon disk polling specification and admission-test validation

**Command**
```bash
PYTHONPATH=e2e .venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  --collect-only -q \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Pending: validate the two stable declarations, fixture generator,
direct 120-second test guards, and 220-second aggregate declared budget.

**Defect** - The current manager-ring implementation does not yet implement the
proposed `source: daemon_disk` route or daemon `resource_stats` configuration.

**Fix** - Keep the admission test strict and red on architecture mismatch; do
not mark it xfail or accept manager-owned resource series.

---

### Iteration 12 - focused resource estimate result and final family plan

**Focused result** - `observability.cgroup.workspace-resource-estimates` passed (1/1, 3.36s) on newly created sandbox `eos-de66a259-e85d-471e-aef0-97576c5c6687`; fixture teardown destroyed it. Public RSS was positive, cumulative CPU advanced, and start identity agreed with the later read-only procfs measurement. Artifact: `e2e/.artifacts/20260717T173855+0800-observability-cgroup-estimates/pytest.log`.

**Final command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T174042+0800-observability-cgroup-estimates-final PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T174042+0800-observability-cgroup-estimates-final/pytest.log
```

**Good** - Planned: run all 10 stable cgroup cases against the rebuilt production daemon.

**Defect** - None in the focused case.

**Fix** - Execute the full family, then perform the new-sandbox memory and console demonstration.

---

### Iteration 11 - estimated workspace resource inputs

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T173855+0800-observability-cgroup-estimates PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup/test_workspace_placement.py::test_workspace_resource_estimate_inputs_are_live -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T173855+0800-observability-cgroup-estimates/pytest.log
```

**Good** - Planned: validate positive RSS, advancing cumulative CPU, and start identity through the packaged public CLIs and a read-only procfs oracle.

**Defect** - Pending execution.

**Fix** - Build and reload the production stack, run this focused case, then run the complete cgroup family without weakening assertions.

---

### Iteration 2 - proc namespace contract after manager merge fix

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163215+0800-observability-cgroup-proc-contract-rerun PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_contract.py::test_proc_topology_contract -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163215+0800-observability-cgroup-proc-contract-rerun/pytest.log
```

**Good** - `observability.cgroup.proc-contract` passed (1/1, 2.39s); schema v2 was available through the public CLI; artifact: `e2e/.artifacts/20260717T163215+0800-observability-cgroup-proc-contract-rerun/pytest.log`.

**Defect** - None.

**Fix** - Continue with the smallest idle-workspace selection.

---

### Iteration 3 - idle workspace visibility

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163253+0800-observability-cgroup-idle PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_workspace_placement.py::test_idle_workspaces_remain_visible -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163253+0800-observability-cgroup-idle/pytest.log
```

**Good** - `observability.cgroup.workspace-idle` passed (1/1, 2.41s); two idle holders remained visible and distinct; artifact: `e2e/.artifacts/20260717T163253+0800-observability-cgroup-idle/pytest.log`.

**Defect** - None.

**Fix** - Continue with dual-namespace two-workspace placement.

---

### Iteration 4 - two-workspace dual namespace placement

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163325+0800-observability-cgroup-placement PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_workspace_placement.py::test_processes_are_placed_by_dual_namespace_identity -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163325+0800-observability-cgroup-placement/pytest.log
```

**Good** - `observability.cgroup.workspace-process-placement` passed (1/1, 1.66s); public topology was disjoint and Docker `stat` independently matched PID and mount namespace device/inode pairs; artifact: `e2e/.artifacts/20260717T163325+0800-observability-cgroup-placement/pytest.log`.

**Defect** - None.

**Fix** - Continue with backend-originated and forked-descendant placement.

---

### Iteration 5 - backend commands and descendants

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163410+0800-observability-cgroup-descendants PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_workspace_placement.py::test_backend_originated_command_appears e2e/observability/cgroup/test_workspace_placement.py::test_forked_descendants_stay_with_workspace -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163410+0800-observability-cgroup-descendants/pytest.log
```

**Good** - `observability.cgroup.backend-originated-command` and `observability.cgroup.forked-descendants` passed (2/2, 2.63s); artifact: `e2e/.artifacts/20260717T163410+0800-observability-cgroup-descendants/pytest.log`.

**Defect** - None.

**Fix** - Continue with process-exit and workspace-destroy lifecycle cases.

---

### Iteration 6 - process and workspace lifecycle

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163439+0800-observability-cgroup-lifecycle PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_lifecycle.py::test_exited_processes_disappear e2e/observability/cgroup/test_lifecycle.py::test_destroyed_workspace_disappears_without_reassignment -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163439+0800-observability-cgroup-lifecycle/pytest.log
```

**Good** - `observability.cgroup.process-exit` and `observability.cgroup.workspace-destroy` passed (2/2, 6.47s); exited PIDs and destroyed holders disappeared without reassignment; artifact: `e2e/.artifacts/20260717T163439+0800-observability-cgroup-lifecycle/pytest.log`.

**Defect** - None.

**Fix** - Continue with cgroup-independence and bounded churn.

---

### Iteration 7 - cgroup independence and natural churn

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163517+0800-observability-cgroup-independence-churn PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup/test_contract.py::test_process_topology_is_cgroup_independent e2e/observability/cgroup/test_lifecycle.py::test_natural_process_churn_preserves_topology -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163517+0800-observability-cgroup-independence-churn/pytest.log
```

**Good** - `observability.cgroup.read-only-independent` and `observability.cgroup.concurrent-churn` passed (2/2, 7.14s); shared/root cgroup membership did not gate topology and natural PID churn preserved availability; artifact: `e2e/.artifacts/20260717T163517+0800-observability-cgroup-independence-churn/pytest.log`.

**Defect** - None.

**Fix** - Perform the required public-CLI two-workspace manual verification, then run the family once as the final proof.

---

### Iteration 8 - manual public-CLI acceptance verification

**Command**
```bash
set -o pipefail; PYTHONPATH=e2e .venv/bin/python e2e/.artifacts/20260717T163634+0800-observability-cgroup-manual/manual_verify.py --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163634+0800-observability-cgroup-manual/manual.log
```

**Good** - Manual verification passed in 9.28s: schema v2 available from `proc_namespaces`; workspaces had disjoint PIDs 58/62; both PID and mount device/inode pairs matched their own holders and differed across workspaces; both commands disappeared to idle; one destroyed workspace vanished while its peer remained. The live cgroup mount was read-only with `0::/`. Cleanup destroyed both workspaces and sandbox `eos-8a8fe215-928e-47b8-8077-6913d8b68510`. Artifact: `e2e/.artifacts/20260717T163634+0800-observability-cgroup-manual/manual.log`.

**Defect** - None.

**Fix** - Run the complete cgroup family once as the final live proof.

---

### Iteration 9 - final observability cgroup family proof

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T163816+0800-observability-cgroup-final PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox 2>&1 | tee e2e/.artifacts/20260717T163816+0800-observability-cgroup-final/pytest.log
```

**Good** - The complete `e2e/observability/cgroup` family passed (9/9, 19.28s). All nine run-owned sandboxes were destroyed by fixture teardown. Artifact: `e2e/.artifacts/20260717T163816+0800-observability-cgroup-final/pytest.log`.

**Defect** - None.

**Fix** - Complete final static verification and the run-owned resource audit, then close out the implementation.

---

### Iteration 10 - merged-main Docker proof and web-console demo

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T170651+0800-observability-cgroup-main PYTHONPATH=e2e .venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /tmp/eos-topology-e2e.uaA3dl/main --product-root /tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T170651+0800-observability-cgroup-main/pytest.log
```

**Execution correction**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T170651+0800-observability-cgroup-main PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T170651+0800-observability-cgroup-main/pytest.log
```

**Good** - The complete merged-main `e2e/observability/cgroup` family passed in the Docker sandbox (9/9, 22.28s). Fixture teardown destroyed all nine run-owned sandboxes. A retained two-workspace console demo independently matched holder/process PID and mount namespace device/inode identities and remained available with a read-only cgroup mount and `0::/` membership. Artifacts: `e2e/.artifacts/20260717T170651+0800-observability-cgroup-main/pytest.log` and `/Users/yifanxu/Ephemeral-AI-Lab/observability-cgroup-demo-workspace-2-20260717.png`.

**Defect** - The temporary main worktree lacked generated `dist/git` archives, and the first pytest launcher used non-canonical `/tmp` roots. Neither failure reached a product assertion.

**Fix** - Reused the repository's packaged Git toolchains through `SANDBOX_GIT_TOOLCHAIN_DIR`, rebuilt and reloaded the merged-main Docker gateway/console, and reran with canonical `/private/tmp` roots without weakening assertions.

---

### Iteration 13 - estimated resource inputs full-family proof

**Command**
```bash
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T174042+0800-observability-cgroup-estimates-final PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T174042+0800-observability-cgroup-estimates-final/pytest.log
```

**Good** - The complete rebuilt-daemon `e2e/observability/cgroup` family passed (10/10, 21.17s), including independently measured live RSS/cumulative CPU inputs. Fixture teardown destroyed all ten run-owned sandboxes. Artifact: `e2e/.artifacts/20260717T174042+0800-observability-cgroup-estimates-final/pytest.log`.

**Defect** - None.

**Fix** - Continue with a fresh retained console demo and sustained-memory verification.

---

### Iteration 14 - fresh demo and bounded daemon-memory verification

**Command**
```bash
bin/sandbox-manager-cli create_sandbox --image ubuntu:24.04 --workspace-bind-root /private/tmp/eos-topology-e2e.uaA3dl/main/.e2e-state/workspaces/templates/testbed
bin/sandbox-runtime-cli --sandbox-id eos-c248f5c4-9812-4364-b24f-380d19d60b7a create_workspace_session
bin/sandbox-runtime-cli --sandbox-id eos-c248f5c4-9812-4364-b24f-380d19d60b7a create_workspace_session
bin/sandbox-observability-cli cgroup --sandbox-id eos-c248f5c4-9812-4364-b24f-380d19d60b7a --scope sandbox --window-ms 60000
```

**Good** - A new sandbox from the rebuilt packaged daemon exposed two disjoint active workspaces in schema v2. The real console displayed live workspace estimates (about 3.1MiB/11.4% and 1.6MiB/0.0%) with `0::/` cgroup membership and no browser warnings or errors. After warm-up, 2,000 repeated public cgroup requests increased daemon PID 7 RSS from 4,512KiB to 6,248KiB with a sharply decelerating rate (996KiB in the first 250 calls, 220KiB in the last 1,000) and no background sampler, cache, or retained per-workspace history. Artifacts: `e2e/.artifacts/20260717T175000+0800-observability-cgroup-memory/memory-growth.log` and `/Users/yifanxu/Ephemeral-AI-Lab/observability-cgroup-estimates-demo-20260717.png`.

**Defect** - None. The bounded collector and two-sample browser calculation necessarily use transient request memory; the live proof found allocator warm-up rather than linear per-request retention.

**Fix** - Retained only the explicitly requested demo sandbox `eos-c248f5c4-9812-4364-b24f-380d19d60b7a` with workspaces `00000118c30a776b940fec` and `00000218c30a776c147ff8`; all automated-test resources were cleaned by the shared fixtures.

---

### Iteration 15 - planned final streamed-parser rebuild proof

**Command**
```bash
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH SANDBOX_GIT_TOOLCHAIN_DIR=/Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox/dist/git bin/start-sandbox-console-stack --rebuild-binary
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T175200+0800-observability-cgroup-streamed-final PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T175200+0800-observability-cgroup-streamed-final/pytest.log
```

**Good** - Pending: rebuild and reload the final daemon/console, then exercise every stable cgroup case on new fixture-owned sandboxes.

**Defect** - The memory review found that parsing `/proc/<pid>/stat` allocated a temporary vector of all fields even though only three fields are used.

**Fix** - Stream the bounded stat fields directly, rerun Rust tests/clippy, rebuild production assets, rerun the full live family, and create the retained demo only after that reload.

---

### Iteration 16 - planned retained-demo memory proof

**Command**
```bash
bin/sandbox-runtime-cli --sandbox-id eos-83be30a7-8d2a-4771-b5e9-72341d769c73 create_workspace_session
bin/sandbox-runtime-cli --sandbox-id eos-83be30a7-8d2a-4771-b5e9-72341d769c73 create_workspace_session
bin/sandbox-observability-cli cgroup --sandbox-id eos-83be30a7-8d2a-4771-b5e9-72341d769c73 --scope sandbox --window-ms 60000
```

**Good** - Pending: seed the final rebuilt-daemon sandbox, verify the public schema-v2 response and estimates, and measure daemon RSS across bounded repeated requests.

**Defect** - None known.

**Fix** - Keep the final demo only if placement, console rendering, and non-linear retained-memory checks pass; clean the superseded run-owned demo afterward.

---

### Iteration 17 - planned bounded cgroup-reader verification

**Command**
```bash
cargo test -p sandbox-observability-telemetry -p sandbox-observability-query
cargo clippy -p sandbox-observability-telemetry -p sandbox-observability-query --all-targets -- -D warnings
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH SANDBOX_GIT_TOOLCHAIN_DIR=/Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox/dist/git bin/start-sandbox-console-stack --rebuild-binary
```

**Good** - Pending: prove the final daemon's cgroup request memory is independent of unrelated persisted observability history, then rerun the complete live family and retain one final two-workspace demo.

**Defect** - The sustained check showed `Reader::samples()` materialized every parsed record plus a duplicate raw line; its peak allocation therefore grew with the append-only observability log.

**Fix** - Stream both log files and retain only matching in-window samples, preserving timestamp sort and counter deltas. Use the same public-operation stress as the before/after reproduction.

---

### Iteration 18 - final bounded-reader, live-family, and retained-demo proof

**Command**
```bash
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH SANDBOX_GIT_TOOLCHAIN_DIR=/Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox/dist/git bin/start-sandbox-console-stack --rebuild-binary
set -o pipefail; CGROUP_E2E_ARTIFACT_DIR=e2e/.artifacts/20260717T184000+0800-observability-cgroup-bounded-reader-final PYTHONPATH=e2e /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/.venv/bin/python -m pytest e2e/observability/cgroup -vv --test-repository-root /private/tmp/eos-topology-e2e.uaA3dl/main --product-root /private/tmp/eos-topology-product.rMhibO/main 2>&1 | tee e2e/.artifacts/20260717T184000+0800-observability-cgroup-bounded-reader-final/pytest.log
```

**Good** - The final packaged daemon (`sha256=e18e88e6e14782e21549191b962c8d3fe3e7fb5d88fd634f10c0dfda880a09cf`) passed the complete live cgroup family (10/10, 20.48s); shared fixture teardown destroyed all ten test sandboxes. The retained two-workspace sandbox `eos-83bb1085-f127-426f-bb2b-7b7ae9d1a535` returned available schema-v2 topology, disjoint holder/PID/mount namespaces, live RSS/cumulative CPU inputs, `0::/` membership, and 48 manager resource samples. Across 2,000 sequential public cgroup calls, daemon RSS was 6,700KiB after warm-up, 7,112KiB after 1,000, and 7,304KiB after 2,000; the final 1,000 added only 192KiB while threads stayed 40 and file descriptors stayed 32. Artifacts: `e2e/.artifacts/20260717T184000+0800-observability-cgroup-bounded-reader-final/pytest.log` and `e2e/.artifacts/20260717T180300+0800-observability-cgroup-memory-final/memory-growth.log`.

**Defect** - The before-fix reproduction grew from 7,284KiB after warm-up to 10,144KiB after 2,000 calls, then 22,168KiB after another 1,000 calls as the append-only log grew. This isolated `Reader::samples()` materializing unrelated persisted records, not topology or per-process estimate retention.

**Fix** - Stream rotated and primary logs with one reusable line buffer and retain only matching in-window samples before sorting/delta calculation. The console keeps only the current topology, previous successful topology, and current estimate map; no background sampler or resource history was added.

---

### Iteration 20 - daemon disk polling static and fixture validation

**Command**
```bash
.venv/bin/python -m py_compile \
  e2e/observability/resource_isolation/daemon_disk_polling_helpers.py \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py
PYTHONPATH=e2e .venv/bin/python fixture-size-probe.py \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Pending: compile both modules and stream-generate the small,
near-rotation, and exact-segment-cap fixtures with no oversized line.

**Defect** - None known in the test implementation.

**Fix** - Correct only the new helper or test files if a static or fixture
invariant fails; preserve unrelated resource-efficiency work.

---

### Iteration 21 - final daemon disk polling collection gate

**Command**
```bash
.venv/bin/python -m py_compile \
  e2e/observability/resource_isolation/daemon_disk_polling_helpers.py \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py
PYTHONPATH=e2e .venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  --collect-only -q \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Pending: revalidate compilation and both catalog declarations after
separating strict size checks from the stable post-append parseability gate.

**Defect** - Product conformance remains intentionally unclaimed until v3
daemon resource files and routing are implemented.

**Fix** - Report collection success separately from the expected current-main
live architecture gap.

---

### Iteration 22 - focused live daemon disk polling admission run

**Command**
```bash
E2E_REBUILD_BINARY=0 PYTHONPATH=e2e .venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py -q \
  --timeout=120 --session-timeout=600 \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Pending: exercise both load-bearing cases through the public CLI and
verify exact-ID teardown plus baseline gateway restoration.

**Defect** - Expected on current main: the proposed daemon-owned
`resource_stats` store and `source: daemon_disk` response are not implemented.

**Fix** - Preserve the first architecture-boundary failure as evidence; do not
weaken the admission oracle.

---

### Iteration 23 - resource-route spec and admission collection gate

**Command**
```bash
.venv/bin/python -m py_compile \
  e2e/observability/resource_isolation/daemon_disk_polling_helpers.py \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py
PYTHONPATH=e2e .venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  --collect-only -q \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Both load-bearing daemon-disk cases compiled and collected through
the repository's observability catalog (2 tests collected in 0.03s). The
pytest-timeout plugin is active, and the module-level declarations keep both
110-second case budgets below the 120-second per-test and 600-second session
limits.

**Defect** - None in static compilation or collection. Product conformance is
not claimed because current main still serves sandbox resources from the
manager ring and does not implement the daemon resource store.

**Fix** - Implement the coordinated config, resource store, daemon sampler,
query handler, sandbox route, and console contract before running the focused
live admission suite.

---

### Iteration 24 - focused daemon resource implementation checks

**Command**
```bash
cargo test -p sandbox-config --test unit configs::observability
cargo test -p sandbox-observability-telemetry --test collect cgroup_read
cargo test -p sandbox-observability-telemetry --test sink strict_append_drops_oversized_samples_without_a_marker_or_write
cargo test -p sandbox-observability-telemetry --test sink boundary_append_rotates_before_write
cargo test -p sandbox-observability-telemetry --test reader resource_samples_stream_rotated_then_active_without_mutating_either_segment
cargo test -p sandbox-operation-catalog --tests
cargo test -p sandbox-observability-query --test query resources_read_only_the_dedicated_daemon_store_and_preserve_unknown_metrics
cargo test -p sandbox-manager --test manager_router resources
cargo test -p sandbox-daemon --features jemalloc --lib resources::tests
npm test -- ResourcesPolling.test.tsx
```

**Good** - Pending: validate the new configuration bounds, strict two-segment
store, partial cgroup parsing, bounded read-only query, split route ownership,
manager pass-through, cgroup target resolution, and console polling lifecycle.

**Defect** - None known after focused compilation; live product behavior remains
unclaimed until the packaged daemon is rebuilt and both Docker cases pass.

**Fix** - Correct only failures in the new resource path, preserve unrelated
dirty E2E work, and record exact focused counts and timings before live proof.

---

### Iteration 25 - corrected focused selectors and supported Node runtime

**Command**
```bash
cargo test -p sandbox-config --test unit observability_tests
cargo test -p sandbox-operation-catalog --all-features --tests
cargo test -p sandbox-manager --test manager_router daemon_quiescent
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - The Iteration 24 telemetry collector checks passed 4/4; strict
oversize, boundary rotation, pure reader, daemon-disk query, and missing-daemon
manager route checks each passed 1/1; daemon cgroup target resolution passed
3/3. Rust build/test wall times were respectively 2.32s, 0.50s, 0.06s,
0.52s, 3.82s, 6.64s, and 16.57s (including compilation and lock waits).

**Defect** - The original sandbox-config selector and featureless operation
catalog command each selected zero tests. The console worker also failed before
collection under the shell's Node 22.7.0 because a CommonJS jsdom dependency
attempted to require an ESM-only module.

**Fix** - Select the real `observability_tests` module, enable every catalog
feature, explicitly exercise the 10,000-read quiescence case, and rerun Vitest
with the installed supported Node 22.22.0 runtime without changing product or
test dependencies.

---

### Iteration 26 - console visibility simulation correction

**Command**
```bash
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - Configuration validation passed 7/7 in 0.01s; the all-feature
catalog suites passed 12/12 in 4.29s; the 10,000-iteration manager quiescence
case passed in 1.00s. The supported Node runtime collected all three resource
polling tests, and the one-in-flight/unmount-abort case passed.

**Defect** - Two console assertions failed. The new test used jest-dom's
`toBeInTheDocument` although this repository intentionally installs no
jest-dom matcher. Its hidden-tab helper also changed `document.hidden` but not
`document.visibilityState`; TanStack's focus manager reads the latter, treated
the synthetic event as a focus event, and initiated a second fetch.

**Fix** - Use the repository's native truthy DOM assertion and make the test's
visibility simulation internally consistent by changing both standard document
properties before dispatching `visibilitychange`.

---

### Iteration 27 - console focus-event and resolved-fetch flush correction

**Command**
```bash
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - The consistent hidden state now prevents every scheduled background
fetch, confirming the polling implementation honors tab visibility. The
one-in-flight/unmount case remains green.

**Defect** - The synthetic `visibilitychange` was dispatched on `document`,
while the installed TanStack focus manager subscribes on `window`, so the
visible transition did not trigger catch-up. The immediately resolved third
mock fetch was invoked, but its React render completed after the timer callback
because that callback does not return the internal fetch promise.

**Fix** - Dispatch the visibility event at the focus manager's actual listener
boundary and flush the resolved query microtask/render once after advancing the
poll timer.

---

### Iteration 28 - deterministic recovered-response settlement

**Command**
```bash
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - Hidden polling and visible catch-up now pass together, as does the
one-in-flight/unmount-abort case.

**Defect** - The third request was invoked exactly once, but an immediate mock
resolution still settled outside React's `act` boundary because TanStack's
interval callback starts rather than returns the fetch promise; another zero-ms
timer advance did not establish a settlement boundary.

**Fix** - Model the recovered request as an explicit deferred promise, assert
that it is the third and only request, then resolve it inside the test and flush
the resulting state transition deterministically.

---

### Iteration 29 - final contract-audit focused checks

**Command**
```bash
cargo test -p sandbox-observability-query --test query resources
cargo test -p sandbox-observability-telemetry --test reader resource
cargo test -p sandbox-daemon --features jemalloc --lib observability::resources::tests
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - Pending: verify bounded partial responses for empty and partially
collected stores, a stressed whole-response cap, concurrent reads during real
rotation, sampler storage-failure isolation and joined shutdown, plus the
previously green three-case console lifecycle.

**Defect** - The final spec audit found that empty resource storage was labeled
`unavailable` instead of the required `partial`. It also found that partial
controller reads were not elevated into the response error list, and that the
focused suite lacked direct concurrent read/write, response-stress,
storage-failure, and sampler-shutdown proofs.

**Fix** - Use the stable `available|partial` daemon-disk contract, surface the
latest bounded cgroup error, and add direct coverage without changing the
sampler cadence, store budget, routing, or console polling architecture.

---

### Iteration 30 - corrected console package directory

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-console/web
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test -- ResourcesPolling.test.tsx
```

**Good** - The Iteration 29 Rust checks passed: daemon resource queries 4/4 in
0.14s, resource reader cases 2/2 in 1.04s, and daemon sampler/path cases 5/5 in
0.01s. These include the stressed 256 KiB/500-record response, concurrent
rotation, empty/partial semantics, storage failure isolation, and joined
sampler shutdown.

**Defect** - The console command was launched from the console repository root,
which has no `package.json`; npm returned ENOENT before test collection.

**Fix** - Rerun the unchanged command from the repository's `web` package using
the same supported Node 22.22.0 runtime.

---

### Iteration 31 - affected-package and collection gate

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo clippy --all-targets --features sandbox-daemon/jemalloc
cargo test \
  -p sandbox-config \
  -p sandbox-operation-catalog \
  -p sandbox-manager \
  -p sandbox-observability-telemetry \
  -p sandbox-observability-query \
  -p sandbox-daemon \
  --features sandbox-daemon/jemalloc

cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-console/web
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm test
PATH=/Users/yifanxu/.nvm/versions/node/v22.22.0/bin:$PATH npm run build

cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
.venv/bin/python -m py_compile \
  e2e/observability/resource_isolation/daemon_disk_polling_helpers.py \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py
PYTHONPATH=e2e .venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  --collect-only -q \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - The corrected focused console run passed 3/3 in 1.55s after asset
verification. Pending: lint every workspace target with the daemon allocator
feature used by its tests, run all tests in the six affected Rust packages,
run the complete console suite and production build, and recollect exactly the
two budgeted live cases.

**Defect** - None known in the implementation after the focused audit gate.

**Fix** - Correct any affected-package or collection regression before the
repository-required packaged-daemon gateway rebuild.

---

### Iteration 32 - sampler lifecycle tracker isolation

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo test -p sandbox-daemon --features jemalloc --test unit \
  observability_tests::daemon_connection_task_tracker_increments_and_returns_to_idle
cargo test \
  -p sandbox-config \
  -p sandbox-operation-catalog \
  -p sandbox-manager \
  -p sandbox-observability-telemetry \
  -p sandbox-observability-query \
  -p sandbox-daemon \
  --features sandbox-daemon/jemalloc
```

**Good** - Workspace all-target clippy passed in 7.93s with only two existing
layerstack dead-code warnings. In the first broad test command, all 79 config
tests and the daemon library's 5 sampler tests passed; 98/99 daemon integration
tests passed.

**Defect** - `daemon_connection_task_tracker_increments_and_returns_to_idle`
timed out because the new long-lived sampler occupied the established RPC/HTTP
connection tracker. This changed an observable connection-count invariant even
though the sampler itself was healthy.

**Fix** - Give the sampler a dedicated lifecycle tracker local to the listener
serve phase, cancel and join it during the same shutdown boundary, and leave the
existing tracker exclusively responsible for connection metrics. Recheck the
exact regression before restarting the complete affected-package command.

---

### Iteration 33 - manager pass-through matrix expectation

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo test -p sandbox-manager --test manager_router \
  manager_router_forwards_every_sandbox_observability_route
cargo test \
  -p sandbox-config \
  -p sandbox-operation-catalog \
  -p sandbox-manager \
  -p sandbox-observability-telemetry \
  -p sandbox-observability-query \
  -p sandbox-daemon \
  --features sandbox-daemon/jemalloc
```

**Good** - The exact connection-tracker regression passed 1/1 in 0.05s. The
restarted broad gate then passed config 79/79, daemon library 5/5, daemon
integration 99/99, manager core 18/18, and manager export 31/31. The manager
router reached 20/21 green, including the new daemon-only resource route and
10,000-read quiescence checks.

**Defect** - The generic "forward every sandbox observability route" test still
expected every fake daemon response to carry a legacy `forwarded: true` marker.
The dedicated `resources` fixture intentionally returns the stable
`source: daemon_disk` payload unchanged, so that marker is correctly absent.

**Fix** - Keep invocation-count and scope assertions for the entire matrix; for
`resources`, assert the daemon source and fixture metric survive intact and
that the manager adds no forwarding marker. Then restart the complete gate.

---

### Iteration 34 - final static and packaging qualification

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo fmt --all -- --check
cargo clippy --all-targets --features sandbox-daemon/jemalloc
git diff --check
bin/start-sandbox-docker-gateway --rebuild-binary
```

**Good** - The repaired complete affected-Rust gate passed 377/377 tests in
about 46s, including strict rotation under a 100,000-record concurrent writer.
The complete console gate passed 101/101 tests across 31 files in 4.99s and its
production build completed in 4.45s. Python compilation succeeded and exact
live collection selected only DP-01 and DP-02 (2/2 collected in 0.02s).

**Defect** - None known after the complete affected-package, console, build,
and collection gates. This iteration qualifies the final source tree and
rebuilds the exact daemon artifact that the live gateway will serve.

**Fix** - If formatting, all-target linting, patch hygiene, or packaging fails,
repair that root cause before spending either live-test budget.

---

### Iteration 35 - packaged daemon live qualification

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
E2E_RUN_ID=daemon-disk-polling-final-20260719T221413 \
E2E_REBUILD_BINARY=0 \
PYTHONPATH=e2e \
.venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  -q --durations=0 \
  --timeout=120 \
  --session-timeout=600 \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - Final formatting, all-target clippy, and `git diff --check` passed.
The repository-required rebuild produced a stripped static ARM64 daemon of
6,601,000 bytes with SHA-256
`ef613452dbb0e681ca02d95b5635a953beab94e9c6b741534be7574cc174be8c`.
The gateway restarted as PID 17430 and is listening on `127.0.0.1:7878` with
`config/prd.yml`, whose configured daemon path is that ARM64 artifact.

**Defect** - None known before live execution. The two exact cases now qualify
read-only high-concurrency polling and strict rotation against the rebuilt,
packaged daemon rather than a host development binary.

**Fix** - On any failure, preserve the bounded case evidence, diagnose the
product or harness root cause, append a new iteration, and do not weaken the
declared budgets or assertions.

---

### Iteration 36 - allocator THP policy before main

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo test -p sandbox-daemon --features jemalloc --test unit \
  observability_tests::selected_allocator_is_bounded_and_reports_native_process_totals
bin/start-sandbox-docker-gateway --rebuild-binary

cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
E2E_RUN_ID=daemon-disk-polling-final-retry1-20260719T221413 \
E2E_REBUILD_BINARY=0 \
PYTHONPATH=e2e \
.venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  -q --durations=0 \
  --timeout=120 \
  --session-timeout=600 \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - In the first live attempt, DP-02 passed in 7.88s: rotation completed
in 1.419s, every retained line was parseable, disk peaked at 1,048,512 logical
bytes, and all four active polls stayed daemon-disk-backed. DP-01 completed all
24 warmups and 192 measured polls in 3.707s; the store was byte-identical,
responses peaked at 64 records / 12,509 bytes, and anonymous memory moved only
237,568 bytes. Both exact run-owned sandbox IDs were destroyed and the baseline
gateway was restored.

**Defect** - DP-01 observed one 2-MiB anonymous huge page both in
`smaps_rollup` and cgroup `memory.stat`, violating the THP-free gate. The daemon
calls `PR_SET_THP_DISABLE` at the start of `main`, but the selected global
jemalloc initializes before `main`; with its default `thp` mode it can therefore
create a huge mapping before the process-wide policy is applied.

**Fix** - Set jemalloc's compile-time `thp:never` option so every allocator
mapping receives `MADV_NOHUGEPAGE` from allocator initialization onward, retain
the existing process-wide THP disable for non-allocator mappings, assert the
exact allocator configuration, rebuild the packaged daemon, and rerun the
unchanged two-case live suite.

---

### Iteration 37 - inherited THP policy before allocator startup

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo test -p sandbox-daemon --features jemalloc --test unit \
  observability_tests::selected_allocator_is_bounded_and_reports_native_process_totals
bin/start-sandbox-docker-gateway --rebuild-binary
```

**Good** - The first retry kept DP-02 green in 7.74s and completed DP-01's
24 warmups plus 192 measured polls in 3.888s. Poll responses remained bounded
at 64 records / 12,509 bytes, the resource store stayed byte-identical, and
peak, final, and later-median anonymous-memory deltas all fell to only 8,192
bytes. Run-owned cleanup passed with zero failures and the baseline gateway was
restored.

**Defect** - The unchanged THP gate still saw one 2-MiB mapping. Direct
inspection showed the daemon's post-start policy was disabled (`THP_enabled: 0`)
but the mapping had already been created without `MADV_NOHUGEPAGE`. The
allocator option controls its later mappings but cannot move the process-policy
boundary ahead of Rust runtime/global-allocator initialization.

**Fix** - On Linux `serve`, read the inherited THP policy; when it is still
enabled, disable it and re-exec the same daemon and original arguments once.
The kernel policy survives exec, so the replacement process starts all runtime
and allocator initialization THP-disabled while keeping the exact executable,
PID, CLI, and workload-child restoration contract. Rebuild and inspect a live
daemon mapping before rerunning the exact suite.

---

### Iteration 38 - Linux cross-target result coercion

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
bin/start-sandbox-docker-gateway --rebuild-binary
```

**Good** - Native formatting and the focused allocator contract passed 1/1.
The packaging workflow then compiled the new startup path for the actual ARM64
Linux target, exercising code excluded from the macOS host build.

**Defect** - The new THP policy getter returned rustix's `Errno` result
directly while declaring `std::io::Result`; the Linux cross-target correctly
rejected the mismatched error type before producing or installing an artifact.

**Fix** - Propagate the rustix result with `?` and wrap the boolean in `Ok`,
using the existing conversion into `std::io::Error`. Restart the repository
packaging command, then inspect the live daemon before qualification.

---

### Iteration 39 - re-exec THP live qualification

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
E2E_RUN_ID=daemon-disk-polling-final-retry2-20260719T221413 \
E2E_REBUILD_BINARY=0 \
PYTHONPATH=e2e \
.venv/bin/python -m pytest \
  e2e/observability/resource_isolation/test_daemon_disk_polling.py \
  -q --durations=0 --tb=short --log-cli-level=WARNING --show-capture=no \
  --timeout=120 \
  --session-timeout=600 \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Good** - The corrected ARM64 Linux package completed with SHA-256
`24346009febf515fb97ca023a4a7a6ec9f0f11fb22c82ec42ed460c08681f1f6`.
A disposable packaged sandbox then reported PID 7, PPID 1, executable
`/eos/bin/sandbox-daemon`, `THP_enabled: 0`, 560 KiB anonymous memory, zero
`AnonHugePages`, and zero cgroup `anon_thp`; exact-ID cleanup succeeded.

**Defect** - None known after the live-process policy and identity inspection.

**Fix** - Run the unchanged DP-01 and DP-02 assertions against this exact
artifact, preserve both bounded summaries, and verify run-owned cleanup plus
baseline restoration.

---

### Iteration 40 - final live result and source qualification

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
cargo fmt --all -- --check
cargo clippy --all-targets --features sandbox-daemon/jemalloc
cargo test \
  -p sandbox-config \
  -p sandbox-operation-catalog \
  -p sandbox-manager \
  -p sandbox-observability-telemetry \
  -p sandbox-observability-query \
  -p sandbox-daemon \
  --features sandbox-daemon/jemalloc
git diff --check
```

**Good** - DP-01 and DP-02 passed unchanged in 18.23s. DP-01 completed all 24
warmups and 192 measured polls in 3.662s; all 192 used `daemon_disk`, responses
peaked at 64 records / 12,509 bytes, the 11,328-byte store remained
byte-identical, and every anonymous-memory delta plus both THP measures was
zero. DP-02 rotated in 1.423s during four daemon-disk polls; storage peaked at
1,048,512 logical / 1,048,576 allocated bytes with every line complete,
bounded, and parseable. Both test sandboxes were destroyed with zero cleanup
failures, and the baseline gateway was restored.

**Defect** - The two disposable manual diagnostics initially remained because
their guarded cleanup used `--sandbox` instead of the catalogued
`--sandbox-id`. This did not affect either run-owned E2E cleanup result.

**Fix** - Destroy only the two known diagnostic IDs with the correct flag and
verify the sandbox inventory returns exactly to the seven pre-existing IDs.
Run the final formatting, lint, affected-package, and patch-hygiene gates on
the source that produced the green live behavior.

---

### Iteration 41 - final console regression gate

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-console
npm test
npm run build
```

**Good** - Final core formatting and `git diff --check` passed. All-target
clippy passed with only the two existing layerstack dead-code warnings, and the
complete affected Rust gate passed 377/377 tests, including 10,000-read routing
quiescence, 10,000-read daemon purity, bounded reader allocation, concurrent
rotation, sampler shutdown, and storage-failure isolation. The exact two live
test IDs passed, all four run-owned/diagnostic sandbox IDs were destroyed, and
inventory returned to the same seven pre-existing IDs.

**Defect** - None known after final core and live qualification.

**Fix** - Re-run the complete console unit gate and production build so the
single-in-flight polling implementation is qualified on the same final source
handoff.

---

### Iteration 42 - console workspace correction

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-console/web
npm test
npm run build
```

**Good** - The final console source remained unchanged after its earlier
101/101 unit pass and successful production build.

**Defect** - Iteration 41 invoked npm at the console repository root, which has
no `package.json`; npm exited immediately with `ENOENT` before running a test or
build command.

**Fix** - Run the same commands from the repository's `web` package directory
and preserve the exact test-file, test-case, duration, and build result.

---

### Iteration 43 - declared Node 24 console gate

**Command**
```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-console/web
PATH=/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH \
  npm test
PATH=/Users/yifanxu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH \
  npm run build
```

**Good** - Dependency inspection found a clean lockfile installation and the
repository explicitly declares `engines.node: 24.x`.

**Defect** - The default shell resolved Node 22.7.0; jsdom 29's dependency
graph fails to initialize there with `ERR_REQUIRE_ESM`, so Vitest started zero
tests and the guarded build did not run. This is a runtime mismatch, not an
application assertion or source failure.

**Fix** - Put the workspace's bundled Node 24.14.0 first on `PATH` for both npm
commands, matching the repository contract without modifying dependencies or
lockfiles.

---

### Iteration 44 - final qualification result

**Result** - With the declared Node 24.14.0 runtime, all 31 console test files
and all 101 test cases passed in 4.55s. The production build transformed 2,733
modules and completed in 477ms; asset verification passed before tests, before
build, and after build. The only build diagnostic was the existing advisory
for a JavaScript chunk larger than 500 KiB.

**Defect** - None known in the requested implementation after focused, broad,
packaged-live, cleanup, console, formatting, lint, build, and patch-hygiene
qualification. No tests were skipped, xfailed, or weakened.

**Fix** - None required.

---

### Iteration 45 - baseline gateway custody verification

**Result** - The final probe found the prior baseline restore had lost its PID
file after a transient address-in-use race. No gateway was listening, and all
four run-owned/diagnostic Docker container IDs were already absent. Restarting
the checked-in baseline configuration succeeded as PID 26915; the manager then
reported exactly the same seven pre-existing sandbox IDs and no test-owned ID.

**Defect** - None remains. The transient gateway custody race occurred after
the green suite and did not change its persisted case or cleanup evidence.

**Fix** - Baseline gateway service and inventory were restored without a
binary rebuild or any sandbox mutation.

---

### Iteration 46 - Stage 00 focused packaged gate intent

**Intent recorded before execution** - `2026-07-24T15:14:03+0800`.

**Git state** - Product
`upgrade-2.0-phase-1@7e8f4562f9079f27dcb5b514f6e4546b87e5aa04` is dirty
with patch SHA-256
`5c98c2d5b6ab2140df2a3f705c05286ca6bbfe1510efed92feade8381dcc3ec0`:

```text
 M config/bench.yml
 M config/linux-amd64.yml
 M config/macos-arm64.yml
 M config/prd.yml
 M config/windows-amd64.yml
 M crates/sandbox-cli/tests/fixtures/observability-help.txt
 M crates/sandbox-cli/tests/observability.rs
 M crates/sandbox-config/src/configs/runtime.rs
 M crates/sandbox-config/tests/unit/configs/runtime.rs
 M crates/sandbox-daemon/src/serve.rs
 M crates/sandbox-observability/query/src/response.rs
 M crates/sandbox-observability/query/tests/query.rs
 M crates/sandbox-operations/catalog/src/runtime.rs
 M crates/sandbox-operations/catalog/tests/runtime.rs
 M crates/sandbox-runtime/layerstack/src/observability.rs
 M crates/sandbox-runtime/layerstack/src/service/mod.rs
 M crates/sandbox-runtime/layerstack/src/service/model.rs
 M crates/sandbox-runtime/layerstack/src/stack/dir_list.rs
 M crates/sandbox-runtime/layerstack/src/stack/file_read.rs
 M crates/sandbox-runtime/layerstack/src/stack/mod.rs
 M crates/sandbox-runtime/layerstack/src/stack/ops/publish.rs
 M crates/sandbox-runtime/layerstack/src/stack/ops/read.rs
 M crates/sandbox-runtime/operation/src/lib.rs
 M crates/sandbox-runtime/operation/src/services.rs
?? crates/sandbox-runtime/layerstack/src/stack/observation.rs
?? crates/sandbox-runtime/layerstack/tests/baseline_v1_golden.rs
?? crates/sandbox-runtime/layerstack/tests/fixtures/v1/
?? crates/sandbox-runtime/layerstack/tests/resource_observation.rs
?? crates/sandbox-runtime/operation/tests/storage_route_observation.rs
```

Test
`upgrade-2.0-phase-1@d594f0c72083c39f95334fac685399bca20193f0` is dirty
with patch SHA-256
`a9be727618a305e2ee40cf3edf98385c17711382e1719ba9e2a1344bd70a301b`:

```text
 M benchmark/backend/benchmark_lab/observability.py
 M benchmark/backend/benchmark_lab/planning.py
 M benchmark/backend/benchmark_lab/product.py
 M benchmark/backend/benchmark_lab/runner.py
 M benchmark/backend/benchmark_lab/sessions.py
 M benchmark/backend/tests/contract/test_api.py
 M benchmark/backend/tests/contract/test_planning.py
 M benchmark/backend/tests/unit/test_runner_squash.py
 M benchmark/defaults/definition-catalog.json
?? benchmark/backend/benchmark_lab/phase1_baseline.py
?? benchmark/backend/tests/unit/test_phase1_baseline.py
?? benchmark/fixtures/
?? benchmark/presets/layerstack-phase1-tiny-baseline.yml
?? benchmark/tools/
?? e2e/fixtures/
?? e2e/runtime/layerstack_baseline/
?? e2e/schemas/
```

**Command**

```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
E2E_IMAGE=ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 \
E2E_REBUILD_BINARY=1 \
PYTHONPATH=e2e \
.venv/bin/python -m pytest \
  e2e/runtime/layerstack_baseline/test_baseline_route.py \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Cases and timeout** -
`layerstack.phase1.baseline.legacy-route` and
`layerstack.phase1.baseline.restart-cleanup`; each declaration has a
600,000 ms timeout. The first changed-product gate requests the repository's
normal rebuild path with `E2E_REBUILD_BINARY=1`.

**Image and source identity** - The immutable image index is
`sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90`.
Raw entry evidence at
`.e2e-state/evidence/stage00-entry-20260724T134437+0800/` proves the expected
ARM64 platform digest
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827d7c5425fb57681140e6efb`
does not exist and the index actually selects
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827dc4b8c707de5e78a6da7ab`.
This remains an artifact-validation blocker regardless of the functional
result. Pre-run product config `config/prd.yml` is SHA-256
`42e970f1c1e9151dd710d576e4d3f4f3d044b046bf3ee0d71b4e20a25a716157`;
pre-run CLI hashes are manager
`cae893d1cc9c411a6d0319f307ef5122ad5406d027b1127deb426d41a1cee228`,
runtime
`b3dcd0c44caf2f7e52dc5e94dfa70e98ccb0a6d1d776c0e23199f5f872f481a9`,
and observability
`d2b7948a79e3561367cbdec97745ad1f56fed235b598409ac8a98a8fb8872f55`.
The focused E2E sources are SHA-256
`47784d498da3ad07bb71fa7ac1f4711fcb52b3f8656f172e517633f3cb057f89`
for `conftest.py` and
`079a7b05f607ceac5fa9f206eccb549a4e27faaecd462aa1707176e1a4ad2878`
for `test_baseline_route.py`.

**Expected evidence** - Exact packaged public create/write/publish/read/execute
/destroy behavior, `legacy_v1` read and write authority, zero
fallback/mismatch/shadow counters, one-shot previsibility failure with
unchanged manifest/layer/metadata state, gateway recovery and one successful
retry, zero staging residue, no case workspace or execution residue, and
logical resource quiescence within five seconds. Each case must write a
bounded schema-v1 evidence artifact.

**Custody and cleanup scope** - The module fixture owns the temporary gateway
configuration, requests the product rebuild, and restores the checked-in
baseline gateway in LIFO teardown. Registered-sandbox and workspace-registry
fixtures own only IDs created by these two cases; each case destroys or
publishes only its own sessions. No global Docker prune, broad cleanup, or
mutation of pre-existing sandbox IDs is authorized.

---

## 2026-07-24 - Iteration 46 - Stage 00 focused packaged gate result

**Result** - Failed: 2 cases failed during their call phase, and the
validation-plugin teardown errors were consequent to those early call
failures because the terminal validation checkpoints were never reached.
The command completed in approximately 80 seconds.

**What worked** - The run exercised the requested rebuild path
(`E2E_REBUILD_BINARY=1`) and reached the packaged public create, write,
publish, read, execute, observation, failpoint, gateway-recovery, and retry
surfaces. The previsibility failpoint was consumed once, preserved the
retained workspace for retry, and the retry succeeded after gateway
recovery before the route assertion stopped the case.

**Defects exposed** -

- `layerstack.phase1.baseline.legacy-route` received a successful public
  execute response, but its `output` did not exactly match the test's
  newline-bearing expectation. The exact transport output contract must be
  traced before changing the assertion or product.
- `layerstack.phase1.baseline.restart-cleanup` received
  `write_authority: "[REDACTED]"` from the harness. The public CLI response
  passes through `e2e/harness/runner/cli.py`, whose broad `auth` key matcher
  treats the non-secret word `authority` as a credential. This prevents the
  test from observing the expected `legacy_v1` route value.

**Cleanup** - Passed for both cases. The run-owned sandboxes
`eos-3623dceb-6cf6-4bec-b68c-7399e4c11736` and
`eos-6b236f69-158a-4edb-b347-6cb26ff806fd` were each registered and
destroyed exactly once with zero cleanup failures. The pre-run baseline
gateway was restored. The post-run sandbox inventory contained the same
seven pre-existing sandbox IDs and neither run-owned ID.

**Artifacts** -

- `.e2e-state/observability/20260724T071601.101621Z-48363/layerstack.phase1.baseline.legacy-route/`
- `.e2e-state/observability/20260724T071604.567269Z-48363/layerstack.phase1.baseline.restart-cleanup/`
- `.e2e-state/metrics/operation-timing/latest.md`

Both observability directories record `evidence_state: early_failure` and
`cleanup_complete: true`. Entry-dependency evidence remains at
`.e2e-state/evidence/stage00-entry-20260724T134437+0800/`.

**Disposition** - Trace the exact public execute response and add focused
regression coverage for route-authority redaction, make only the
source-level fixes those reproductions justify, then rerun the two focused
cases. The immutable ARM64 image-manifest mismatch remains an independent
artifact-validation blocker.

---

## 2026-07-24 - Iteration 47 - Stage 00 focused packaged gate rerun intent

**Git state** - Product
`upgrade-2.0-phase-1@7e8f4562f9079f27dcb5b514f6e4546b87e5aa04` and test
`upgrade-2.0-phase-1@d594f0c72083c39f95334fac685399bca20193f0`
remain dirty. The SHA-256 of the sorted changed-file names and contents is
`fba5866ca90ca99b9de92d37a50ef8c70fb9c5499fc97ab33374dd3b91f92f4f`
for product and, excluding this append-only report,
`eb2c48f63cc865bf6cc889f3463099261e923951cb6b89df69aab04ced7235f5`
for test.

**Fix identity** - The CLI sanitizer is SHA-256
`d06dccc22795983a48bac000986f50b319a842691b599daae072e226e9d55820`;
its new offline regression is
`5abb16e47d71c5c91566404f765bc32eb88a46aef5109b4cce83c940474ea60f`.
The focused test is
`bee55438ea3952e3e60b002a4029641a94702023c925b41c267720a4eac92cca`
and the unchanged gateway fixture is
`47784d498da3ad07bb71fa7ac1f4711fcb52b3f8656f172e517633f3cb057f89`.
The redaction regression passed 1/1, the existing direct-daemon
credential-safety tests passed 8/8, focused collection found 2/2 cases, and
all three changed Python modules compile.

**Command**

```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test
E2E_IMAGE=ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 \
E2E_REBUILD_BINARY=1 \
PYTHONPATH=e2e \
.venv/bin/python -m pytest \
  e2e/runtime/layerstack_baseline/test_baseline_route.py \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

**Cases and timeout** -
`layerstack.phase1.baseline.legacy-route` and
`layerstack.phase1.baseline.restart-cleanup`; each declaration has a
600,000 ms timeout. The rerun deliberately requests
`E2E_REBUILD_BINARY=1` rather than inferring packaged-binary identity.

**Expected evidence** - Both cases pass the exact public
create/write/publish/read/execute/destroy flow. Read and write authority are
observable as `legacy_v1`; fallback, mismatch, and shadow counters remain
zero; the one-shot previsibility failure leaves manifest, layer, metadata,
and staging state unchanged before one successful retry; all case-owned
workspace and logical resource gauges quiesce within five seconds. Each
case writes bounded schema-v1 evidence.

**Image and custody** - The pinned image and its known ARM64 manifest
mismatch are unchanged from Iteration 46. The module fixture owns only its
temporary gateway configuration and restores the checked-in baseline
gateway. Per-case fixtures own only their registered sandbox and workspace
IDs. No pre-existing sandbox, broad Docker state, or unrelated repository
state is in cleanup scope.

---

## 2026-07-24 - Iteration 47 - Stage 00 focused packaged gate rerun result

**Result** - Passed: 2/2 cases in 53.20 seconds after the requested product
rebuild. `layerstack.phase1.baseline.legacy-route` passed in 43.623 seconds,
including rebuild setup, and
`layerstack.phase1.baseline.restart-cleanup` passed in 9.395 seconds,
including gateway restoration.

**Evidence** -

- `.e2e-state/observability/20260724T072355.899293Z-52477/layerstack.phase1.baseline.legacy-route/`
  records `evidence_state: passed`, exact public read and execute content,
  one manifest revision and one added layer, `legacy_v1` read/write
  authority, zero fallback/mismatch/shadow counters, zero active logical
  gauges, and zero-millisecond quiescence. Its evidence SHA-256 is
  `cd153eccc41ab5725e75dec0dda40f69a050d14e9d255bd179457540374d072d`.
- `.e2e-state/observability/20260724T072358.601433Z-52477/layerstack.phase1.baseline.restart-cleanup/`
  records `evidence_state: passed`, a one-shot `before_staging`
  `operation_failed`, unchanged manifest/layer/metadata counts before retry,
  zero staging residue, gateway recovery, one successful retry, exact public
  read content, `legacy_v1` authority, zero alternate-route counters, zero
  active logical gauges, and zero-millisecond quiescence. Its evidence
  SHA-256 is
  `ee3d11b9855c1007860c8089ddf5e23140cd20938e70648ba7db400e300da1c0`.

Both schema-v1 evidence artifacts are bounded at less than 4.3 KiB. The
fixed observation bookkeeping allocation is 176 bytes at quiescence and
equals its measured high-water value; no active owner, lease, task, worker,
queue, transaction, staging owner, cache, or registry entry remains.

**Cleanup** - Passed. Sandboxes
`eos-da18183a-501e-45cb-af3a-5ecb3cfefc69` and
`eos-e57b7593-d326-439b-832f-4bdd5015842d` were each registered and
destroyed exactly once with zero failures. The post-run inventory contains
the same seven pre-existing sandboxes and neither run-owned ID. The
checked-in baseline gateway was restored.

**Artifact blocker** - Functional packaged validation is green, but the
frozen ARM64 fixture still cannot be validated against the prompt's expected
platform manifest: the pinned index selects
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827dc4b8c707de5e78a6da7ab`,
not the expected
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827d7c5425fb57681140e6efb`.
Raw dependency evidence remains at
`.e2e-state/evidence/stage00-entry-20260724T134437+0800/`.

---

## 2026-07-24 - Iteration 48 - Stage 00 tiny benchmark rerun intent

**Git state** - Product and test repositories are both on
`upgrade-2.0-phase-1` with the Stage 00 working changes uncommitted. The
frozen entry image and its known ARM64 platform-manifest mismatch are
unchanged.

**Preflight** - Exact plan validation passes for
`layerstack-phase1-tiny-baseline`, resolving one runnable cell with one
warmup pair, five measured pairs, three sentinel warmups, and twenty
measured sentinel cycles. Focused strict-schema regressions pass 14/14.
They cover the current sandbox record, manager-owned cgroup response,
initial partial resource-ring availability, snapshot event-store counters,
and Stage 00 route/resource observations.

**Command**

```bash
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/benchmark
../.benchmark-state/test-venv/bin/sandbox-benchmark run \
  --plan layerstack-phase1-tiny-baseline \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox \
  --product-bin-dir /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox/bin
```

**Expected evidence** - Preserve all five measured raw/control pairs and all
twenty measured sentinel cycles, prove legacy-only route authority and
bounded logical-resource reclamation, and emit an explicit verdict without
zero-filling unavailable resource samples.

**Custody** - The benchmark runner owns only its generated run directory,
gateway process, workspace, and registered sandbox. The two product
executables staged in `bin/` did not exist before this gate and will be
removed only after their recorded SHA-256 identities are revalidated.
Pre-existing sandboxes, Docker objects, result corpora, and repository
changes are outside cleanup scope.

---

## 2026-07-24 - Iteration 48 - Stage 00 tiny benchmark rerun result

**Result** - Failed after 2.11 seconds. Run
`019f9316-3076-7cd3-af43-b01a948f02ff` reached the operation stage, where
the first internal baseline request returned a typed product error. The
summary records one attempted trial, one `product_failed` trial, zero
successful trials, and zero completed public request observations. This is
no longer an observability-schema failure: 175 strict resource observations
were accepted, including explicit unavailable values instead of fabricated
zeroes.

**Evidence** -
`.benchmark-state/results/019f9316-3076-7cd3-af43-b01a948f02ff/` contains
the immutable plan hash
`sha256:153e556767e3f027bfb74a8be1fd3845fef0f88dceaa12de8cb8d3342272e5fd`,
the failed summary and report, 192 lifecycle events, and 175 resource
observations. The runner retained only redacted gateway-log digests.

**Cleanup** - Passed. The runner destroyed its owned sandbox, terminated
its isolated gateway, removed its runtime directory, and retained only the
bounded result corpus and ownership marker. No pre-existing sandbox or
Docker resource entered cleanup scope.

**Disposition** - Capture the sanitized product-error kind and message at
the existing gateway client boundary in one otherwise exact rerun. Use that
evidence to identify the first failing control-arm operation before making a
source change.

---

## 2026-07-24 - Iteration 49 - Stage 00 tiny benchmark diagnostic rerun intent

**Scope** - Repeat the exact validated tiny plan with an in-memory diagnostic
wrapper around `GatewayClient.request`. The wrapper prints only
`GatewayProductError.kind` and the already-sanitized public detail; it does
not print credentials, request arguments, response bodies, or gateway
configuration.

**Expected evidence** - Identify the exact closed product operation and
sanitized error responsible for the first control-arm failure. Preserve the
normal runner's ownership ledger, isolated gateway lifecycle, resource
sampling, result persistence, and cleanup behavior.

**Custody** - Identical to Iteration 48. The diagnostic is process-local and
does not edit benchmark or product source. Cleanup remains limited to the
new run's registered sandbox, gateway, workspace, and runtime directory.

---

## 2026-07-24 - Iteration 49 - Stage 00 tiny benchmark diagnostic rerun result

**Result** - The diagnostic run
`019f9319-3078-7f47-84b8-0ca9e1a56056` isolated the first failure to
`file_read`: `invalid_request`, because the control arm selected a 1 MiB
single-line file while the public read response cap is 262,144 bytes. No
credential, request argument, raw response, or configuration value was
printed.

**Root cause** - `_verify_file` requested up to 2,000 lines and assumed that
all frozen corpus members fit in one public response. The localized and
incompressible fixtures are each one 1 MiB line, so no line window can make
their selected output fit the product cap.

**Cleanup** - Passed through the unchanged benchmark runner. The run-owned
sandbox and isolated gateway were removed; only the bounded result corpus
and ownership marker remain.

**Regression and fix** - A failing focused regression first reproduced that
the 1 MiB single-line verifier incorrectly called `file_read`. The verifier
now uses bounded `wc -c` plus `sha256sum` output for content above 256 KiB,
compares both exact byte length and digest, and retains public `file_read`
verification for smaller files. The full focused module passes 11/11. The
implementation SHA-256 is
`8d0c57ffc53f399605d2bd5bea74cf01e3cb959d2003d8adb7ca78b5f7d86fa5`;
the regression SHA-256 is
`3e022c3cd2998b91309e87f3806bf2e53d0b00c5225fd7d3fb6006c30fac8ced`.

---

## 2026-07-24 - Iteration 50 - Stage 00 tiny benchmark rerun intent

**Command** - Repeat the exact `layerstack-phase1-tiny-baseline` run command
from Iteration 48 after the bounded large-file verification regression
passes 11/11.

**Expected evidence** - Advance past the first raw-arm 1 MiB content
verification, preserve exact corpus byte/digest checks, then complete all
five measured pairs and twenty measured sentinel cycles or isolate the next
single public-contract failure.

**Custody** - Unchanged from Iteration 48. Cleanup may affect only the new
run's ownership-registered sandbox, workspace, gateway, and runtime
directory.

---

## 2026-07-24 - Iteration 50 - Stage 00 tiny benchmark rerun result

**Result** - Run `019f931b-1ed3-7770-ab05-d0be2c6495e5` advanced beyond
the 1 MiB public-read failure and ran for 3.29 seconds, then stopped on a
benchmark-side operation assertion. Its single trial is
`infrastructure_failed`, with zero product failures and cleanup restored.
The runner accepted 294 resource observations before teardown.

**Evidence** -
`.benchmark-state/results/019f931b-1ed3-7770-ab05-d0be2c6495e5/` contains
the result corpus. The trial records 2,541,056,334 ns in the operation stage
and 613,875 ns in teardown; all six baseline checks are conservatively
failed because the internal protocol did not complete.

**Cleanup** - Passed. The run-owned sandbox and gateway were removed under
the existing ownership ledger. No pre-existing resource entered cleanup
scope.

**Disposition** - Capture only the typed benchmark assertion message in one
process-local diagnostic repeat, then add a focused regression for the
specific assertion before changing source.

---

## 2026-07-24 - Iteration 51 - Stage 00 assertion diagnostic rerun intent

**Scope** - Repeat the validated tiny plan with an in-memory wrapper around
`run_phase1_baseline` that prints only the `Phase1BaselineError` message.
The wrapper does not print corpus content, product payloads, credentials,
gateway configuration, or raw responses.

**Expected evidence** - Identify the exact benchmark assertion reached after
the bounded 1 MiB digest verification and preserve normal result persistence
and owned cleanup.

**Custody** - Identical to Iteration 50; the diagnostic wrapper is
process-local and makes no source change.

---

## 2026-07-24 - Iteration 51 - Stage 00 assertion diagnostic rerun result

**Result** - Run `019f931c-17c3-7355-9f6d-182b50d85a3e` isolated the
assertion to `small-file corpus count or byte total is incorrect`. The
already-sanitized assertion message was the only added diagnostic output.

**Root cause** - The product's successful execute projection normalizes away
the terminal newline. The benchmark required exact output
`256 1048576\n`, and the same obsolete newline assumption also existed in
the later sentinel byte-count check.

**Regression and fix** - A failing focused regression first demonstrated
the missing output matcher. Command assertions now require exit code zero
and exact whitespace-delimited tokens, accepting both normalized and
newline-terminated output while rejecting wrong tokens or a nonzero exit.
The digest, small-file, and sentinel checks share the matcher. Focused
benchmark tests pass 27/27. The implementation SHA-256 is
`e831af5e175c2e1d30bcb2f01225a82b9d7cb8e88f4405aa20d454cfd784fe9c`;
the unit regression SHA-256 is
`0b6e93269be68f4c8dda75bb6b186ff299de7ec847f8536299b8bfb4ea042403`.

**Cleanup** - Passed through the unchanged benchmark runner. No
pre-existing resource entered cleanup scope.

---

## 2026-07-24 - Iteration 52 - Stage 00 tiny benchmark rerun intent

**Command** - Repeat the exact validated
`layerstack-phase1-tiny-baseline` command after all 27 focused benchmark
tests pass.

**Expected evidence** - Complete all six raw/control pairs and twenty-three
sentinel cycles, retain five measured pairs and twenty measured sentinel
cycles, prove the legacy-only route and logical release, and emit a bounded
memory verdict without zero-filled unavailable metrics.

**Custody** - Unchanged. Only the new run's ownership-registered resources
are eligible for cleanup.

---

## 2026-07-24 - Iteration 52 - Stage 00 tiny benchmark rerun result

**Result** - Run `019f931d-abb0-73be-b8cc-ee54e8d8871c` advanced beyond
the corrected small-file assertion, then stopped on the next benchmark-side
operation assertion after 3.38 seconds. The trial records
`infrastructure_failed`, zero product failures, 2,577,876,000 ns in the
operation stage, and cleanup restored.

**Cleanup** - Passed. The run-owned sandbox and gateway were removed through
the ownership ledger; no pre-existing resource entered scope.

**Disposition** - Capture only the next `Phase1BaselineError` message in a
process-local diagnostic repeat before changing source.

---

## 2026-07-24 - Iteration 53 - Stage 00 assertion diagnostic rerun intent

**Scope and custody** - Repeat the exact validated tiny plan with the same
message-only `Phase1BaselineError` wrapper used in Iteration 51. It makes no
source change and preserves normal ownership, persistence, and cleanup.

**Expected evidence** - Name the first assertion reached after successful
large-file digest and small-file total verification.

---

## 2026-07-24 - Iteration 53 - Stage 00 assertion diagnostic rerun result

**Result** - Run `019f931e-4702-745a-9e87-56fb4ae98cff` confirmed that
the remaining failure was still the small-file count/byte-total assertion,
not a later protocol step.

**Root cause** - The newline fix exposed a separate arithmetic error in the
verification command. `find ... -exec wc -c {} +` invokes `wc` with multiple
files, which emits both each file size and an aggregate `total` line. The
following `awk` summed both and therefore double-counted the corpus bytes.

**Regression and fix** - A focused shell-backed unit regression creates
three selected files plus one ignored file and proves the generated command
returns exactly three files and nine bytes. The command now emits one byte
count per selected file through a bounded `sh` batch, then derives count and
sum from those rows. Focused benchmark tests pass 28/28. The implementation
SHA-256 is
`59927617ef1da63b562b87228644b219be37499b6c14274cdfae2e32be62d5a6`;
the unit regression SHA-256 is
`bc3d34f0d6c32a4daf89c4d484de985e8ace5aae6ece89833760ae7a3cf6ba35`.

**Cleanup** - Passed through the unchanged runner. No pre-existing resource
entered cleanup scope.

---

## 2026-07-24 - Iteration 54 - Stage 00 tiny benchmark rerun intent

**Command** - Repeat the exact validated tiny baseline after all 28 focused
benchmark tests pass.

**Expected evidence** - Advance beyond the corrected 256-file verification
and complete the paired and sentinel protocols, or isolate one next
contract failure with cleanup restored.

**Custody** - Unchanged. Cleanup remains limited to the run's
ownership-registered sandbox, gateway, workspace, and runtime directory.

---

## 2026-07-24 - Iteration 54 - Stage 00 tiny benchmark rerun result

**Result** - Passed. Run `019f9320-08ea-7d6f-9f8e-f086d972a927`
completed in 78.22 seconds with correctness `pass`, one reportable
successful trial, zero product/correctness/infrastructure/cleanup failures,
and all six declared checks passing.

**Protocol evidence** - The bounded operation artifact retains one warmup
pair, five measured counterbalanced pairs, three sentinel warmups, and
twenty measured sentinel cycles. It records 1,109 timed public requests,
7,294 resource observations, exact corpus digests and shapes, and a
246,917-byte bounded evidence artifact with SHA-256
`97ac576a6e31658d986204ffd5453a879c5c8a4967831f22f78aa0ccba36a2d6`.
No warning was emitted.

**Route and reclamation** - Final authority is `legacy_v1` for reads and
writes under configured mode `legacy`. Fallback, mismatch, shadow, and
saturation counters are zero. Every measured sentinel cycle released its
workspace, namespace execution, lease, registry entry, transaction,
staging owner, task, worker, queue, and buffer. Final fixed bookkeeping is
176 live bytes, equal to its high-water value, with zero active logical
resources and zero-millisecond quiescence.

The twenty settled cgroup samples are complete. Their first-five median is
48,340,992 bytes, last-five median is 49,332,224 bytes, delta is 991,232
bytes, and robust slope is 74,988.31 bytes/cycle. The frozen non-blocking
verdict is `allocator-or-page-cache-retained`; logical release is complete
and neither route nor resource counters saturated.

**Cleanup** - Passed. The trial records
`cleanup_baseline_restored: true`; its owned sandbox, gateway, workspace,
and runtime directory were removed. Pre-existing resources were untouched.

**Evidence** -
`.benchmark-state/results/019f9320-08ea-7d6f-9f8e-f086d972a927/`.

---

## 2026-07-24 - Iteration 55 - Stage 00 post-run dependency comparison intent

**Scope** - Capture the same dependency, supported-invocation, tool,
machine, platform, runtime-inventory, and immutable image-fixture surfaces
recorded at entry, then compare the frozen entry and post-run records
without installing or updating dependencies.

**Expected evidence** - Prove an exact dependency-graph delta of zero across
all supported invocations. Treat record ordering, Docker's observation
clock, and filesystem usage counters as volatile only after proving their
stable inventory and identity fields are exact. Preserve the five planned
`rollout_mode: legacy` configuration additions as source-authority changes,
not dependency changes.

**Custody** - Read-only capture and comparison. No product, test, Docker, or
host resource is eligible for cleanup.

---

## 2026-07-24 - Iteration 55 - Stage 00 post-run dependency comparison result

**Result** - Dependency delta is zero and semantic environment-inventory
delta is zero. The complete dependency contract is exact across all 16
supported invocations: invocation metadata, 1,143 external package records,
2,179 enabled external package-feature pairs, 116 direct external manifest
edges, and both product and test contract-file inventories are unchanged.
The static tool surface, machine, platform, mounts, system observation,
product route listeners, and product process names are exact.

**Volatile runtime observations** - The same 43 Docker image records appear
in a different order. Docker information has 59 stable fields exact and
differs only in `SystemTime`. Filesystem identity, mount, total capacity,
and utilization percentage are exact; used/free block and inode counters
changed as expected while the focused tests and benchmark wrote and removed
run-owned evidence. These raw observations are therefore not byte-equal,
but their canonical inventory and stable identity are exact.

**Source-authority scope** - All 17 authority paths are still present.
Exactly five changed:
`config/bench.yml`, `config/linux-amd64.yml`,
`config/macos-arm64.yml`, `config/prd.yml`, and
`config/windows-amd64.yml`. Each change is the planned explicit
`rollout_mode: legacy` setting; every other authority file is exact.

**Immutable blocker** - The image-fixture record is exact between entry and
post-run and remains invalid. Index
`sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90`
expects Linux arm64 digest
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827d7c5425fb57681140e6efb`,
but the pinned local artifact resolves to
`sha256:7f622ca8766bccb22f04242ecb6f19f770b2f08827dc4b8c707de5e78a6da7ab`.
The Stage 00 verdict is therefore `blocked`, not passed.

**Evidence** -
`.e2e-state/evidence/stage00-post-20260724T155849+0800/`.
`stage00-post-baseline.json` has SHA-256
`f5cd3c8eb4f06b7360302c6d12b26632197a04de88c8ad3c3ac3181fb4f0f511`;
`stage00-dependency-comparison.json` has SHA-256
`5f00ed4ce892f2bb7069f0f9c084dbe5e224a393ea6992275dab08187896d503`.
All four post-capture artifacts pass the recorded `SHA256SUMS`.

**Cleanup** - Not applicable. The comparison is read-only and changed no
runtime or host state.

---
