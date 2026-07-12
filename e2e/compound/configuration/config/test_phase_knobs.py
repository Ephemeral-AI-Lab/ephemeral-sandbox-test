"""B — consolidation-phase knobs, keyed to the config consolidation spec.

All three phase classes are live (phases 1-3 landed); each test names its
contract in its docstring. Phase 4 (gateway/console sections) is
intentionally absent: gateway bind/PID knobs are exercised implicitly by
this family's own gateway bring-up, max_concurrent_connections has no
deterministic CLI observable, and the console is outside this suite's
sandbox-cli charter.
"""

import hashlib
import json

import pytest

from config import helpers
from harness.runner import cli as climod
from harness.runner.daemon_http import daemon_http_endpoint, http_post
from harness.catalog.declarations import e2e_test

pytestmark = pytest.mark.config


def _tree_digest(root):
    """{relpath: (kind, mode[, sha256])} for every entry under root —
    everything the export transport must carry except mtimes, which publish
    stamps with wall time."""
    digest = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        mode = path.stat().st_mode & 0o7777
        if path.is_dir():
            digest[rel] = ("dir", mode)
        else:
            digest[rel] = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
    return digest


class TestPhase1:
    """runtime.layerstack, manager.export, daemon.http.export (phase 1).

    Lane A methods run first (definition order), and every Lane B arm
    restores the module's family gateway on exit: later Lane A tests (also
    TestPhase2/3 below) rewrite the module's daemon YAML, which only that
    gateway reads.
    """

    @e2e_test(
        id='phase0.fd146f600feadbd4df95c2c9',
        title='Sweep Width Squash Invariance',
        description='Validates the behavior exercised by Sweep Width Squash Invariance.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-sweep-width-squash-invariance': 'The assertions for sweep width squash invariance hold.'},
        execution_surface='cli',
    )
    def test_sweep_width_squash_invariance(self, lane_a_daemon_yaml):
        """P1-F1 — remount_sweep_width 1 vs 4: squash succeeds identically in
        both arms (perf knob; correctness invariance is the e2e contract) and
        the retired env smuggle is gone from the flow — the width rides only
        the daemon YAML's runtime.layerstack section."""
        for width in (1, 4):
            generated = helpers.rewrite_daemon_yaml(
                lane_a_daemon_yaml,
                {"runtime": {"layerstack": {"remount_sweep_width": width}}},
            )
            rendered = generated.read_text(encoding="utf-8")
            assert "EOS_" not in rendered, "env side channels must be gone from the flow"
            assert "remount_sweep_width" in rendered
            with helpers.sandbox() as sandbox_id:
                helpers.exec_output(sandbox_id, "printf one > sweep-a.txt")
                helpers.exec_output(sandbox_id, "printf two > sweep-b.txt")
                result = climod.manager(
                    "squash_layerstacks", "--sandbox-id", sandbox_id, timeout=240
                )
                assert isinstance(result, dict) and not climod.is_error(result), (
                    f"squash failed at width {width}: {result}"
                )
                assert helpers.exec_output(sandbox_id, "cat sweep-a.txt").strip() == "one"
                assert helpers.exec_output(sandbox_id, "cat sweep-b.txt").strip() == "two"

    @e2e_test(
        id='phase0.912e631dcd54877c6cb67f31',
        title='Export Chunk Shape Invariance',
        description='Validates the behavior exercised by Export Chunk Shape Invariance.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-export-chunk-shape-invariance': 'The assertions for export chunk shape invariance hold.'},
        execution_surface='cli',
    )
    def test_export_chunk_shape_invariance(self, lane_a_daemon_yaml, tmp_path):
        """P1-F4 (adapted) — runtime.layerstack.export_chunk_bytes: 4096 pages
        a multi-chunk spool whose exported content is identical to the 2 MiB
        default arm.

        Spec drift, twofold: (1) the spec named daemon.http.export frame
        shape, but the export stream surface was removed in favor of
        read_export_chunk RPC paging while phase 1 landed, so the
        transport-shape knob is the chunk cap; (2) raw archive checksums are
        not comparable across sandboxes — publish stamps wall-clock mtimes
        into the layer store — so the invariance contract is the exported
        tree (entry set, modes, content bytes), which any paging fault
        (lost, duplicated, reordered chunk) would corrupt.
        """
        seed_command = (
            "mkdir -p chunks"
            " && i=0; while [ $i -lt 400 ]; do echo $i | sha256sum; i=$((i+1)); done"
            " > chunks/blob.txt"
            " && chmod 755 chunks && chmod 644 chunks/blob.txt"
        )
        trees = {}
        arms = (
            ("narrow", {"runtime": {"layerstack": {"export_chunk_bytes": 4096}}}),
            ("default", {}),
        )
        for arm, overrides in arms:
            helpers.rewrite_daemon_yaml(lane_a_daemon_yaml, overrides)
            with helpers.sandbox() as sandbox_id:
                helpers.exec_output(sandbox_id, seed_command)
                archive = tmp_path / f"chunks-{arm}.tar.zst"
                result = climod.manager(
                    "export_changes",
                    "--sandbox-id",
                    sandbox_id,
                    "--dest",
                    str(archive),
                    "--format",
                    "tar-zst",
                )
                assert not climod.is_error(result), f"{arm} archive export failed: {result}"
                assert archive.stat().st_size > 4096, (
                    "spool must span several narrow chunks to exercise paging"
                )
                dest = tmp_path / f"tree-{arm}"
                result = climod.manager(
                    "export_changes",
                    "--sandbox-id",
                    sandbox_id,
                    "--dest",
                    str(dest),
                    "--format",
                    "dir",
                )
                assert not climod.is_error(result), f"{arm} dir export failed: {result}"
                trees[arm] = _tree_digest(dest)
        assert trees["narrow"].get("chunks/blob.txt"), "delta must carry the payload"
        assert trees["narrow"] == trees["default"], (
            f"chunk shape must not change exported content: {trees}"
        )

    @e2e_test(
        id='phase0.11db9d7567dc480c73ad91d5',
        title='Export Stream Cap Error',
        description='Validates the behavior exercised by Export Stream Cap Error.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-export-stream-cap-error': 'The assertions for export stream cap error hold.'},
        execution_surface='cli',
    )
    @pytest.mark.slow
    def test_export_stream_cap_error(self, lane_a_daemon_yaml, tmp_path):
        """P1-F2 — manager.export.max_stream_bytes: 4096 fails an export of a
        larger delta with the cap error; a generous-cap (baseline) arm accepts
        the same payload. Restores the module's family gateway on exit."""
        payload_command = "head -c 65536 /dev/urandom > payload.bin"
        try:
            capped_yaml = helpers.make_config(
                {"manager": {"export": {"max_stream_bytes": 4096}}},
                tmp_path / "gateway-stream-cap.yml",
            )
            helpers.start_gateway(capped_yaml)
            with helpers.sandbox() as sandbox_id:
                helpers.exec_output(sandbox_id, payload_command)
                dest = tmp_path / "capped.tar.zst"
                result = climod.manager(
                    "export_changes",
                    "--sandbox-id",
                    sandbox_id,
                    "--dest",
                    str(dest),
                    "--format",
                    "tar-zst",
                )
                error = helpers.error_text(result)
                assert "export stream cap exceeded" in error, error
                assert not dest.exists(), (
                    "a capped export must not materialize the archive"
                )

            generous_yaml = helpers.make_config({}, tmp_path / "gateway-generous.yml")
            helpers.start_gateway(generous_yaml)
            with helpers.sandbox() as sandbox_id:
                helpers.exec_output(sandbox_id, payload_command)
                dest = tmp_path / "generous.tar.zst"
                result = climod.manager(
                    "export_changes",
                    "--sandbox-id",
                    sandbox_id,
                    "--dest",
                    str(dest),
                    "--format",
                    "tar-zst",
                )
                assert not climod.is_error(result), (
                    f"generous arm export failed: {result}"
                )
                assert dest.exists() and dest.stat().st_size > 4096
        finally:
            helpers.start_gateway(lane_a_daemon_yaml.parent / "gateway.yml")

    @e2e_test(
        id='phase0.1aa8ac9074c3b41486cdee81',
        title='Export Apply Entry Cap Error',
        description='Validates the behavior exercised by Export Apply Entry Cap Error.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-export-apply-entry-cap-error': 'The assertions for export apply entry cap error hold.'},
        execution_surface='cli',
    )
    @pytest.mark.slow
    def test_export_apply_entry_cap_error(self, lane_a_daemon_yaml, tmp_path):
        """P1-F3 — manager.export.max_apply_entries: 1 fails a dir-mode export
        of a two-file delta with the entry-cap error, with zero writes into
        the destination. Restores the module's family gateway on exit."""
        capped_yaml = helpers.make_config(
            {"manager": {"export": {"max_apply_entries": 1}}},
            tmp_path / "gateway-entry-cap.yml",
        )
        try:
            with helpers.gateway_with_config(capped_yaml):
                with helpers.sandbox() as sandbox_id:
                    helpers.exec_output(
                        sandbox_id,
                        "printf one > entry-a.txt && printf two > entry-b.txt",
                    )
                    dest = tmp_path / "entry-capped-dest"
                    result = climod.manager(
                        "export_changes",
                        "--sandbox-id",
                        sandbox_id,
                        "--dest",
                        str(dest),
                        "--format",
                        "dir",
                    )
                    error = helpers.error_text(result)
                    assert "entry-count cap exceeded" in error, error
                    assert not dest.exists() or not any(dest.iterdir()), (
                        "a capped dir export must not write into dest"
                    )
        finally:
            helpers.start_gateway(lane_a_daemon_yaml.parent / "gateway.yml")


class TestPhase2:
    """daemon.server limits, observability.views (phase 2). Lane A only."""

    @e2e_test(
        id='phase0.6d2d24764b7ec0cba0d1005b',
        title='Request Cap Rejects Oversized Write',
        description='Validates the behavior exercised by Request Cap Rejects Oversized Write.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-request-cap-rejects-oversized-write': 'The assertions for request cap rejects oversized write hold.'},
        execution_surface='cli',
    )
    def test_request_cap_rejects_oversized_write(self, lane_a_daemon_yaml):
        """P2-F1 — daemon.server.max_request_bytes: 65536 rejects a file_write
        whose request envelope exceeds 64 KiB with the daemon's
        request-too-large error; the default arm accepts the same payload."""
        payload = "x" * (96 * 1024)
        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml, {"daemon": {"server": {"max_request_bytes": 65536}}}
        )
        with helpers.sandbox() as sandbox_id:
            result = climod.runtime(
                sandbox_id, "file_write", "--path", "cap.txt", "--content", payload
            )
            error = helpers.error_text(result)
            assert "exceeds" in error and "byte limit" in error, error

        helpers.rewrite_daemon_yaml(lane_a_daemon_yaml)
        with helpers.sandbox() as sandbox_id:
            result = climod.runtime(
                sandbox_id, "file_write", "--path", "cap.txt", "--content", payload
            )
            assert isinstance(result, dict) and not climod.is_error(result), (
                f"default arm must accept the payload: {result}"
            )

    @e2e_test(
        id='phase0.df09d255a4308e4bdff4e209',
        title='Layer Delta View Honors Default Limit',
        description='Validates the behavior exercised by Layer Delta View Honors Default Limit.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-layer-delta-view-honors-default-limit': 'The assertions for layer delta view honors default limit hold.'},
        execution_surface='cli',
    )
    def test_layer_delta_view_honors_default_limit(self, lane_a_daemon_yaml):
        """P2-F2 — observability.views.layer_delta_default_limit: 3 caps the
        layer-delta view at 3 entries for a layer carrying more, with the
        truncation flag set; an explicit limit above layer_delta_max_limit is
        rejected. The layer_id/limit args ride the authenticated raw gateway
        call — the CLI catalog exposes only the inventory shape."""

        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml,
            {
                "observability": {
                    "views": {
                        "layer_delta_default_limit": 3,
                        "layer_delta_max_limit": 10,
                    }
                }
            },
        )
        with helpers.sandbox() as sandbox_id:
            helpers.exec_output(
                sandbox_id,
                "mkdir -p delta && for i in 1 2 3 4 5 6; do echo $i > delta/f$i.txt; done",
            )
            inventory = climod.raw_gateway(sandbox_id, "layerstack", {})
            layers = inventory.get("layers")
            assert layers, f"layerstack inventory must list layers: {inventory}"
            published = [
                layer["layer_id"]
                for layer in layers
                if not layer["layer_id"].startswith("B")
            ]
            assert published, f"the exec must have published a delta layer: {layers}"
            delta_layer = published[0]

            view = climod.raw_gateway(
                sandbox_id,
                "layerstack",
                {"layer_id": delta_layer},
            )
            entries = view.get("entries")
            assert entries is not None and len(entries) == 3, (
                f"default limit 3 must cap the delta entries: {view}"
            )
            assert view.get("truncated") is True, view

            rejected = climod.raw_gateway(
                sandbox_id,
                "layerstack",
                {"layer_id": delta_layer, "limit": 50},
            )
            error = helpers.error_text(rejected)
            assert "limit exceeds max" in error, error


class TestPhase3:
    """runtime.command, runtime.file, runtime.namespace_execution (phase 3).
    Lane A only."""

    @e2e_test(
        id='phase0.e435dfddfc6422e8c447152b',
        title='File List Truncates At Cap',
        description='Validates the behavior exercised by File List Truncates At Cap.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-file-list-truncates-at-cap': 'The assertions for file list truncates at cap hold.'},
        execution_surface='cli',
    )
    def test_file_list_truncates_at_cap(self, lane_a_daemon_yaml):
        """P3-F1 — runtime.file.max_list_entries: 5 lists exactly 5 of 10
        entries with the truncation flag set through the documented
        ``POST /files/list`` HTTP surface."""

        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml, {"runtime": {"file": {"max_list_entries": 5}}}
        )
        with helpers.sandbox() as sandbox_id:
            helpers.exec_output(
                sandbox_id,
                "mkdir -p listing"
                " && for i in 0 1 2 3 4 5 6 7 8 9; do echo $i > listing/f$i.txt; done",
            )
            host, port = daemon_http_endpoint(sandbox_id)
            status, body, content_type = http_post(
                f"http://{host}:{port}/files/list",
                {"path": "listing"},
            )
            assert status == 200, body
            assert "application/json" in content_type, content_type
            listing = json.loads(body)
            entries = listing.get("entries")
            assert entries is not None and len(entries) == 5, (
                f"max_list_entries 5 must cap the listing: {listing}"
            )
            assert listing.get("truncated") is True, listing

    @e2e_test(
        id='phase0.6e9f10a5c97a4142f9e3f5e8',
        title='File Read Default Lines',
        description='Validates the behavior exercised by File Read Default Lines.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-file-read-default-lines': 'The assertions for file read default lines hold.'},
        execution_surface='cli',
    )
    def test_file_read_default_lines(self, lane_a_daemon_yaml):
        """P3-F2 (adapted) — runtime.file.read_lines_default: 10 returns 10
        lines of a 100-line file when the request names no limit.

        Spec drift: the CLI materializes the catalog's `--limit` default
        (2000) client-side — the argument surface is contract, per the spec's
        non-goals — so the service default governs the raw operation surface
        and the probe rides the authenticated raw gateway call."""

        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml, {"runtime": {"file": {"read_lines_default": 10}}}
        )
        with helpers.sandbox() as sandbox_id:
            helpers.exec_output(sandbox_id, "seq 1 100 > lines.txt")
            result = climod.raw_gateway(
                sandbox_id, "file_read", {"path": "lines.txt"}
            )
            assert not climod.is_error(result), result
            lines = [line for line in result.get("content", "").split("\n") if line]
            assert len(lines) == 10, f"default window must be 10 lines: {result}"
            assert lines[0] == "1" and lines[-1] == "10", lines

    @e2e_test(
        id='phase0.1a062c1bccce4faa075f85d1',
        title='File Edit Size Cap Error',
        description='Validates the behavior exercised by File Edit Size Cap Error.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-file-edit-size-cap-error': 'The assertions for file edit size cap error hold.'},
        execution_surface='cli',
    )
    def test_file_edit_size_cap_error(self, lane_a_daemon_yaml):
        """P3-F3 — runtime.file.max_edit_bytes: 1024 fails an edit of a 2 KiB
        file with the size-cap error."""
        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml, {"runtime": {"file": {"max_edit_bytes": 1024}}}
        )
        with helpers.sandbox() as sandbox_id:
            helpers.exec_output(
                sandbox_id, "head -c 2048 /dev/zero | tr '\\0' 'a' > big.txt"
            )
            result = climod.runtime(
                sandbox_id,
                "file_edit",
                "--path",
                "big.txt",
                "--edits",
                '[{"old_string": "aaaa", "new_string": "bbbb"}]',
            )
            error = helpers.error_text(result)
            assert "too large" in error, error

    @e2e_test(
        id='phase0.e1e3c8635fdc933760ae4cb1',
        title='Command Admission Cap',
        description='Validates the behavior exercised by Command Admission Cap.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-command-admission-cap': 'The assertions for command admission cap hold.'},
        execution_surface='cli',
    )
    def test_command_admission_cap(self, lane_a_daemon_yaml):
        """P3-F4 — runtime.command.max_active: 1 refuses a second submission
        with the admission error while one long-running command is active."""
        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml, {"runtime": {"command": {"max_active": 1}}}
        )
        with helpers.sandbox() as sandbox_id:
            running = climod.runtime(
                sandbox_id, "exec_command", "--yield-time-ms", "100", "sleep 15"
            )
            assert isinstance(running, dict) and running.get("status") == "running", (
                f"the first command must still be running: {running}"
            )
            refused = climod.runtime(sandbox_id, "exec_command", "echo second")
            error = helpers.error_text(refused)
            assert "admission refused" in error, error

    @e2e_test(
        id='phase0.4ca68485652f8c133c749f4c',
        title='Terminal Retention Eviction',
        description='Validates the behavior exercised by Terminal Retention Eviction.',
        features=('manager.management', 'runtime.command'),
        validations={'assert-terminal-retention-eviction': 'The assertions for terminal retention eviction hold.'},
        execution_surface='cli',
    )
    def test_terminal_retention_eviction(self, lane_a_daemon_yaml):
        """P3-F5 — runtime.namespace_execution.max_terminal_entries: 2 evicts
        the oldest of three completed commands: draining it answers the empty
        terminal read (the landed missing-entry contract), while the two
        newest still serve their transcripts."""
        helpers.rewrite_daemon_yaml(
            lane_a_daemon_yaml,
            {"runtime": {"namespace_execution": {"max_terminal_entries": 2}}},
        )
        import time

        with helpers.sandbox() as sandbox_id:
            ids = {}
            for word in ("one", "two", "three"):
                started = climod.runtime(
                    sandbox_id,
                    "exec_command",
                    "--yield-time-ms",
                    "50",
                    f"sleep 0.3 && echo {word}",
                )
                assert isinstance(started, dict) and started.get("status") == "running", (
                    f"the command must outlive its yield to expose its id: {started}"
                )
                ids[word] = started["command_session_id"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    polled = climod.runtime(
                        sandbox_id,
                        "read_command_lines",
                        "--command-session-id",
                        ids[word],
                    )
                    if isinstance(polled, dict) and polled.get("status") == "ok":
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError(f"command {word} never completed")

            evicted = climod.runtime(
                sandbox_id,
                "read_command_lines",
                "--command-session-id",
                ids["one"],
            )
            assert isinstance(evicted, dict) and not climod.is_error(evicted), evicted
            assert not evicted.get("output"), (
                f"the evicted command must drain empty: {evicted}"
            )

            for word in ("two", "three"):
                drained = climod.runtime(
                    sandbox_id,
                    "read_command_lines",
                    "--command-session-id",
                    ids[word],
                )
                assert isinstance(drained, dict) and word in str(
                    drained.get("output", "")
                ), f"retained command {word} must drain its transcript: {drained}"
