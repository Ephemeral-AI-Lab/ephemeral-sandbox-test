"""Offline Control Room API contracts, including transport and evidence safety."""

from __future__ import annotations

import hashlib
import json
import base64
from http.client import HTTPConnection
import socket
import subprocess
from threading import Thread
from time import perf_counter

import pytest

from harness.api import app as app_module
from harness.api import server as api_module
from harness.api.loopback import make_loopback_server
from harness.api.redaction import redact_chunks
from harness.api.server import ApiRequest, ControlRoomApi
from harness.catalog.declarations import e2e_test
from harness.runner.controller import PreviewController
from harness.runner.runner import SerialPytestRunner
from harness.storage.roots import derive_roots
from harness.storage.store import append_event, create_attempt, create_run, load_projection, source_tree_digest


def _roots(tmp_path):
    test_root = tmp_path / "tests"
    product_root = tmp_path / "product"
    (test_root / "e2e").mkdir(parents=True)
    product_root.mkdir()
    return derive_roots(test_root, product_root)


def _case(test_id="harness.api.catalog", case_id="default"):
    return {
        "test_id": test_id,
        "case_id": case_id,
        "title": "Catalog contract case",
        "purpose": "A fixture-driven API contract.",
        "source": "e2e/case.py",
        "pytest_nodeid": "case.py::test_contract",
        "domain_id": "harness",
        "family_id": "api",
        "kind": "harness",
        "runnable": True,
        "timeout_ms": 100,
        "validations": [{"id": "response", "required": True}],
        "execution_surface": None,
        "effective_features": [],
        "direct_feature_ids": [],
        "owner_id": "e2e-core",
        "execution_label_ids": [],
    }


def _catalog(cases):
    return {
        "schema_version": 1,
        "kind": "e2e_catalog",
        "catalog_revision": "sha256:catalog",
        "source_revision": "sha256:source",
        "cases": cases,
    }


def _manifest(run_id, cases):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "preview_id": "preview-api",
        "created_at": "2026-07-13T00:00:00Z",
        "catalog_revision": "sha256:catalog",
        "source_revision": "sha256:source",
        "cases": cases,
        "policies": {"fail_fast": False},
        "preflight_snapshot": {},
        "controller_bundle_digest": "sha256:controller",
        "runner_bundle_digest": "sha256:runner",
        "product_builds": {},
        "source_files": [],
        "source_snapshot_digest": source_tree_digest([]),
        "workspace_template": "template-default",
        "attempt_ids": ["attempt-api"],
        "limits": {},
        "idempotency_digest": "sha256:idempotency",
    }


def _api(tmp_path, *, secret="", expected_host="127.0.0.1:9411"):
    roots = _roots(tmp_path)
    (roots.e2e_source_root / "case.py").write_text("def test_contract(): pass\n", encoding="utf-8")
    catalog = _catalog([_case()])
    controller = PreviewController(
        roots,
        controller_bundle_digest="sha256:controller",
        runner_bundle_digest="sha256:runner",
        catalog_loader=lambda: catalog,
        health_loader=lambda: {"schema_version": 1, "state": "ready", "current_revision": "sha256:catalog"},
        source_revision_loader=lambda: "sha256:source",
        disk_free_bytes=lambda: 2 << 30,
    )
    api = ControlRoomApi(
        roots,
        controller,
        expected_host=expected_host,
        runner=SerialPytestRunner(roots, producer_revision="sha256:runner"),
        catalog_loader=lambda: catalog,
        health_loader=lambda: {"schema_version": 1, "state": "ready", "current_revision": "sha256:catalog"},
        catalog_refresh=lambda: {"state": "requested", "coalesced": True},
        template_prepare=lambda: {"state": "prepared"},
        known_secrets=(secret,) if secret else (),
    )
    return roots, api


def _request(api, method, target, *, body=None, mutation=False, headers=None):
    values = {"Host": "127.0.0.1:9411", **(headers or {})}
    if mutation:
        values.update({"Origin": "http://127.0.0.1:9411", "X-E2E-Nonce": api.nonce})
    encoded = json.dumps(body).encode() if body is not None else b""
    return api.handle(ApiRequest(method, target, values, encoded))


def _p95_ms(action) -> float:
    for _ in range(5):
        action()
    samples = []
    for _ in range(30):
        started = perf_counter()
        action()
        samples.append((perf_counter() - started) * 1_000)
    return sorted(samples)[28]


@e2e_test(
    id="harness.api.routes",
    title="Control Room exposes its fourteen narrow routes",
    description="Every API route is fixture-tested through envelope, bodyless action, and controller-owner contracts.",
    validations={"routes": "The fourteen documented paths map to catalog, controller, run, evidence, and workspace owners."},
)
def test_fourteen_routes_use_existing_controller_owners(tmp_path, validation):
    roots, api = _api(tmp_path)
    health = _request(api, "GET", "/api/v1/health")
    catalog = _request(api, "GET", "/api/v1/catalog?family_id=api&limit=50")
    refresh = _request(api, "POST", "/api/v1/catalog/refresh", mutation=True)
    events = _request(api, "GET", "/api/v1/events")
    preview = _request(
        api,
        "POST",
        "/api/v1/previews",
        mutation=True,
        body={
            "selection": {
                "schema_version": 1,
                "catalog_revision": "sha256:catalog",
                "include": [{"case": {"test_id": "harness.api.catalog", "case_id": "default"}}],
                "exclude": [],
            }
        },
    )
    preview_data = preview.json()["data"]
    admitted = _request(
        api,
        "POST",
        "/api/v1/runs",
        mutation=True,
        body={"preview_id": preview_data["preview_id"], "admission_token": preview_data["admission_token"], "idempotency_key": "api-route"},
    )
    run_id = admitted.json()["data"]["run_id"]
    run = _request(api, "GET", f"/api/v1/runs/{run_id}")
    runs = _request(api, "GET", "/api/v1/runs?limit=50")
    cancel = _request(api, "POST", f"/api/v1/runs/{run_id}/cancel", mutation=True)
    evidence = _request(api, "GET", f"/api/v1/runs/{run_id}/evidence/missing")
    workspaces = _request(api, "GET", "/api/v1/workspaces")
    prepare = _request(api, "POST", "/api/v1/workspaces/template/prepare", mutation=True)
    api.runner.execute(run_id, lambda _case: {"state": "cancelled", "validations": {"response": "cancelled"}})
    create_attempt(roots, "attempt-api", run_id=run_id)
    workspace_purge = _request(api, "POST", "/api/v1/workspaces/attempt-api/purge", mutation=True)
    purge = _request(api, "POST", f"/api/v1/runs/{run_id}/purge", mutation=True)

    with validation("routes", expected=14, actual=lambda: 14):
        assert health.status == catalog.status == refresh.status == preview.status == admitted.status == run.status == runs.status == cancel.status == workspaces.status == prepare.status == workspace_purge.status == purge.status == 200
        assert events.status == 200 and events.headers["Content-Type"].startswith("text/event-stream")
        assert evidence.status == 404
        assert catalog.json()["data"]["facets"]["family_id"] == {"api": 1}
        assert refresh.json()["data"] == {"state": "requested", "coalesced": True}
        assert run.json()["data"]["run_id"] == run_id and runs.json()["data"]["items"][0]["run_id"] == run_id
        assert cancel.json()["data"]["run_id"] == run_id
        assert prepare.json()["data"] == {"state": "prepared"}
        assert workspace_purge.json()["data"]["workspace_id"] == "attempt-api"
        assert purge.json()["data"]["state"] == "purged"


@e2e_test(
    id="harness.api.transport-evidence",
    title="Loopback transport and evidence responses are redacted and ownership-safe",
    description="Exact Host, Origin, memory nonce, no CORS, artifact map, traversal, corruption, and purge behavior remain typed and non-leaking.",
    validations={"transport": "Mutations require exact loopback transport and bodyless actions reject browser input.", "evidence": "Plain and encoded canaries do not survive durable, SSE, or API evidence paths."},
)
def test_transport_and_evidence_contracts_are_safe(tmp_path, validation):
    secret = "api-secret-canary"
    encoded_secret = base64.b64encode(secret.encode()).decode()
    roots, api = _api(tmp_path, secret=secret)
    case = _case("harness.api.evidence")
    create_run(roots, _manifest("run-evidence", [case]))
    evidence_root = roots.e2e_state_root / "runs" / "run-evidence" / "evidence"
    artifact = evidence_root / "opaque-artifact"
    artifact_content = f"safe artifact {secret} {encoded_secret}".encode()
    artifact.write_bytes(artifact_content)
    append_event(
        roots,
        "run-evidence",
        {
            "at": "2026-07-13T00:00:00Z",
            "monotonic_ns": 1,
            "producer": "runner",
            "producer_revision": "sha256:runner",
            "type": "evidence.recorded",
            "test_id": case["test_id"],
            "case_id": case["case_id"],
            "payload": {"evidence_id": "opaque-evidence", "availability": "available", "storage_ref": "opaque-artifact", "sha256": "sha256:" + hashlib.sha256(artifact_content).hexdigest(), "message": f"{secret} {encoded_secret}"},
        },
    )
    missing_host = api.handle(ApiRequest("GET", "/api/v1/health", {}))
    wrong_origin = _request(api, "POST", "/api/v1/catalog/refresh", headers={"Origin": "http://evil.invalid", "X-E2E-Nonce": api.nonce})
    wrong_nonce = _request(api, "POST", "/api/v1/catalog/refresh", headers={"Origin": "http://127.0.0.1:9411", "X-E2E-Nonce": "wrong"})
    bodyless = _request(api, "POST", "/api/v1/catalog/refresh", mutation=True, body={"path": "/unsafe"})
    artifact_response = _request(api, "GET", "/api/v1/runs/run-evidence/evidence/opaque-evidence")
    traversal = _request(api, "GET", "/api/v1/runs/run-evidence/evidence/../manifest.json")
    stream = _request(api, "GET", "/api/v1/events?run_id=run-evidence&after=0")
    durable = (roots.e2e_state_root / "runs" / "run-evidence" / "events.jsonl").read_text(encoding="utf-8")
    artifact.write_bytes(b"corrupt")
    corrupt = _request(api, "GET", "/api/v1/runs/run-evidence/evidence/opaque-evidence")
    append_event(roots, "run-evidence", {"at": "2026-07-13T00:00:01Z", "monotonic_ns": 2, "producer": "controller", "producer_revision": "sha256:controller", "type": "run.state", "payload": {"from": "queued", "to": "running"}})
    append_event(roots, "run-evidence", {"at": "2026-07-13T00:00:02Z", "monotonic_ns": 3, "producer": "controller", "producer_revision": "sha256:controller", "type": "run.state", "payload": {"from": "running", "to": "passed"}})
    purged = _request(api, "POST", "/api/v1/runs/run-evidence/purge", mutation=True)
    assert purged.status == 200, purged.json()
    after_purge = _request(api, "GET", "/api/v1/runs/run-evidence/evidence/opaque-evidence")

    with validation("transport", expected=(421, 403, 403, 400), actual=lambda: (missing_host.status, wrong_origin.status, wrong_nonce.status, bodyless.status)):
        assert (missing_host.status, wrong_origin.status, wrong_nonce.status, bodyless.status) == (421, 403, 403, 400)
        assert all("Access-Control-Allow-Origin" not in response.headers for response in (missing_host, wrong_origin, wrong_nonce, bodyless, artifact_response))
    with validation("evidence", expected=(200, 404, 500, 410), actual=lambda: (artifact_response.status, traversal.status, corrupt.status, after_purge.status)):
        assert artifact_response.body == b"safe artifact [REDACTED] [REDACTED]" and artifact_response.headers["X-Content-Type-Options"] == "nosniff"
        assert (traversal.status, corrupt.status, after_purge.status) == (404, 500, 410)
        assert secret not in durable and encoded_secret not in durable
        assert secret not in stream.body.decode() and encoded_secret not in stream.body.decode()
        assert b"event: stream.heartbeat" in stream.body
        assert secret not in artifact_response.body.decode() and encoded_secret not in artifact_response.body.decode()
        assert secret.encode() not in redact_chunks([secret[:7].encode(), secret[7:].encode()])
        assert "purged" in purged.body.decode() and load_projection(roots, "run-evidence")["retention"]["state"] == "purged"


@e2e_test(
    id="harness.api.loopback",
    title="Control Room binds only loopback and preserves browser transport checks",
    description="The HTTP adapter forwards the exact Host, Origin, nonce, SSE, and bodyless contracts without CORS headers.",
    validations={"loopback": "Only a numeric loopback listener serves an exact-host request."},
)
def test_loopback_adapter_enforces_exact_browser_transport(tmp_path, validation):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    roots, api = _api(tmp_path, expected_host=f"127.0.0.1:{port}")
    server = make_loopback_server(api, "127.0.0.1", port)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/v1/health", headers={"Host": api.expected_host})
        health = connection.getresponse()
        health_body = health.read()
        connection.close()

        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("POST", "/api/v1/catalog/refresh", headers={"Host": api.expected_host, "Origin": api.expected_origin, "X-E2E-Nonce": api.nonce})
        refresh = connection.getresponse()
        refresh_body = refresh.read()
        connection.close()

        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/v1/events", headers={"Host": api.expected_host})
        stream = connection.getresponse()
        stream_body = stream.read()
        connection.close()

        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/v1/health", headers={"Host": "127.0.0.1:1"})
        rejected = connection.getresponse()
        rejected.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    with validation("loopback", expected=(200, 200, 421), actual=lambda: (health.status, refresh.status, rejected.status)):
        assert (health.status, refresh.status, rejected.status) == (200, 200, 421)
        assert json.loads(health_body)["data"]["nonce"] == api.nonce
        assert json.loads(refresh_body)["data"] == {"state": "requested", "coalesced": True}
        assert stream.headers["Content-Type"].startswith("text/event-stream") and b": heartbeat" in stream_body
        assert all("Access-Control-Allow-Origin" not in response.headers for response in (health, refresh, stream, rejected))
    try:
        make_loopback_server(api, "0.0.0.0", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("a public listener must not be constructible")


@e2e_test(
    id="harness.api.static-control-room",
    title="Loopback server hosts the built Control Room and refreshes its catalog",
    description="The executable composition keeps UI, API, and the validated catalog refresh workflow on one loopback origin.",
    validations={"composition": "SPA routes and assets are served safely, while refresh invokes offline catalog collection."},
)
def test_loopback_serves_the_built_control_room_on_the_same_origin(tmp_path, validation, monkeypatch):
    roots, api = _api(tmp_path)
    web_root = tmp_path / "web-dist"
    assets = web_root / "assets"
    assets.mkdir(parents=True)
    (web_root / "index.html").write_text("<!doctype html><title>E2E Control Room</title>", encoding="utf-8")
    (assets / "app.js").write_text("globalThis.controlRoomLoaded = true;", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("must not be served", encoding="utf-8")

    server = make_loopback_server(api, "127.0.0.1", 0, web_root=web_root)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        responses = []
        for path in ("/e2e/catalog", "/assets/app.js", "/api/v1/health", "/%2e%2e/outside.txt"):
            connection = HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", path, headers={"Host": api.expected_host})
            response = connection.getresponse()
            responses.append((response.status, dict(response.headers), response.read()))
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    spa, asset, health, traversal = responses
    refresh_command = []

    def collect(command, **kwargs):
        refresh_command.extend(command)
        assert kwargs["cwd"] == roots.test_repository_root
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(app_module.subprocess, "run", collect)
    refreshed = app_module._refresh_catalog(roots)

    with validation("composition", expected="safe SPA and published refresh", actual=lambda: "safe SPA and published refresh"):
        assert spa[0] == 200 and spa[1]["Content-Type"].startswith("text/html")
        assert b"E2E Control Room" in spa[2]
        assert asset[0] == 200 and asset[1]["Content-Type"].startswith("text/javascript")
        assert json.loads(health[2])["data"]["nonce"] == api.nonce
        assert traversal[0] == 404 and b"must not be served" not in traversal[2]
        assert refreshed == {"state": "published", "coalesced": False}
        assert refresh_command[1].endswith("/harness/catalog/collect.py")

    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2, "", "failed"),
    )
    with pytest.raises(app_module.ApiError) as failed:
        app_module._refresh_catalog(roots)
    assert failed.value.code == "catalog_refresh_failed"


@e2e_test(
    id="harness.api.catalog-revision",
    title="Ordinary catalog refetch exposes a newly discovered case",
    description="The API derives every card and family from the current catalog without a route or UI registration.",
    validations={"revision": "A subsequent normal catalog read returns the changed revision, new folder source, and generated facet."},
)
def test_catalog_refetch_exposes_new_folder_without_api_registration(tmp_path, validation):
    roots, api = _api(tmp_path)
    current = _catalog([_case()])
    discovered = _case("runtime.additive", "folder")
    discovered.update({"title": "New folder catalog record", "domain_id": "runtime", "family_id": "additive", "kind": "product", "source": "e2e/runtime/additive/test_case.py", "execution_surface": "runtime_cli", "effective_features": ["runtime.additive"]})
    revision = _catalog([_case(), discovered])
    revision["catalog_revision"] = "sha256:catalog-v2"
    holder = {"value": current}
    api._catalog_loader = lambda: holder["value"]

    before = _request(api, "GET", "/api/v1/catalog")
    holder["value"] = revision
    after = _request(api, "GET", "/api/v1/catalog")

    with validation("revision", expected=("sha256:catalog-v2", "additive", 2), actual=lambda: (after.json()["data"]["catalog_revision"], after.json()["data"]["items"][1]["family_id"], after.json()["data"]["total"])):
        assert before.json()["data"]["total"] == 1
        assert after.json()["data"]["catalog_revision"] == "sha256:catalog-v2"
        assert after.json()["data"]["items"][1]["source"] == "e2e/runtime/additive/test_case.py"
        assert after.json()["data"]["facets"]["family_id"]["additive"] == 1


@e2e_test(
    id="harness.api.evidence-caps",
    title="Evidence cap reports retained and omitted counts",
    description="A browser evidence response is bounded while retaining exact byte and line omissions for the UI.",
    validations={"caps": "The response reports retained bytes, omitted bytes, and omitted lines instead of inventing zero."},
)
def test_evidence_cap_exposes_omitted_counts(tmp_path, validation, monkeypatch):
    roots, api = _api(tmp_path)
    case = _case("harness.api.caps")
    create_run(roots, _manifest("run-caps", [case]))
    raw = (b"retained-line\n" * 4) + (b"omitted-line\n" * 4)
    monkeypatch.setattr(api_module, "MAX_EVIDENCE_BYTES", 32)
    artifact = roots.e2e_state_root / "runs" / "run-caps" / "evidence" / "opaque-cap"
    artifact.write_bytes(raw)
    append_event(
        roots,
        "run-caps",
        {
            "at": "2026-07-13T00:00:00Z",
            "monotonic_ns": 1,
            "producer": "runner",
            "producer_revision": "sha256:runner",
            "type": "evidence.recorded",
            "test_id": case["test_id"],
            "case_id": case["case_id"],
            "payload": {"evidence_id": "opaque-cap", "availability": "available", "storage_ref": "opaque-cap", "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()},
        },
    )
    response = _request(api, "GET", "/api/v1/runs/run-caps/evidence/opaque-cap")

    with validation("caps", expected=(32, len(raw) - 32), actual=lambda: (len(response.body), int(response.headers["X-E2E-Evidence-Omitted-Bytes"]))):
        assert response.status == 200
        assert len(response.body) == 32
        assert response.headers["X-E2E-Evidence-Retained-Bytes"] == "32"
        assert int(response.headers["X-E2E-Evidence-Omitted-Bytes"]) == len(raw) - 32
        assert int(response.headers["X-E2E-Evidence-Omitted-Lines"]) > 0


@e2e_test(
    id="harness.api.budget",
    title="Catalog and history meet the supported offline fixture budget",
    description="The controller serves 10,000 catalog cases in pages of 50 and scans 1,000 retained runs without an index.",
    validations={"catalog": "Thirty warm samples keep a 10,000-case catalog page below 100ms p95.", "history": "Thirty warm samples keep a 1,000-run history scan below 500ms p95."},
)
def test_offline_catalog_and_history_budget(tmp_path, validation, monkeypatch):
    roots, api = _api(tmp_path)
    cases = []
    for index in range(10_000):
        case = _case(f"runtime.budget-{index}")
        case.update({"domain_id": "runtime", "family_id": f"family-{index % 7}", "kind": "product", "execution_surface": "runtime_cli", "effective_features": [f"runtime.budget-{index}"]})
        cases.append(case)
    fixture = _catalog(cases)
    fixture["catalog_revision"] = "sha256:catalog-10000"
    api._catalog_loader = lambda: fixture

    catalog_p95 = _p95_ms(lambda: _request(api, "GET", "/api/v1/catalog?limit=50"))
    assert _request(api, "GET", "/api/v1/catalog?limit=50").json()["data"]["total"] == 10_000

    runs_root = roots.e2e_state_root / "runs"
    for index in range(1_000):
        (runs_root / f"run-{index:04d}").mkdir()
    monkeypatch.setattr(api_module, "load_projection", lambda _roots, run_id: {"run_id": run_id, "state": "passed", "created_at": "2026-07-13T00:00:00Z", "case_counts": {"passed": 1}, "evidence_health": "complete", "retention": {"state": "retained"}})
    history_p95 = _p95_ms(lambda: _request(api, "GET", "/api/v1/runs?limit=50"))
    assert len(_request(api, "GET", "/api/v1/runs?limit=50").json()["data"]["items"]) == 50
    print(f"PERF catalog_10000_page50_p95_ms={catalog_p95:.2f} history_1000_page50_p95_ms={history_p95:.2f}")

    with validation("catalog", expected="p95 < 100ms", actual=lambda: f"{catalog_p95:.2f}ms"):
        assert catalog_p95 < 100
    with validation("history", expected="p95 <= 500ms", actual=lambda: f"{history_p95:.2f}ms"):
        assert history_p95 <= 500
