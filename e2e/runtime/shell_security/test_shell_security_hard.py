"""SS-H01..SS-H15 — hard tier.

Adversarial / exploit-shaped, arch-specific, and orchestration-heavy cases. Most
denial checks read the shared module probe; the X32 reject, setuid/setcap
helpers, and the same-pgid subtree take dedicated sandboxes. Each test is named
after its SS-ID and carries the catalog's Guards text as the assert message.

Image: ubuntu:24.04 (pinned via ``core.config.IMAGE`` / ``E2E_IMAGE``).
"""

import pytest

from harness.runner.cli import is_error
from runtime.shell_security.helpers import (
    CAP_SYS_ADMIN,
    apt_install_command,
    exec_cmd,
    file_write,
    has_cap,
    linux_musl_target,
    parse_probe_output,
    run_probe_raw,
    run_to_completion,
)
from harness.catalog.declarations import e2e_test

pytestmark = pytest.mark.hard

_TARGET_IS_X86_64 = linux_musl_target().startswith("x86_64")

_NEW_MOUNT_API = ("fsopen", "fsconfig", "fsmount", "fspick", "move_mount", "open_tree")
_MOUNT_MUTATION_FAMILY = ("umount2", "pivot_root", "mount_setattr")
_MODULE_LOAD = ("init_module", "finit_module")
_IO_URING = ("io_uring", "io_uring_enter", "io_uring_register")
_KERNEL_LPE = ("userfaultfd", "perf_event_open", "fanotify_init")
_RESOURCE_DOS = ("swapon", "swapoff", "quotactl", "reboot")


@e2e_test(
    id='phase0.1b6c10af9312cc8c7e40cbb6',
    title='Ss H01 Userns Escape Primitive Denied',
    description='Validates the behavior exercised by Ss H01 Userns Escape Primitive Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h01-userns-escape-primitive-denied': 'The assertions for ss h01 userns escape primitive denied hold.'},
    execution_surface='cli',
)
def test_ss_h01_userns_escape_primitive_denied(probe):
    assert probe["unshare_newuser"] == "EPERM", f"blocks the userns→full-cap-set pivot; not cap-gated | unshare_newuser={probe.get('unshare_newuser')}"


@e2e_test(
    id='phase0.4822bceeeb57aacaae7f35ef',
    title='Ss H02 Clone New Flag Mask Denies Exactly',
    description='Validates the behavior exercised by Ss H02 Clone New Flag Mask Denies Exactly.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h02-clone-new-flag-mask-denies-exactly': 'The assertions for ss h02 clone new flag mask denies exactly hold.'},
    execution_surface='cli',
)
def test_ss_h02_clone_new_flag_mask_denies_exactly(probe):
    assert probe["clone_newuser"] == "EPERM", f"flag-mask rule denies exactly the CLONE_NEW* bits | clone_newuser={probe.get('clone_newuser')}"
    assert probe["clone_sigchld"] == "OK", f"flag-mask rule denies exactly the CLONE_NEW* bits | clone_sigchld={probe.get('clone_sigchld')}"


@e2e_test(
    id='phase0.c0bbf4c05645f0b5def79e5f',
    title='Ss H03 Clone3 Blanket Enosys',
    description='Validates the behavior exercised by Ss H03 Clone3 Blanket Enosys.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h03-clone3-blanket-enosys': 'The assertions for ss h03 clone3 blanket enosys hold.'},
    execution_surface='cli',
)
def test_ss_h03_clone3_blanket_enosys(probe):
    assert probe["clone3"] == "ENOSYS", f"seccomp can't deref the clone3 args pointer → blanket ENOSYS | clone3={probe.get('clone3')}"
    assert probe["clone3_args"] == "ENOSYS", f"seccomp can't deref the clone3 args pointer → blanket ENOSYS | clone3_args={probe.get('clone3_args')}"


@e2e_test(
    id='phase0.a6067df7b4b6dce75fdc008b',
    title='Ss H04 X32 Abi Rejected',
    description='Validates the behavior exercised by Ss H04 X32 Abi Rejected.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h04-x32-abi-rejected': 'The assertions for ss h04 x32 abi rejected hold.'},
    execution_surface='cli',
)
@pytest.mark.skipif(not _TARGET_IS_X86_64, reason="X32 ABI reject is x86_64-only; skip on aarch64")
def test_ss_h04_x32_abi_rejected(module_sandbox, probe):
    result = run_probe_raw(module_sandbox, "x32")
    assert result.get("status") != "ok", f"killed (SECCOMP_RET_KILL_PROCESS); native call unaffected | x32 not rejected: {result}"
    assert "x32_survived" not in result.get("output", ""), f"killed (SECCOMP_RET_KILL_PROCESS); native call unaffected | x32 filter absent: {result}"
    assert probe["nnp"] == "1", f"killed (SECCOMP_RET_KILL_PROCESS); native call unaffected | native probe broken: {probe}"


@e2e_test(
    id='phase0.006a19c20f8389e655e6a120',
    title='Ss H05 Open By Handle At Denied',
    description='Validates the behavior exercised by Ss H05 Open By Handle At Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h05-open-by-handle-at-denied': 'The assertions for ss h05 open by handle at denied hold.'},
    execution_surface='cli',
)
def test_ss_h05_open_by_handle_at_denied(probe):
    assert probe["open_by_handle_at"] == "EPERM", f"seccomp is the only barrier — DAC_READ_SEARCH is kept | open_by_handle_at={probe.get('open_by_handle_at')}"


@e2e_test(
    id='phase0.fbf4091f6553bb3b3341b3e1',
    title='Ss H06 New Mount Api Denied',
    description='Validates the behavior exercised by Ss H06 New Mount Api Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h06-new-mount-api-denied': 'The assertions for ss h06 new mount api denied hold.'},
    execution_surface='cli',
)
def test_ss_h06_new_mount_api_denied(probe):
    for name in _NEW_MOUNT_API:
        assert probe.get(name) == "EPERM", f"can't sidestep the classic mount denial via the new API | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.98c52f7ea15e09ecd15b650e',
    title='Ss H07 Mount Mutation Family Denied',
    description='Validates the behavior exercised by Ss H07 Mount Mutation Family Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h07-mount-mutation-family-denied': 'The assertions for ss h07 mount mutation family denied hold.'},
    execution_surface='cli',
)
def test_ss_h07_mount_mutation_family_denied(probe):
    for name in _MOUNT_MUTATION_FAMILY:
        assert probe.get(name) == "EPERM", f"full mount-mutation family, not just mount | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.c502b23e8dd2827da8d26140',
    title='Ss H08 Module Load Denied',
    description='Validates the behavior exercised by Ss H08 Module Load Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h08-module-load-denied': 'The assertions for ss h08 module load denied hold.'},
    execution_surface='cli',
)
def test_ss_h08_module_load_denied(probe):
    for name in _MODULE_LOAD:
        assert probe.get(name) == "EPERM", f"kernel-module load (T3) — SYS_MODULE dropped + seccomp | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.d1d8fe1e53d0f56cb24f70f0',
    title='Ss H09 Io Uring Ring Closed',
    description='Validates the behavior exercised by Ss H09 Io Uring Ring Closed.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h09-io-uring-ring-closed': 'The assertions for ss h09 io uring ring closed hold.'},
    execution_surface='cli',
)
def test_ss_h09_io_uring_ring_closed(probe):
    for name in _IO_URING:
        assert probe.get(name) == "EPERM", f"close the whole ring — io_uring is a proxy-syscall bypass surface | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.5ba6e5e05e7bb747df6e964d',
    title='Ss H10 Kernel Lpe Surfaces Denied',
    description='Validates the behavior exercised by Ss H10 Kernel Lpe Surfaces Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h10-kernel-lpe-surfaces-denied': 'The assertions for ss h10 kernel lpe surfaces denied hold.'},
    execution_surface='cli',
)
def test_ss_h10_kernel_lpe_surfaces_denied(probe):
    for name in _KERNEL_LPE:
        assert probe.get(name) == "EPERM", f"non-cap-gated LPE surfaces — seccomp load-bearing | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.030e77144439706c29b4715e',
    title='Ss H11 No New Privs Neutralizes Setuid',
    description='Validates the behavior exercised by Ss H11 No New Privs Neutralizes Setuid.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h11-no-new-privs-neutralizes-setuid': 'The assertions for ss h11 no new privs neutralizes setuid hold.'},
    execution_surface='cli',
)
def test_ss_h11_no_new_privs_neutralizes_setuid(fresh_sandbox):
    # The sandbox userns maps only uid 0 (`uid_map: 0 0 1`), so a setuid-root exec
    # cannot be observed as a uid *raise*. NoNewPrivs is what makes that safe: it
    # is inherited across the setuid execve, the euid does not transition, and no
    # dropped capability is regained. We assert exactly those invariants on a
    # genuine setuid-root helper (`test -u` guarantees the bit is set).
    stage = exec_cmd(
        fresh_sandbox,
        "sh -lc 'cp eos_shell_security_probe /tmp/eos-suid-helper && "
        "chown 0:0 /tmp/eos-suid-helper && chmod u+s,go+rx /tmp/eos-suid-helper && "
        "test -u /tmp/eos-suid-helper'",
    )
    assert stage.get("status") == "ok", f"no_new_privs neutralizes setuid | could not stage a setuid-root helper: {stage}"

    run = exec_cmd(fresh_sandbox, "/tmp/eos-suid-helper privinfo")
    assert run.get("status") == "ok", f"no_new_privs neutralizes setuid | helper did not run: {run}"
    report = parse_probe_output(run.get("output", ""))
    assert report["nnp"] == "1", f"no_new_privs neutralizes setuid | setuid-root execve not under NNP: {report}"
    assert report["euid"] == report["ruid"], f"no_new_privs neutralizes setuid | setuid changed euid {report.get('ruid')}->{report.get('euid')}"
    assert not has_cap(report["capeff"], CAP_SYS_ADMIN), f"no_new_privs neutralizes setuid | setuid-root regained SYS_ADMIN: capeff={report.get('capeff')}"


@e2e_test(
    id='phase0.652ee2dbf7c114f716af18a7',
    title='Ss H12 File Caps Defeated',
    description='Validates the behavior exercised by Ss H12 File Caps Defeated.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h12-file-caps-defeated': 'The assertions for ss h12 file caps defeated hold.'},
    execution_surface='cli',
)
def test_ss_h12_file_caps_defeated(fresh_sandbox):
    # setcap ships in libcap2-bin, which ubuntu:24.04 lacks by default — install it
    # (needs package egress), else skip cleanly.
    if exec_cmd(fresh_sandbox, "sh -lc 'command -v setcap'").get("status") != "ok":
        install = run_to_completion(
            fresh_sandbox, apt_install_command(["libcap2-bin"]), timeout_s=180
        )
        if exec_cmd(fresh_sandbox, "sh -lc 'command -v setcap'").get("status") != "ok":
            pytest.skip(f"setcap unavailable (no package egress for libcap2-bin): {install}")

    staged = exec_cmd(
        fresh_sandbox,
        "sh -lc 'cp eos_shell_security_probe /tmp/eos-cap-helper && "
        "setcap cap_sys_admin+ep /tmp/eos-cap-helper && echo SETCAP_OK && "
        "{ /tmp/eos-cap-helper privinfo || echo EXEC_DEFEATED=$?; }'",
    )
    output = staged.get("output", "")
    assert "SETCAP_OK" in output, f"NNP + bounding-set drop defeat file caps | setcap cap_sys_admin+ep did not succeed: {staged}"
    if "EXEC_DEFEATED" in output:
        # The +e file cap names SYS_ADMIN, which was dropped from the bounding set,
        # so the effective cap can never be honored and the kernel refuses the
        # execve (EPERM) — the file cap is defeated before the child even starts.
        return
    report = parse_probe_output(output)
    assert report["nnp"] == "1", f"NNP + bounding-set drop defeat file caps | {report}"
    assert not has_cap(report["capeff"], CAP_SYS_ADMIN), f"NNP + bounding-set drop defeat file caps | capeff={report.get('capeff')}"


@e2e_test(
    id='phase0.cbd3d40e8777759614da0095',
    title='Ss H13 Setns Denied Distinctly',
    description='Validates the behavior exercised by Ss H13 Setns Denied Distinctly.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h13-setns-denied-distinctly': 'The assertions for ss h13 setns denied distinctly hold.'},
    execution_surface='cli',
)
def test_ss_h13_setns_denied_distinctly(probe):
    assert probe["setns"] == "EPERM", f"setns denied distinctly from unshare | setns={probe.get('setns')}"


@e2e_test(
    id='phase0.75908b16e40ae63903fc9a0d',
    title='Ss H14 Resource Dos Family Denied',
    description='Validates the behavior exercised by Ss H14 Resource Dos Family Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h14-resource-dos-family-denied': 'The assertions for ss h14 resource dos family denied hold.'},
    execution_surface='cli',
)
def test_ss_h14_resource_dos_family_denied(probe):
    for name in _RESOURCE_DOS:
        assert probe.get(name) == "EPERM", f"resource/DoS family (T7) — belt-and-suspenders (cap + seccomp) | {name}={probe.get(name)}"


@e2e_test(
    id='phase0.cd6dbe82573100cf99aef1d1',
    title='Ss H15 Same Pgid Subtree All Denied',
    description='Validates the behavior exercised by Ss H15 Same Pgid Subtree All Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-h15-same-pgid-subtree-all-denied': 'The assertions for ss h15 same pgid subtree all denied hold.'},
    execution_surface='cli',
)
def test_ss_h15_same_pgid_subtree_all_denied(fresh_sandbox):
    subtree = run_to_completion(
        fresh_sandbox,
        "sh -lc 'for i in 1 2 3 4; do ./eos_shell_security_probe > /tmp/eos-h15-$i.out 2>&1 & done; "
        "wait; grep -h \"^mount=\" /tmp/eos-h15-*.out'",
        timeout_s=120,
    )
    assert subtree.get("status") == "ok", f"scope-wait + policy scoped to the shell-exec child only | subtree {subtree}"
    mount_lines = [line for line in subtree.get("output", "").splitlines() if line.startswith("mount=")]
    assert len(mount_lines) == 4, f"scope-wait + policy scoped to the shell-exec child only | subtree probes: {mount_lines}"
    assert all(line == "mount=EPERM" for line in mount_lines), f"scope-wait + policy scoped to the shell-exec child only | {mount_lines}"

    alive = exec_cmd(fresh_sandbox, "id -u")
    assert alive.get("status") == "ok", f"scope-wait + policy scoped to the shell-exec child only | daemon/ns-runner not alive: {alive}"
    overlay = file_write(fresh_sandbox, "h15.txt", "ss-h15\n")
    assert not is_error(overlay), f"scope-wait + policy scoped to the shell-exec child only | overlay write path lost privilege: {overlay}"
