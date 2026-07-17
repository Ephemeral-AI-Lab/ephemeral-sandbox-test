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
