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
