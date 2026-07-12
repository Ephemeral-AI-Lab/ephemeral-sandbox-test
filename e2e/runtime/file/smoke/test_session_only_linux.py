"""Live e2e: Session-Only Cases (Docker/Linux sandbox)."""

from runtime.file.helpers import (
    assert_content,
    assert_error,
    assert_ok,
    exec_command,
    file_edit,
    file_read,
    file_write,
    workspace_session,
)
from harness.catalog.declarations import e2e_test


@e2e_test(
    id='phase0.7767860809e822f7d8f6b612',
    title='Session Write Updates Existing Executable File And Preserves Mode',
    description='Validates the behavior exercised by Session Write Updates Existing Executable File And Preserves Mode.',
    features=('runtime.file',),
    validations={'assert-session-write-updates-existing-executable-file-and-preserves-mode': 'The assertions for session write updates existing executable file and preserves mode hold.'},
    execution_surface='cli',
)
def test_session_write_updates_existing_executable_file_and_preserves_mode(
    sandbox, workspace_session
):
    """Session write updates an existing executable file and preserves its mode."""
    assert_ok(
        exec_command(
            sandbox,
            "printf '#!/bin/sh\\necho v1\\n' > run.sh && chmod +x run.sh",
            workspace_session_id=workspace_session,
        )
    )
    assert_ok(
        file_write(
            sandbox,
            "run.sh",
            "#!/bin/sh\necho v2\n",
            workspace_session_id=workspace_session,
        )
    )
    result = exec_command(sandbox, "./run.sh", workspace_session_id=workspace_session)
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "v2" in result["output"]


@e2e_test(
    id='phase0.22bfeb00a992b12278ac9445',
    title='Session Write To In Session Directory Is Rejected',
    description='Validates the behavior exercised by Session Write To In Session Directory Is Rejected.',
    features=('runtime.file',),
    validations={'assert-session-write-to-in-session-directory-is-rejected': 'The assertions for session write to in session directory is rejected hold.'},
    execution_surface='cli',
)
def test_session_write_to_in_session_directory_is_rejected(sandbox, workspace_session):
    """Session write to an in-session directory is rejected as invalid request /
    not regular."""
    assert_ok(exec_command(sandbox, "mkdir dir-target", workspace_session_id=workspace_session))
    assert_error(
        file_write(
            sandbox,
            "dir-target",
            "x",
            workspace_session_id=workspace_session,
        ),
        "invalid_request",
    )


@e2e_test(
    id='phase0.47c3c442cc83e5de1850b9a3',
    title='Session Write To In Session Symlink Is Rejected And Not Followed',
    description='Validates the behavior exercised by Session Write To In Session Symlink Is Rejected And Not Followed.',
    features=('runtime.file',),
    validations={'assert-session-write-to-in-session-symlink-is-rejected-and-not-followed': 'The assertions for session write to in session symlink is rejected and not followed hold.'},
    execution_surface='cli',
)
def test_session_write_to_in_session_symlink_is_rejected_and_not_followed(
    sandbox, workspace_session
):
    """Session write to an in-session symlink is rejected as invalid request /
    not regular; the symlink is not followed."""
    assert_ok(
        exec_command(
            sandbox,
            "printf real > real.txt && ln -s real.txt link.txt",
            workspace_session_id=workspace_session,
        )
    )
    assert_error(
        file_write(
            sandbox,
            "link.txt",
            "changed",
            workspace_session_id=workspace_session,
        ),
        "invalid_request",
    )
    assert_content(
        file_read(sandbox, "real.txt", workspace_session_id=workspace_session),
        "real",
    )


@e2e_test(
    id='phase0.2a85f69e5eee1ae6cb95f04a',
    title='Session Write To In Session Symlink Parent Is Rejected',
    description='Validates the behavior exercised by Session Write To In Session Symlink Parent Is Rejected.',
    features=('runtime.file',),
    validations={'assert-session-write-to-in-session-symlink-parent-is-rejected': 'The assertions for session write to in session symlink parent is rejected hold.'},
    execution_surface='cli',
)
def test_session_write_to_in_session_symlink_parent_is_rejected(sandbox, workspace_session):
    """Session write to an in-session symlink parent is rejected as invalid
    request; no symlink-parent traversal."""
    assert_ok(
        exec_command(
            sandbox,
            "mkdir realdir && ln -s realdir linkdir",
            workspace_session_id=workspace_session,
        )
    )
    assert_error(
        file_write(
            sandbox,
            "linkdir/new.txt",
            "x",
            workspace_session_id=workspace_session,
        ),
        "invalid_request",
    )


@e2e_test(
    id='phase0.baef571baf5c9b7cef27ff53',
    title='Session Edit To In Session Symlink Or Symlink Parent Is Rejected',
    description='Validates the behavior exercised by Session Edit To In Session Symlink Or Symlink Parent Is Rejected.',
    features=('runtime.file',),
    validations={'assert-session-edit-to-in-session-symlink-or-symlink-parent-is-rejected': 'The assertions for session edit to in session symlink or symlink parent is rejected hold.'},
    execution_surface='cli',
)
def test_session_edit_to_in_session_symlink_or_symlink_parent_is_rejected(
    sandbox, workspace_session
):
    """Session edit to an in-session symlink or symlink parent is rejected as
    invalid request; no symlink traversal."""
    assert_ok(
        exec_command(
            sandbox,
            "mkdir realdir && printf old > realdir/inner.txt && ln -s realdir linkdir && ln -s realdir/inner.txt link.txt",
            workspace_session_id=workspace_session,
        )
    )
    assert_error(
        file_edit(
            sandbox,
            "link.txt",
            [{"old_string": "old", "new_string": "new"}],
            workspace_session_id=workspace_session,
        ),
        "invalid_request",
    )
    assert_error(
        file_edit(
            sandbox,
            "linkdir/inner.txt",
            [{"old_string": "old", "new_string": "new"}],
            workspace_session_id=workspace_session,
        ),
        "invalid_request",
    )
