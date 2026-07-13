"""SS-E01..SS-E10 — easy tier.

One deterministic assertion per case: a single syscall verdict or status field
from the in-process probe, or a single command through the daemon path. Each test
is named after its SS-ID and carries the catalog's Guards text as the assert
message so a failure traces straight back to the case.

Image: ubuntu:24.04 (pinned via ``core.config.IMAGE`` / ``E2E_IMAGE``).
"""

import pytest

from harness.runner.cli import is_error
from runtime.shell_security.helpers import exec_cmd, file_read, file_write
from harness.catalog.declarations import e2e_test

pytestmark = pytest.mark.easy


@e2e_test(
    timeout_ms=3_000,
    id='phase0.2537b71377a542e5c1c8319f',
    title='Ss E01 Normal Commands Still Work',
    description='Validates the behavior exercised by Ss E01 Normal Commands Still Work.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e01-normal-commands-still-work': 'The assertions for ss e01 normal commands still work hold.'},
    execution_surface='cli',
)
def test_ss_e01_normal_commands_still_work(module_sandbox):
    for command in ("id -u", "uname -m", "sh -lc 'echo ok'"):
        result = exec_cmd(module_sandbox, command)
        assert not is_error(result), f"policy doesn't break normal commands; daemon path intact | {command}: {result}"
        assert result.get("status") == "ok", f"policy doesn't break normal commands; daemon path intact | {command}: {result}"
        assert result.get("output", "").strip(), f"policy doesn't break normal commands; daemon path intact | {command}: {result}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.89ab46b9d95b61632c7b442b',
    title='Ss E02 Workspace Write Path Unaffected',
    description='Validates the behavior exercised by Ss E02 Workspace Write Path Unaffected.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e02-workspace-write-path-unaffected': 'The assertions for ss e02 workspace write path unaffected hold.'},
    execution_surface='cli',
)
def test_ss_e02_workspace_write_path_unaffected(module_sandbox):
    content = "ss-e02-roundtrip"
    written = file_write(module_sandbox, "e02.txt", content)
    assert not is_error(written), f"overlay/workspace write path unaffected | {written}"
    read = file_read(module_sandbox, "e02.txt")
    assert not is_error(read), f"overlay/workspace write path unaffected | {read}"
    assert read.get("content") == content, f"overlay/workspace write path unaffected | {read}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.5f03ae739da21fbbafc6a977',
    title='Ss E03 No New Privs Installed',
    description='Validates the behavior exercised by Ss E03 No New Privs Installed.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e03-no-new-privs-installed': 'The assertions for ss e03 no new privs installed hold.'},
    execution_surface='cli',
)
def test_ss_e03_no_new_privs_installed(probe):
    assert probe["nnp"] == "1", f"NNP installed before seccomp | nnp={probe.get('nnp')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.7b28d1aec7634fc9cfacfa8a',
    title='Ss E04 Seccomp Filter Mode Active',
    description='Validates the behavior exercised by Ss E04 Seccomp Filter Mode Active.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e04-seccomp-filter-mode-active': 'The assertions for ss e04 seccomp filter mode active hold.'},
    execution_surface='cli',
)
def test_ss_e04_seccomp_filter_mode_active(probe):
    assert probe["seccomp"] == "2", f"filter mode active | seccomp={probe.get('seccomp')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.74442978b8be054dba182f34',
    title='Ss E05 Mount Mutation Denied',
    description='Validates the behavior exercised by Ss E05 Mount Mutation Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e05-mount-mutation-denied': 'The assertions for ss e05 mount mutation denied hold.'},
    execution_surface='cli',
)
def test_ss_e05_mount_mutation_denied(probe):
    assert probe["mount"] == "EPERM", f"mount-mutation denial (T1) | mount={probe.get('mount')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.81fa8e870547a89643b75a2a',
    title='Ss E06 Namespace Mutation Denied',
    description='Validates the behavior exercised by Ss E06 Namespace Mutation Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e06-namespace-mutation-denied': 'The assertions for ss e06 namespace mutation denied hold.'},
    execution_surface='cli',
)
def test_ss_e06_namespace_mutation_denied(probe):
    assert probe["unshare_newns"] == "EPERM", f"namespace-mutation denial (T2) — not cap-gated | unshare_newns={probe.get('unshare_newns')}"
    assert probe["unshare_zero"] == "EPERM", f"namespace-mutation denial (T2) — not cap-gated | unshare_zero={probe.get('unshare_zero')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.d6920f758f5b59c02e850c25',
    title='Ss E07 Device Node Char Denied',
    description='Validates the behavior exercised by Ss E07 Device Node Char Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e07-device-node-char-denied': 'The assertions for ss e07 device node char denied hold.'},
    execution_surface='cli',
)
def test_ss_e07_device_node_char_denied(probe):
    assert probe["mknod_char"] == "EPERM", f"device-node denial (T5); MKNOD cap kept, seccomp is the barrier | mknod_char={probe.get('mknod_char')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.a7775bc30a1ccb4c6eef7236',
    title='Ss E08 Device Node Fifo Allowed',
    description='Validates the behavior exercised by Ss E08 Device Node Fifo Allowed.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e08-device-node-fifo-allowed': 'The assertions for ss e08 device node fifo allowed hold.'},
    execution_surface='cli',
)
def test_ss_e08_device_node_fifo_allowed(probe):
    assert probe["mknod_fifo"] == "OK", f"mode-filter allows non-device nodes (pkg postinst) | mknod_fifo={probe.get('mknod_fifo')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.aef78c3d8e194cf159fb0f7f',
    title='Ss E09 Kernel Surface Denied',
    description='Validates the behavior exercised by Ss E09 Kernel Surface Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e09-kernel-surface-denied': 'The assertions for ss e09 kernel surface denied hold.'},
    execution_surface='cli',
)
def test_ss_e09_kernel_surface_denied(probe):
    assert probe["bpf"] == "EPERM", f"kernel-surface denial (T4) — not cap-gated | bpf={probe.get('bpf')}"
    assert probe["io_uring"] == "EPERM", f"kernel-surface denial (T4) — not cap-gated | io_uring={probe.get('io_uring')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.0785186f3356e83570623a8f',
    title='Ss E10 Clone3 Forced Enosys',
    description='Validates the behavior exercised by Ss E10 Clone3 Forced Enosys.',
    features=('runtime.shell_security',),
    validations={'assert-ss-e10-clone3-forced-enosys': 'The assertions for ss e10 clone3 forced enosys hold.'},
    execution_surface='cli',
)
def test_ss_e10_clone3_forced_enosys(probe):
    assert probe["clone3"] == "ENOSYS", f"forces glibc/musl clone(2) fallback the flag-mask inspects | clone3={probe.get('clone3')}"
