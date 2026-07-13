"""SS-M01..SS-M15 — medium tier.

Full deny/allow set sweeps, capability decode, real ``util-linux`` / ``apt``
tooling, and multi-step scoping checks. Read-only cases run their own probe in a
shared module sandbox; package installs and mutation take a dedicated
``fresh_sandbox``. Each test is named after its SS-ID and carries the catalog's
Guards text as the assert message.

Image: ubuntu:24.04 (pinned via ``core.config.IMAGE`` / ``E2E_IMAGE``).
"""

import pytest

from harness.runner.cli import is_error
from runtime.shell_security.helpers import (
    ALLOWED_SYSCALLS,
    CAP_CHOWN,
    CAP_DAC_OVERRIDE,
    CAP_FOWNER,
    CAP_NET_ADMIN,
    CAP_SETFCAP,
    CAP_SYS_ADMIN,
    CAP_SYS_MODULE,
    DENIED_SYSCALLS,
    apt_install_command,
    exec_cmd,
    file_read,
    file_write,
    has_cap,
    run_probe,
    run_to_completion,
)
from harness.catalog.declarations import e2e_test

pytestmark = pytest.mark.medium


@e2e_test(
    timeout_ms=2_000,
    id='phase0.06b29fd7c09723a02dc6f0f4',
    title='Ss M01 Full Deny Set',
    description='Validates the behavior exercised by Ss M01 Full Deny Set.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m01-full-deny-set': 'The assertions for ss m01 full deny set hold.'},
    execution_surface='cli',
)
def test_ss_m01_full_deny_set(probe):
    for name in DENIED_SYSCALLS:
        assert probe.get(name) == "EPERM", f"full current deny set in one run | {name}={probe.get(name)}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.5498cc25584c066d2773ded4',
    title='Ss M02 Full Allow Set',
    description='Validates the behavior exercised by Ss M02 Full Allow Set.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m02-full-allow-set': 'The assertions for ss m02 full allow set hold.'},
    execution_surface='cli',
)
def test_ss_m02_full_allow_set(probe):
    for name in ALLOWED_SYSCALLS:
        if name == "fchmodat2":
            assert probe.get(name) in ("OK", "ENOSYS"), f"usability refinements not swept up | {name}={probe.get(name)}"
            continue
        assert probe.get(name) == "OK", f"usability refinements not swept up | {name}={probe.get(name)}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.cde90f97993fa86ebf4c3677',
    title='Ss M03 System Power Caps Dropped',
    description='Validates the behavior exercised by Ss M03 System Power Caps Dropped.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m03-system-power-caps-dropped': 'The assertions for ss m03 system power caps dropped hold.'},
    execution_surface='cli',
)
def test_ss_m03_system_power_caps_dropped(probe):
    for cap in (CAP_SYS_ADMIN, CAP_NET_ADMIN, CAP_SYS_MODULE):
        assert not has_cap(probe["capeff"], cap), f"system-power caps dropped | capeff={probe.get('capeff')} bit={cap}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.a189f8b432e3af0c18b76913',
    title='Ss M04 Fs Identity Caps Kept',
    description='Validates the behavior exercised by Ss M04 Fs Identity Caps Kept.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m04-fs-identity-caps-kept': 'The assertions for ss m04 fs identity caps kept hold.'},
    execution_surface='cli',
)
def test_ss_m04_fs_identity_caps_kept(probe):
    for cap in (CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FOWNER, CAP_SETFCAP):
        assert has_cap(probe["capeff"], cap), f"FS/identity caps kept so pkg managers work | capeff={probe.get('capeff')} bit={cap}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.f60ec41f57128fafe862f7a1',
    title='Ss M05 Sys Admin Dropped From Bounding Set',
    description='Validates the behavior exercised by Ss M05 Sys Admin Dropped From Bounding Set.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m05-sys-admin-dropped-from-bounding-set': 'The assertions for ss m05 sys admin dropped from bounding set hold.'},
    execution_surface='cli',
)
def test_ss_m05_sys_admin_dropped_from_bounding_set(probe):
    assert not has_cap(probe["capbnd"], CAP_SYS_ADMIN), f"dropped from the bounding set → not re-gainable via execve | capbnd={probe.get('capbnd')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.b80f4405d3f94d45c89eae24',
    title='Ss M06 Image Unshare Denied',
    description='Validates the behavior exercised by Ss M06 Image Unshare Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m06-image-unshare-denied': 'The assertions for ss m06 image unshare denied hold.'},
    execution_surface='cli',
)
def test_ss_m06_image_unshare_denied(module_sandbox):
    result = exec_cmd(
        module_sandbox,
        "sh -lc 'command -v unshare >/dev/null || exit 77; unshare -m true'",
    )
    if result.get("status") == "error" and result.get("exit_code") == 77:
        pytest.skip("image does not include unshare")
    assert result.get("status") != "ok", f"filter holds against real util-linux | {result}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.33606d0f86785d10f2b100ed',
    title='Ss M07 Image Mount Tools Denied',
    description='Validates the behavior exercised by Ss M07 Image Mount Tools Denied.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m07-image-mount-tools-denied': 'The assertions for ss m07 image mount tools denied hold.'},
    execution_surface='cli',
)
def test_ss_m07_image_mount_tools_denied(module_sandbox):
    mount_result = exec_cmd(
        module_sandbox,
        "sh -lc 'mkdir -p /tmp/eos-cs-tool-mount && mount -t tmpfs none /tmp/eos-cs-tool-mount'",
    )
    assert mount_result.get("status") != "ok", f"mount tools rejected | mount: {mount_result}"

    umount_result = exec_cmd(module_sandbox, "umount /workspace")
    assert umount_result.get("status") != "ok", f"mount tools rejected | umount: {umount_result}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.25e96586353ad38bbe9aa498',
    title='Ss M08 Package Manager Starts',
    description='Validates the behavior exercised by Ss M08 Package Manager Starts.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m08-package-manager-starts': 'The assertions for ss m08 package manager starts hold.'},
    execution_surface='cli',
)
def test_ss_m08_package_manager_starts(module_sandbox):
    version = exec_cmd(module_sandbox, "apt-get --version")
    assert version.get("status") == "ok", f"pkg manager runs under reduced caps/seccomp | {version}"


@e2e_test(
    timeout_ms=43_000,
    id='phase0.3ca5dea3d573b39e7adbb897',
    title='Ss M09 Real Install Under Policy',
    description='Validates the behavior exercised by Ss M09 Real Install Under Policy.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m09-real-install-under-policy': 'The assertions for ss m09 real install under policy hold.'},
    execution_surface='cli',
)
def test_ss_m09_real_install_under_policy(fresh_sandbox):
    version = exec_cmd(fresh_sandbox, "apt-get --version")
    assert version.get("status") == "ok", f"real install under policy; poll the session | {version}"

    install = run_to_completion(
        fresh_sandbox,
        apt_install_command(["hello"], then=["hello >/tmp/eos-cs-hello.out"]),
        timeout_s=180,
    )
    if install.get("status") != "ok":
        output = repr(install).lower()
        for marker in ("operation not permitted", "setgroups", "seccomp", "capability"):
            assert marker not in output, f"real install under policy; poll the session | {install}"
    assert install.get("status") == "ok", f"real install under policy; poll the session | {install}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.b92fa88fed87a7301befb542',
    title='Ss M10 Dac Override Kept',
    description='Validates the behavior exercised by Ss M10 Dac Override Kept.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m10-dac-override-kept': 'The assertions for ss m10 dac override kept hold.'},
    execution_surface='cli',
)
def test_ss_m10_dac_override_kept(probe):
    assert probe["dac_override"] == "OK", f"DAC_OVERRIDE kept | dac_override={probe.get('dac_override')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.7b175bb916514672daa942f7',
    title='Ss M11 Device Node Mode Filter Both Directions',
    description='Validates the behavior exercised by Ss M11 Device Node Mode Filter Both Directions.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m11-device-node-mode-filter-both-directions': 'The assertions for ss m11 device node mode filter both directions hold.'},
    execution_surface='cli',
)
def test_ss_m11_device_node_mode_filter_both_directions(probe):
    assert probe["mknod_char"] == "EPERM", f"mode-filter both directions | mknod_char={probe.get('mknod_char')}"
    assert probe["mknod_block"] == "EPERM", f"mode-filter both directions | mknod_block={probe.get('mknod_block')}"
    assert probe["mknod_fifo"] == "OK", f"mode-filter both directions | mknod_fifo={probe.get('mknod_fifo')}"
    assert probe["mknod_regular"] == "OK", f"mode-filter both directions | mknod_regular={probe.get('mknod_regular')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.28be718328b1218f078f9707',
    title='Ss M12 Rename And Chmod Allowed',
    description='Validates the behavior exercised by Ss M12 Rename And Chmod Allowed.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m12-rename-and-chmod-allowed': 'The assertions for ss m12 rename and chmod allowed hold.'},
    execution_surface='cli',
)
def test_ss_m12_rename_and_chmod_allowed(probe):
    assert probe["renameat"] == "OK", f"not caught by the deny table | renameat={probe.get('renameat')}"
    assert probe["renameat2"] == "OK", f"not caught by the deny table | renameat2={probe.get('renameat2')}"
    assert probe["fchmodat2"] in ("OK", "ENOSYS"), f"not caught by the deny table | fchmodat2={probe.get('fchmodat2')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.944badf226efcfca719768e8',
    title='Ss M13 Ptrace Kept Within Pid Namespace',
    description='Validates the behavior exercised by Ss M13 Ptrace Kept Within Pid Namespace.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m13-ptrace-kept-within-pid-namespace': 'The assertions for ss m13 ptrace kept within pid namespace hold.'},
    execution_surface='cli',
)
def test_ss_m13_ptrace_kept_within_pid_namespace(probe):
    assert probe["ptrace"] == "OK", f"ptrace kept, confined to the PID namespace | ptrace(TRACEME)={probe.get('ptrace')}"
    assert probe["ptrace_attach"] == "OK", f"ptrace kept, confined to the PID namespace | ptrace(ATTACH)={probe.get('ptrace_attach')}"


@e2e_test(
    timeout_ms=1_000,
    id='phase0.646ab3f20c9e63095a530a6d',
    title='Ss M14 Every Child Independently Hardened',
    description='Validates the behavior exercised by Ss M14 Every Child Independently Hardened.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m14-every-child-independently-hardened': 'The assertions for ss m14 every child independently hardened hold.'},
    execution_surface='cli',
)
def test_ss_m14_every_child_independently_hardened(module_sandbox):
    first = run_probe(module_sandbox)
    second = run_probe(module_sandbox)
    for label, report in (("run1", first), ("run2", second)):
        assert report["nnp"] == "1", f"every child independently hardened; no leak/persistence | {label} nnp={report.get('nnp')}"
        assert report["seccomp"] == "2", f"every child independently hardened; no leak/persistence | {label} seccomp={report.get('seccomp')}"


@e2e_test(
    timeout_ms=2_000,
    id='phase0.9de5f149723a9593d1bd3b4c',
    title='Ss M15 Policy Scoped To Shell Exec Child',
    description='Validates the behavior exercised by Ss M15 Policy Scoped To Shell Exec Child.',
    features=('runtime.shell_security',),
    validations={'assert-ss-m15-policy-scoped-to-shell-exec-child': 'The assertions for ss m15 policy scoped to shell exec child hold.'},
    execution_surface='cli',
)
def test_ss_m15_policy_scoped_to_shell_exec_child(module_sandbox, probe):
    content = "ss-m15-overlay"
    written = file_write(module_sandbox, "m15.txt", content)
    assert not is_error(written), f"policy scoped to the shell-exec child, not setup | write {written}"
    read = file_read(module_sandbox, "m15.txt")
    assert not is_error(read) and read.get("content") == content, f"policy scoped to the shell-exec child, not setup | read {read}"

    assert probe["nnp"] == "1", f"policy scoped to the shell-exec child, not setup | probe nnp={probe.get('nnp')}"
    assert probe["mount"] == "EPERM", f"policy scoped to the shell-exec child, not setup | probe mount={probe.get('mount')}"
