# Observability live E2E

This family verifies the standalone public observability CLI against a real
Docker sandbox. It covers both routing forms:

- `sandbox-observability-cli snapshot` returns the aggregate manager view and
  includes the ready test sandbox with a reachable daemon snapshot. The
  system-scoped route is owned by the manager application.
- `sandbox-observability-cli snapshot --sandbox-id <id>` returns that
  sandbox's scoped live snapshot. The sandbox-scoped route is owned by the
  observability application in the daemon.

The tests use the shared `core.cli` launcher, so they exercise the same
semantic route from `sandbox-operation-catalog`, CLI projection from
`sandbox-cli`, shared request path from `sandbox-operation-client`,
authenticated gateway RPC, and structured JSON response path as an operator
invocation. They do not call the daemon directly or inspect logs. The bounded
memory regression installs a large rotated-log fixture before polling the
public snapshot route, then measures container memory through `docker stats`
because cgroup memory metrics are unavailable inside Docker Desktop sandboxes.

The planned memory-neutrality and disk-budget conformance matrix is specified
in [`test_spec.md`](test_spec.md). Those cases are backend live E2E: they create
real Docker sandboxes and exercise public CLI routes. Browser tests are not a
substitute for this family.

The permanent correction for stale workspace holders, oversized daemon
runtimes, manager/daemon polling coupling, and missing resource guardrails is
defined in
[`sandbox_resource_efficiency_spec.md`](sandbox_resource_efficiency_spec.md).
Its focused live-Docker coverage plan is
[`sandbox_resource_efficiency_e2e_test_spec.md`](sandbox_resource_efficiency_e2e_test_spec.md).

Run this family after building or rebuilding the gateway binaries:

```sh
cd e2e
E2E_REBUILD_BINARY=0 python3 -m pytest -q observability/snapshot/test_snapshot.py
```
