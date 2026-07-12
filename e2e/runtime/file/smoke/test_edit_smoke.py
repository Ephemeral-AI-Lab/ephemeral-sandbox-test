"""Live e2e: Edit Smoke (5 cases)."""

from runtime.file.helpers import (
    assert_blame_owners,
    assert_content,
    assert_error,
    assert_ok,
    assert_single_owner,
    edit,
    file_edit,
    file_read,
    file_write,
    owner_for_line,
    workspace_session,
)
from harness.catalog.declarations import e2e_test


@e2e_test(
    id='phase0.3eee2226168fce0492dff17b',
    title='Sessionless Edit Unique Replacement And Read Sees Result',
    description='Validates the behavior exercised by Sessionless Edit Unique Replacement And Read Sees Result.',
    features=('runtime.file',),
    validations={'assert-sessionless-edit-unique-replacement-and-read-sees-result': 'The assertions for sessionless edit unique replacement and read sees result hold.'},
    execution_surface='cli',
)
def test_sessionless_edit_unique_replacement_and_read_sees_result(sandbox):
    """Sessionless edit performs one unique replacement and sessionless read sees
    the result."""
    assert_ok(file_write(sandbox, "edit-smoke/unique.txt", "alpha\nbeta"))
    seed_owner = owner_for_line(sandbox, "edit-smoke/unique.txt", 1)
    result = file_edit(
        sandbox, "edit-smoke/unique.txt", [edit("beta", "BETA")]
    )
    assert result["type"] == "edit"
    assert result["replacements"] == 1
    assert_content(file_read(sandbox, "edit-smoke/unique.txt"), "alpha\nBETA")
    edit_owner = owner_for_line(sandbox, "edit-smoke/unique.txt", 2)
    assert edit_owner.startswith("operation:")
    assert_blame_owners(sandbox, "edit-smoke/unique.txt", [seed_owner, edit_owner])


@e2e_test(
    id='phase0.916c2c6804959838db383059',
    title='Sessionless Edit Replace All True Replaces Multiple Occurrences',
    description='Validates the behavior exercised by Sessionless Edit Replace All True Replaces Multiple Occurrences.',
    features=('runtime.file',),
    validations={'assert-sessionless-edit-replace-all-true-replaces-multiple-occurrences': 'The assertions for sessionless edit replace all true replaces multiple occurrences hold.'},
    execution_surface='cli',
)
def test_sessionless_edit_replace_all_true_replaces_multiple_occurrences(sandbox):
    """Sessionless edit with `replace_all=true` replaces multiple occurrences."""
    assert_ok(file_write(sandbox, "edit-smoke/all.txt", "a b a"))
    result = file_edit(
        sandbox,
        "edit-smoke/all.txt",
        [edit("a", "A", replace_all=True)],
    )
    assert result["replacements"] == 2
    assert_content(file_read(sandbox, "edit-smoke/all.txt"), "A b A")
    assert_single_owner(sandbox, "edit-smoke/all.txt", prefix="operation:")


@e2e_test(
    id='phase0.fa8669a38d4df8a3305571ba',
    title='Sessionless Edit Missing Old String Returns Edit Not Found',
    description='Validates the behavior exercised by Sessionless Edit Missing Old String Returns Edit Not Found.',
    features=('runtime.file',),
    validations={'assert-sessionless-edit-missing-old-string-returns-edit-not-found': 'The assertions for sessionless edit missing old string returns edit not found hold.'},
    execution_surface='cli',
)
def test_sessionless_edit_missing_old_string_returns_edit_not_found(sandbox):
    """Sessionless edit with missing `old_string` returns edit-not-found."""
    assert_ok(file_write(sandbox, "edit-smoke/missing-old.txt", "alpha"))
    assert_error(
        file_edit(sandbox, "edit-smoke/missing-old.txt", [edit("beta", "BETA")]),
        "invalid_request",
    )


@e2e_test(
    id='phase0.7aef434316349c5a3b815864',
    title='Session Edit Visible With Workspace Session Id And Invisible Sessionless',
    description='Validates the behavior exercised by Session Edit Visible With Workspace Session Id And Invisible Sessionless.',
    features=('runtime.file',),
    validations={'assert-session-edit-visible-with-workspace-session-id-and-invisible-sessionless': 'The assertions for session edit visible with workspace session id and invisible sessionless hold.'},
    execution_surface='cli',
)
def test_session_edit_visible_with_workspace_session_id_and_invisible_sessionless(
    sandbox, workspace_session
):
    """Session edit is visible with `workspace_session_id` and invisible to
    sessionless read before capture."""
    assert_ok(
        file_write(
            sandbox,
            "edit-smoke/session.txt",
            "draft alpha",
            workspace_session_id=workspace_session,
        )
    )
    result = file_edit(
        sandbox,
        "edit-smoke/session.txt",
        [edit("alpha", "beta")],
        workspace_session_id=workspace_session,
    )
    assert result["replacements"] == 1
    assert_content(
        file_read(sandbox, "edit-smoke/session.txt", workspace_session_id=workspace_session),
        "draft beta",
    )
    assert_error(file_read(sandbox, "edit-smoke/session.txt"), "not_found")


@e2e_test(
    id='phase0.267061494614dfa4948295f6',
    title='Ordered Multi Edit Applies Against Evolving Content',
    description='Validates the behavior exercised by Ordered Multi Edit Applies Against Evolving Content.',
    features=('runtime.file',),
    validations={'assert-ordered-multi-edit-applies-against-evolving-content': 'The assertions for ordered multi edit applies against evolving content hold.'},
    execution_surface='cli',
)
def test_ordered_multi_edit_applies_against_evolving_content(sandbox):
    """Ordered multi-edit applies against evolving content."""
    assert_ok(file_write(sandbox, "edit-smoke/ordered.txt", "one"))
    result = file_edit(
        sandbox,
        "edit-smoke/ordered.txt",
        [edit("one", "two"), edit("two", "three")],
    )
    assert result["edits_applied"] == 2
    assert result["replacements"] == 2
    assert_content(file_read(sandbox, "edit-smoke/ordered.txt"), "three")
    assert_single_owner(sandbox, "edit-smoke/ordered.txt", prefix="operation:")
