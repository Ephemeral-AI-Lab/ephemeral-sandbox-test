"""Live e2e: Read Smoke (5 cases)."""

from runtime.file.helpers import (
    assert_content,
    assert_error,
    assert_ok,
    file_read,
    file_write,
    workspace_session,
)
from harness.catalog.declarations import e2e_test


@e2e_test(
    timeout_ms=3_000,
    id='phase0.a9af17e774634e939a9d463c',
    title='Sessionless Read Of File Created By Sessionless File Write',
    description='Validates the behavior exercised by Sessionless Read Of File Created By Sessionless File Write.',
    features=('runtime.file',),
    validations={'assert-sessionless-read-of-file-created-by-sessionless-file-write': 'The assertions for sessionless read of file created by sessionless file write hold.'},
    execution_surface='cli',
)
def test_sessionless_read_of_file_created_by_sessionless_file_write(sandbox):
    """Sessionless read of a file created by sessionless `file_write`."""
    result = file_write(sandbox, "read-smoke/sessionless.txt", "alpha\nbeta")
    assert_ok(result)

    read = file_read(sandbox, "read-smoke/sessionless.txt")
    assert_content(read, "alpha\nbeta")
    assert read["start_line"] == 1
    assert read["num_lines"] == 2
    assert read["total_lines"] == 2


@e2e_test(
    timeout_ms=3_000,
    id='phase0.b180a5054762d6df30da21ca',
    title='Session Read Of File Created By Session File Write',
    description='Validates the behavior exercised by Session Read Of File Created By Session File Write.',
    features=('runtime.file',),
    validations={'assert-session-read-of-file-created-by-session-file-write': 'The assertions for session read of file created by session file write hold.'},
    execution_surface='cli',
)
def test_session_read_of_file_created_by_session_file_write(sandbox, workspace_session):
    """Session read of a file created by session `file_write`."""
    result = file_write(
        sandbox,
        "read-smoke/session.txt",
        "session-only",
        workspace_session_id=workspace_session,
    )
    assert_ok(result)

    read = file_read(
        sandbox, "read-smoke/session.txt", workspace_session_id=workspace_session
    )
    assert_content(read, "session-only")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.4e125cfdc2c3209841ffdf8c',
    title='Sessionless Read With Offset And Limit Over Multiline File',
    description='Validates the behavior exercised by Sessionless Read With Offset And Limit Over Multiline File.',
    features=('runtime.file',),
    validations={'assert-sessionless-read-with-offset-and-limit-over-multiline-file': 'The assertions for sessionless read with offset and limit over multiline file hold.'},
    execution_surface='cli',
)
def test_sessionless_read_with_offset_and_limit_over_multiline_file(sandbox):
    """Sessionless read with `offset` and `limit` over a multi-line file."""
    content = "\n".join(f"line-{index}" for index in range(1, 8))
    assert_ok(file_write(sandbox, "read-smoke/window.txt", content))

    read = file_read(sandbox, "read-smoke/window.txt", offset=3, limit=2)
    assert_content(read, "line-3\nline-4")
    assert read["start_line"] == 3
    assert read["num_lines"] == 2
    assert read["total_lines"] == 7
    assert read["next_offset"] == 5
    assert read["truncated"] is True


@e2e_test(
    timeout_ms=2_000,
    id='phase0.43e92c6cd6d6bdbdd8526300',
    title='Sessionless Read Of Missing File Returns Not Found',
    description='Validates the behavior exercised by Sessionless Read Of Missing File Returns Not Found.',
    features=('runtime.file',),
    validations={'assert-sessionless-read-of-missing-file-returns-not-found': 'The assertions for sessionless read of missing file returns not found hold.'},
    execution_surface='cli',
)
def test_sessionless_read_of_missing_file_returns_not_found(sandbox):
    """Sessionless read of a missing file returns `not_found`."""
    assert_error(file_read(sandbox, "read-smoke/missing.txt"), "not_found")


@e2e_test(
    timeout_ms=2_000,
    id='phase0.45eee852ba27e44e2652b823',
    title='Sessionless Read Rejects Absolute Path Outside Workspace Root',
    description='Validates the behavior exercised by Sessionless Read Rejects Absolute Path Outside Workspace Root.',
    features=('runtime.file',),
    validations={'assert-sessionless-read-rejects-absolute-path-outside-workspace-root': 'The assertions for sessionless read rejects absolute path outside workspace root hold.'},
    execution_surface='cli',
)
def test_sessionless_read_rejects_absolute_path_outside_workspace_root(sandbox):
    """Sessionless read rejects an absolute path outside the workspace root."""
    assert_error(file_read(sandbox, "/tmp/outside-workspace.txt"), "invalid_request")
