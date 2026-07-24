import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from benchmark_lab.phase1_baseline import (
    Phase1BaselineError,
    _command_output_matches,
    _small_files_totals_command,
    _verify_file,
    load_phase1_corpus,
    reclamation_verdict,
    robust_slope_bytes_per_cycle,
)
from benchmark_lab.transport import TimedGatewayResponse


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "fixtures/layerstack-phase1-v1"


def test_frozen_phase1_corpus_has_exact_tiny_shape() -> None:
    manifest, values = load_phase1_corpus(CORPUS)

    assert manifest.corpus_version == "v1"
    assert len(values["localized_base"].encode()) == 1024 * 1024
    assert len(values["incompressible"].encode()) == 1024 * 1024
    assert len(values["small_source"].encode()) == 1024 * 1024
    assert [len(item.encode()) for item in values["history_payloads"]] == [4096] * 32


def test_frozen_phase1_corpus_rejects_digest_drift(tmp_path: Path) -> None:
    shutil.copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    (tmp_path / "localized_base.gz").write_bytes(b"not-gzip")

    with pytest.raises(Phase1BaselineError, match="compressed digest mismatch"):
        load_phase1_corpus(tmp_path)


@pytest.mark.parametrize("output", ["256 1048576", "256 1048576\n"])
def test_command_output_match_ignores_only_token_whitespace(output: str) -> None:
    assert _command_output_matches(
        {"exit_code": 0, "output": output}, ["256", "1048576"]
    )
    assert not _command_output_matches(
        {"exit_code": 1, "output": output}, ["256", "1048576"]
    )
    assert not _command_output_matches(
        {"exit_code": 0, "output": "256 1048577"}, ["256", "1048576"]
    )


def test_small_file_totals_command_counts_each_file_once(tmp_path: Path) -> None:
    for index, content in enumerate((b"a", b"bb", b"cccccc")):
        (tmp_path / f"file-{index:03d}.bin").write_bytes(content)
    (tmp_path / "ignored.bin").write_bytes(b"not counted")

    completed = subprocess.run(
        ["sh", "-c", _small_files_totals_command(str(tmp_path))],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.split() == ["3", "9"]


@pytest.mark.asyncio
async def test_large_single_line_verification_uses_bounded_digest_output() -> None:
    expected = "x" * (1024 * 1024)
    expected_digest = hashlib.sha256(expected.encode()).hexdigest()

    class Product:
        async def file_read(self, *args, **kwargs):
            raise AssertionError("oversized single-line content cannot use file_read")

        async def exec_command(
            self, sandbox, *, session_id, command, timeout_ms, request_id
        ):
            assert sandbox == "sandbox-1"
            assert session_id is None
            assert "wc -c" in command
            assert "sha256sum" in command
            return TimedGatewayResponse(
                request_id,
                123,
                17,
                "sha256:response",
                {
                    "exit_code": 0,
                    "output": f"{len(expected.encode())} {expected_digest}\n",
                },
            )

    responses = []
    await _verify_file(
        Product(),
        "sandbox-1",
        ".eos-benchmark-phase1/large.bin",
        expected,
        120_000,
        "large.verify",
        responses,
    )

    assert [response.request_id for response in responses] == ["large.verify"]


def test_robust_slope_uses_all_pairwise_cycle_slopes() -> None:
    assert robust_slope_bytes_per_cycle([10, 20, 30, 40]) == 10.0
    assert robust_slope_bytes_per_cycle([10]) is None


@pytest.mark.parametrize(
    ("logical", "saturated", "samples", "band", "expected"),
    [
        (True, True, [0] * 20, 10, "measurement-unavailable"),
        (False, False, [0] * 20, 10, "confirmed-leak"),
        (True, False, list(range(0, 60, 3))[:20], 10, "confirmed-leak"),
        (True, False, list(range(20)), 10, "suspected-leak"),
        (True, False, list(range(20)), 100, "allocator-or-page-cache-retained"),
        (True, False, [20 - index for index in range(20)], 100, "bounded-retained-by-design"),
        (True, False, [7] * 20, 100, "bounded-and-released"),
    ],
)
def test_reclamation_verdict_is_closed_and_deterministic(
    logical: bool,
    saturated: bool,
    samples: list[int],
    band: int,
    expected: str,
) -> None:
    assert (
        reclamation_verdict(
            logical_release_complete=logical,
            counter_saturated=saturated,
            memory_samples=samples,
            noise_band_bytes=band,
        )
        == expected
    )
