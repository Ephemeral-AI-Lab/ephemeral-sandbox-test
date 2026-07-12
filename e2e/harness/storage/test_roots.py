from pathlib import Path

import pytest

from harness.storage.roots import (
    BENCHMARK_STATE_MARKER,
    E2E_STATE_MARKER,
    RootValidationError,
    assert_safe_destructive_target,
    derive_roots,
    initialize_benchmark_state,
    initialize_e2e_state,
)


def _roots(tmp_path):
    test_root = tmp_path / "external-tests"
    product_root = tmp_path / "product"
    (test_root / "e2e").mkdir(parents=True)
    product_root.mkdir()
    return test_root, product_root


def test_find_repo_root_skips_partial_marker_directory(tmp_path):
    test_root, product_root = _roots(tmp_path)

    roots = derive_roots(test_root, product_root)

    assert roots.e2e_source_root == test_root / "e2e"
    assert roots.benchmark_source_root == test_root / "benchmark"
    assert roots.e2e_state_root == test_root / ".e2e-state"
    assert roots.benchmark_state_root == test_root / ".benchmark-state"
    assert Path(roots.product_root) == product_root
    assert initialize_e2e_state(roots) == roots.e2e_state_root
    assert initialize_benchmark_state(roots) == roots.benchmark_state_root
    assert E2E_STATE_MARKER["role"] == "e2e-state"
    assert BENCHMARK_STATE_MARKER["role"] == "benchmark-state"


def test_find_repo_root_fails_without_markers(tmp_path):
    test_root, product_root = _roots(tmp_path)
    alias = tmp_path / "test-alias"
    alias.symlink_to(test_root, target_is_directory=True)

    with pytest.raises(RootValidationError):
        derive_roots("relative", product_root)
    with pytest.raises(RootValidationError):
        derive_roots(alias, product_root)
    with pytest.raises(RootValidationError):
        derive_roots(test_root, test_root / "e2e")
    roots = derive_roots(test_root, product_root)
    initialize_e2e_state(roots)
    target = roots.e2e_state_root / "workspaces" / "case-a"
    target.mkdir(parents=True)

    assert assert_safe_destructive_target(target, roots) == target
    for protected in (test_root, product_root, roots.e2e_source_root, roots.e2e_state_root):
        with pytest.raises(RootValidationError):
            assert_safe_destructive_target(protected, roots)
