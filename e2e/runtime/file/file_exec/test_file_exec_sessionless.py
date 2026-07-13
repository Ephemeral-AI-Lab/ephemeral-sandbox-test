"""Live e2e: File Ops + Exec Ops - Sessionless (19 cases)."""

import pytest

from runtime.file.helpers import (
    assert_blame_ranges,
    assert_blame_tiling,
    assert_content,
    assert_error,
    assert_manifest_delta,
    assert_ok,
    assert_single_owner,
    edit,
    exec_command,
    file_edit,
    file_read,
    file_write,
    layerstack,
    owners_by_line,
)
from harness.catalog.declarations import e2e_test


def _exec_ok(sandbox, command, **kwargs):
    kwargs.setdefault("yield_time_ms", 30_000)
    result = exec_command(sandbox, command, **kwargs)
    assert "error" not in result, result
    assert result["status"] == "ok", result
    assert result["exit_code"] == 0, result
    return result


def _assert_window(result, content, *, total_lines):
    assert_content(result, content)
    assert result["start_line"] == 1, result
    assert result["num_lines"] == total_lines, result
    assert result["total_lines"] == total_lines, result
    return result


@e2e_test(
    timeout_ms=3_000,
    id='phase0.dd58e5523fa91a024492dcfe',
    title='One Shot Exec Creates New File Then Sessionless Read And Blame',
    description='Validates the behavior exercised by One Shot Exec Creates New File Then Sessionless Read And Blame.',
    features=('runtime.file',),
    validations={'assert-one-shot-exec-creates-new-file-then-sessionless-read-and-blame': 'The assertions for one shot exec creates new file then sessionless read and blame hold.'},
    execution_surface='cli',
)
def test_one_shot_exec_creates_new_file_then_sessionless_read_and_blame(sandbox):
    """One-shot `exec_command` creates a new file
    (`printf 'alpha\nbeta' > exec/made.txt`), then sessionless `file_read` and
    `file_blame` inspect it.
    Expected: exec returns `status = ok`, `exit_code = 0`; the one-shot
    capture publishes on completion, so `file_read` returns
    `content = "alpha\nbeta"` with correct window fields, and `file_blame`
    shows a single range with owner `workspace_session:<one-shot id>`."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "mkdir -p exec && printf 'alpha\\nbeta' > exec/made.txt")
    assert_manifest_delta(sandbox, before, 1)
    _assert_window(file_read(sandbox, "exec/made.txt"), "alpha\nbeta", total_lines=2)
    assert_single_owner(sandbox, "exec/made.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.d81eaff73cc8b7fa2e01d70d',
    title='File Write Then One Shot Sed Reassigns Only Edited Line',
    description='Validates the behavior exercised by File Write Then One Shot Sed Reassigns Only Edited Line.',
    features=('runtime.file',),
    validations={'assert-file-write-then-one-shot-sed-reassigns-only-edited-line': 'The assertions for file write then one shot sed reassigns only edited line hold.'},
    execution_surface='cli',
)
def test_file_write_then_one_shot_sed_reassigns_only_edited_line(sandbox):
    """Sessionless `file_write` creates a 3-line file (`operation:<request A>`),
    then a one-shot `exec_command` runs `sed -i` replacing only line 2, then
    `file_read` + `file_blame`.
    Expected: `file_read` shows the sed result; `file_blame` shows line 2
    owned by `workspace_session:<one-shot id>` while lines 1 and 3 keep owner
    `operation:<request A>`."""
    before = layerstack(sandbox)
    path = "exec/sed.txt"
    assert_ok(file_write(sandbox, path, "line-1\nline-2\nline-3"))
    owner_a = assert_single_owner(sandbox, path, prefix="operation:")
    _exec_ok(sandbox, f"sed -i '2s/line-2/LINE-2/' {path}")
    assert_manifest_delta(sandbox, before, 2)
    assert_content(file_read(sandbox, path), "line-1\nLINE-2\nline-3")
    blame = assert_blame_tiling(sandbox, path)
    owner_b = owners_by_line(blame)[1]
    assert owner_b.startswith("workspace_session:"), blame
    assert_blame_ranges(
        sandbox,
        path,
        [(1, 1, owner_a), (2, 1, owner_b), (3, 1, owner_a)],
        blame,
    )


@e2e_test(
    timeout_ms=3_000,
    id='phase0.81b71c5c6b2225c3a017189d',
    title='One Shot Rm Publishes Delete Whiteout For File Ops',
    description='Validates the behavior exercised by One Shot Rm Publishes Delete Whiteout For File Ops.',
    features=('runtime.file',),
    validations={'assert-one-shot-rm-publishes-delete-whiteout-for-file-ops': 'The assertions for one shot rm publishes delete whiteout for file ops hold.'},
    execution_surface='cli',
)
def test_one_shot_rm_publishes_delete_whiteout_for_file_ops(sandbox):
    """Sessionless `file_write` publishes `victim.txt`; a one-shot `exec_command`
    runs `rm victim.txt` (exit 0); then sessionless `file_read` and
    `file_edit` target the path.
    Expected: the published `Delete` whiteout makes both `file_read` and
    `file_edit` return kind `not_found` (`file not found: victim.txt`), not
    stale content."""
    before = layerstack(sandbox)
    path = "victim.txt"
    assert_ok(file_write(sandbox, path, "doomed"))
    assert_single_owner(sandbox, path, prefix="operation:")
    _exec_ok(sandbox, f"rm {path}")
    assert_manifest_delta(sandbox, before, 2)
    assert_error(file_read(sandbox, path), "not_found", "file not found")
    assert_error(file_edit(sandbox, path, [edit("doomed", "saved")]), "not_found")


@e2e_test(
    timeout_ms=4_000,
    id='phase0.31f8cff318391a7d355f81b5',
    title='Rm Rf Parent Whiteout Then Recreate Child Path',
    description='Validates the behavior exercised by Rm Rf Parent Whiteout Then Recreate Child Path.',
    features=('runtime.file',),
    validations={'assert-rm-rf-parent-whiteout-then-recreate-child-path': 'The assertions for rm rf parent whiteout then recreate child path hold.'},
    execution_surface='cli',
)
def test_rm_rf_parent_whiteout_then_recreate_child_path(sandbox):
    """One-shot exec creates `reports/daily/r1.txt`, a second one-shot exec runs
    `rm -rf reports`, then sessionless `file_write` creates
    `reports/daily/r2.txt` and a third exec runs `cat reports/daily/r2.txt`.
    Expected: after the `rm -rf`, `file_read reports/daily/r1.txt` is
    `not_found` (parent hidden by whiteout, never resolved to the old object);
    the `file_write` returns `type = create` with parents recreated; the final
    exec exits 0 printing r2's content, and r1 stays `not_found`."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "mkdir -p reports/daily && printf r1 > reports/daily/r1.txt")
    _exec_ok(sandbox, "rm -rf reports")
    assert_error(file_read(sandbox, "reports/daily/r1.txt"), "not_found")
    result = assert_ok(file_write(sandbox, "reports/daily/r2.txt", "r2"))
    assert result["type"] == "create", result
    assert_single_owner(sandbox, "reports/daily/r2.txt", prefix="operation:")
    output = _exec_ok(sandbox, "cat reports/daily/r2.txt")
    assert "r2" in output["output"], output
    assert_error(file_read(sandbox, "reports/daily/r1.txt"), "not_found")
    assert_manifest_delta(sandbox, before, 3)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.acc9e70a0c8d0d0dacf20643',
    title='One Shot Mv Over Published File Reassigns New Path Blame',
    description='Validates the behavior exercised by One Shot Mv Over Published File Reassigns New Path Blame.',
    features=('runtime.file',),
    validations={'assert-one-shot-mv-over-published-file-reassigns-new-path-blame': 'The assertions for one shot mv over published file reassigns new path blame hold.'},
    execution_surface='cli',
)
def test_one_shot_mv_over_published_file_reassigns_new_path_blame(sandbox):
    """One-shot exec runs `mv old.txt new.txt` over a previously published file,
    then sessionless reads and blame on both paths.
    Expected: `file_read old.txt` -> `not_found`; `file_read new.txt` returns
    the moved content; `file_blame new.txt` attributes its lines to
    `workspace_session:<one-shot id>`."""
    before = layerstack(sandbox)
    assert_ok(file_write(sandbox, "old.txt", "moved\nbody"))
    assert_single_owner(sandbox, "old.txt", prefix="operation:")
    _exec_ok(sandbox, "mv old.txt new.txt")
    assert_manifest_delta(sandbox, before, 2)
    assert_error(file_read(sandbox, "old.txt"), "not_found")
    assert_content(file_read(sandbox, "new.txt"), "moved\nbody")
    assert_single_owner(sandbox, "new.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.e3d61f29614a8f333d7ccab3',
    title='File Write Script Then One Shot Exec Side Effect Is Published',
    description='Validates the behavior exercised by File Write Script Then One Shot Exec Side Effect Is Published.',
    features=('runtime.file',),
    validations={'assert-file-write-script-then-one-shot-exec-side-effect-is-published': 'The assertions for file write script then one shot exec side effect is published hold.'},
    execution_surface='cli',
)
def test_file_write_script_then_one_shot_exec_side_effect_is_published(sandbox):
    """Sessionless `file_write` creates `scripts/hello.sh` (echo + side-effect
    write to `out/result.txt`), then one one-shot `exec_command` runs
    `chmod +x scripts/hello.sh && ./scripts/hello.sh`.
    Expected: exec `status = ok`, `exit_code = 0` with the script's stdout in
    `output`; sessionless `file_read out/result.txt` returns the side-effect
    content written by the script."""
    before = layerstack(sandbox)
    script = "#!/bin/sh\necho hello\nmkdir -p out\nprintf result > out/result.txt\n"
    assert_ok(file_write(sandbox, "scripts/hello.sh", script))
    assert_single_owner(sandbox, "scripts/hello.sh", prefix="operation:")
    output = _exec_ok(sandbox, "chmod +x scripts/hello.sh && ./scripts/hello.sh")
    assert "hello" in output["output"], output
    assert_manifest_delta(sandbox, before, 2)
    assert_content(file_read(sandbox, "out/result.txt"), "result")
    assert_single_owner(sandbox, "out/result.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=4_000,
    id='phase0.e42cc19df900868e63039500',
    title='Executable Bit Survives One Shot Capture And Projection',
    description='Validates the behavior exercised by Executable Bit Survives One Shot Capture And Projection.',
    features=('runtime.file',),
    validations={'assert-executable-bit-survives-one-shot-capture-and-projection': 'The assertions for executable bit survives one shot capture and projection hold.'},
    execution_surface='cli',
)
def test_executable_bit_survives_one_shot_capture_and_projection(sandbox):
    """One-shot exec creates an executable in one command
    (`printf '#!/bin/sh\necho tool-v1' > tool.sh && chmod +x tool.sh`); then
    sessionless `file_read tool.sh` and a second one-shot exec runs
    `./tool.sh`.
    Expected: `file_read` returns the script source; the executable bit
    survives capture/publish/projection, so the second exec returns
    `exit_code = 0` with `tool-v1` in `output`."""
    before = layerstack(sandbox)
    source = "#!/bin/sh\necho tool-v1"
    _exec_ok(sandbox, "printf '#!/bin/sh\\necho tool-v1' > tool.sh && chmod +x tool.sh")
    assert_manifest_delta(sandbox, before, 1)
    assert_content(file_read(sandbox, "tool.sh"), source)
    assert_single_owner(sandbox, "tool.sh", prefix="workspace_session:")
    output = _exec_ok(sandbox, "./tool.sh")
    assert "tool-v1" in output["output"], output
    assert_manifest_delta(sandbox, before, 1)


@e2e_test(
    timeout_ms=3_000,
    id='phase0.27127b15536c27a7abf880d1',
    title='Published Symlink File Is Not Followed By File Ops',
    description='Validates the behavior exercised by Published Symlink File Is Not Followed By File Ops.',
    features=('runtime.file',),
    validations={'assert-published-symlink-file-is-not-followed-by-file-ops': 'The assertions for published symlink file is not followed by file ops hold.'},
    execution_surface='cli',
)
def test_published_symlink_file_is_not_followed_by_file_ops(sandbox):
    """One-shot exec creates `ln -s real.txt link.txt` next to a published
    `real.txt`; then sessionless `file_read link.txt` and
    `file_write link.txt`.
    Expected: the symlink is published as a symlink entry; both operations
    fail `invalid_request` with `path is not a regular file (Symlink)`; the
    symlink is not followed and `file_read real.txt` still returns the
    original content."""
    before = layerstack(sandbox)
    assert_ok(file_write(sandbox, "real.txt", "real"))
    assert_single_owner(sandbox, "real.txt", prefix="operation:")
    _exec_ok(sandbox, "ln -s real.txt link.txt")
    assert_manifest_delta(sandbox, before, 2)
    assert_error(file_read(sandbox, "link.txt"), "invalid_request")
    assert_error(file_write(sandbox, "link.txt", "bad"), "invalid_request")
    assert_content(file_read(sandbox, "real.txt"), "real")
    assert_single_owner(sandbox, "real.txt", prefix="operation:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.c29e57384e1a51137c2a4448',
    title='Published Symlink Parent Is Not Traversed By File Ops',
    description='Validates the behavior exercised by Published Symlink Parent Is Not Traversed By File Ops.',
    features=('runtime.file',),
    validations={'assert-published-symlink-parent-is-not-traversed-by-file-ops': 'The assertions for published symlink parent is not traversed by file ops hold.'},
    execution_surface='cli',
)
def test_published_symlink_parent_is_not_traversed_by_file_ops(sandbox):
    """One-shot exec creates a symlinked directory
    (`mkdir realdir; printf x > realdir/inner.txt; ln -s realdir linkdir`);
    then sessionless `file_read linkdir/inner.txt` and
    `file_write linkdir/new.txt`.
    Expected: both are rejected as `invalid_request` (symlink parent; no
    parent-symlink traversal); `file_read realdir/inner.txt` through the real
    parent succeeds."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "mkdir realdir && printf x > realdir/inner.txt && ln -s realdir linkdir")
    assert_manifest_delta(sandbox, before, 1)
    assert_error(file_read(sandbox, "linkdir/inner.txt"), "invalid_request")
    assert_error(file_write(sandbox, "linkdir/new.txt", "bad"), "invalid_request")
    assert_content(file_read(sandbox, "realdir/inner.txt"), "x")
    assert_single_owner(sandbox, "realdir/inner.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.24d97dc6cdabe0dd3403a289',
    title='One Shot Fifo Is Protected Drop But Regular Note Publishes',
    description='Validates the behavior exercised by One Shot Fifo Is Protected Drop But Regular Note Publishes.',
    features=('runtime.file',),
    validations={'assert-one-shot-fifo-is-protected-drop-but-regular-note-publishes': 'The assertions for one shot fifo is protected drop but regular note publishes hold.'},
    execution_surface='cli',
)
def test_one_shot_fifo_is_protected_drop_but_regular_note_publishes(sandbox):
    """One-shot exec runs `mkfifo pipe.fifo && printf ok > note.txt`; then
    sessionless `file_read` of both paths.
    Expected: exec `exit_code = 0`; capture drops the FIFO as a protected drop
    (`unsupported_special_file`) so `file_read pipe.fifo` -> `not_found`, while
    `note.txt` is published and readable - the special file never reaches the
    layerstack."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "mkfifo pipe.fifo && printf ok > note.txt")
    assert_manifest_delta(sandbox, before, 1)
    assert_error(file_read(sandbox, "pipe.fifo"), "not_found")
    assert_content(file_read(sandbox, "note.txt"), "ok")
    assert_single_owner(sandbox, "note.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.5fbd1a4b9ddbd273e2fb74cd',
    title='Exec Written Bom And Crlf Are Normalized On Read',
    description='Validates the behavior exercised by Exec Written Bom And Crlf Are Normalized On Read.',
    features=('runtime.file',),
    validations={'assert-exec-written-bom-and-crlf-are-normalized-on-read': 'The assertions for exec written bom and crlf are normalized on read hold.'},
    execution_surface='cli',
)
def test_exec_written_bom_and_crlf_are_normalized_on_read(sandbox):
    """One-shot exec writes a file with a UTF-8 BOM and CRLF endings
    (`printf '\xef\xbb\xbfline1\r\nline2\r\n' > mixed.txt`); then sessionless
    `file_read mixed.txt`.
    Expected: BOM removed and CRLF normalized before windowing -
    `content = "line1\nline2"`, `start_line = 1`, `total_lines = 2`, no `\r`
    bytes in content."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "printf '\\357\\273\\277line1\\r\\nline2\\r\\n' > mixed.txt")
    assert_manifest_delta(sandbox, before, 1)
    result = _assert_window(file_read(sandbox, "mixed.txt"), "line1\nline2", total_lines=2)
    assert "\r" not in result["content"], result
    assert_single_owner(sandbox, "mixed.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.689f63ebd122510fc2cf9e1e',
    title='Non Utf8 Exec File Is Rejected By Read And Edit',
    description='Validates the behavior exercised by Non Utf8 Exec File Is Rejected By Read And Edit.',
    features=('runtime.file',),
    validations={'assert-non-utf8-exec-file-is-rejected-by-read-and-edit': 'The assertions for non utf8 exec file is rejected by read and edit hold.'},
    execution_surface='cli',
)
def test_non_utf8_exec_file_is_rejected_by_read_and_edit(sandbox):
    """One-shot exec writes non-UTF-8 bytes
    (`head -c 64 /dev/urandom > blob.bin`); then sessionless
    `file_read blob.bin` and `file_edit blob.bin`.
    Expected: both fail `invalid_request` with
    `file is not valid UTF-8: blob.bin`; neither returns partial bytes."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "printf '\\377\\376\\375bad' > blob.bin")
    assert_manifest_delta(sandbox, before, 1)
    assert_error(file_read(sandbox, "blob.bin"), "invalid_request", "not valid UTF-8")
    assert_error(
        file_edit(sandbox, "blob.bin", [edit("bad", "good")]),
        "invalid_request",
        "not valid UTF-8",
    )


@e2e_test(
    timeout_ms=3_000,
    id='phase0.79b0434b3eea5dd53bb24d20',
    title='One Shot Exec Consumes Published File Write Content',
    description='Validates the behavior exercised by One Shot Exec Consumes Published File Write Content.',
    features=('runtime.file',),
    validations={'assert-one-shot-exec-consumes-published-file-write-content': 'The assertions for one shot exec consumes published file write content hold.'},
    execution_surface='cli',
)
def test_one_shot_exec_consumes_published_file_write_content(sandbox):
    """Sessionless `file_write` creates a 10-line `data/data.csv`, then a
    one-shot exec runs `wc -l < data/data.csv && grep -c ',' data/data.csv`.
    Expected: exec `status = ok`, `exit_code = 0`, `output` shows the expected
    line and match counts - the exec consumes exactly the published
    `file_write` content."""
    before = layerstack(sandbox)
    content = "\n".join(f"{index},value-{index}" for index in range(1, 11)) + "\n"
    assert_ok(file_write(sandbox, "data/data.csv", content))
    assert_single_owner(sandbox, "data/data.csv", prefix="operation:")
    output = _exec_ok(sandbox, "wc -l < data/data.csv && grep -c ',' data/data.csv")
    assert "10\n10" in output["output"], output
    assert_manifest_delta(sandbox, before, 1)


@e2e_test(
    timeout_ms=11_000,
    id='phase0.edb82e8b41f03fcb61327dcb',
    title='Complex Long Sessionless Interleave Exec Append Then File Edit',
    description='Validates the behavior exercised by Complex Long Sessionless Interleave Exec Append Then File Edit.',
    features=('runtime.file',),
    validations={'assert-complex-long-sessionless-interleave-exec-append-then-file-edit': 'The assertions for complex long sessionless interleave exec append then file edit hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_long_sessionless_interleave_exec_append_then_file_edit(sandbox):
    """[complex] Long sessionless interleave: 12 rounds where round *i* runs a
    one-shot exec appending `exec-i` to `journal.txt` followed by a
    sessionless `file_edit` rewriting that line to `edited-i`; finish with a
    one-shot exec `cat journal.txt`.
    Expected: every round's exec sees all prior published rounds (layer
    ordering); final `file_read` and the exec `output` both show the 12
    `edited-i` lines in order; `file_blame` shows each line owned by its
    round's `operation:<edit request_id>` (no `workspace_session:` owners
    survive on edited lines)."""
    before = layerstack(sandbox)
    path = "journal.txt"
    lines = []
    owners = []

    for index in range(1, 13):
        _exec_ok(sandbox, f"printf 'exec-{index}\\n' >> {path}")
        lines.append(f"exec-{index}")
        assert_content(file_read(sandbox, path), "\n".join(lines))
        assert_ok(file_edit(sandbox, path, [edit(f"exec-{index}", f"edited-{index}")]))
        lines[-1] = f"edited-{index}"
        owner = owners_by_line(assert_blame_tiling(sandbox, path))[index - 1]
        assert owner.startswith("operation:"), owner
        owners.append(owner)

    expected = "\n".join(lines)
    assert_content(file_read(sandbox, path), expected)
    output = _exec_ok(sandbox, f"cat {path}")
    assert expected in output["output"], output
    assert owners_by_line(assert_blame_tiling(sandbox, path)) == owners
    assert all(owner.startswith("operation:") for owner in owners)
    assert_manifest_delta(sandbox, before, 24)


@e2e_test(
    timeout_ms=6_000,
    id='phase0.8ce0c28fcb5e98d37428d81e',
    title='Complex Tar Pack And Extract Exec Generated Tree',
    description='Validates the behavior exercised by Complex Tar Pack And Extract Exec Generated Tree.',
    features=('runtime.file',),
    validations={'assert-complex-tar-pack-and-extract-exec-generated-tree': 'The assertions for complex tar pack and extract exec generated tree hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_tar_pack_and_extract_exec_generated_tree(sandbox):
    """[complex] One-shot exec generates 400 files (`src/f001.txt`...`f400.txt`,
    each with a unique known body) and packs `tar czf bundle/src.tgz src`; a
    second one-shot exec extracts it to `unpacked/` (`tar xzf`).
    Expected: both execs `exit_code = 0`; sampled sessionless `file_read`s of
    `unpacked/src/f001.txt`, `f200.txt`, `f400.txt` return the exact bodies;
    `file_blame` on a sampled file shows one range owned by the extracting
    exec's `workspace_session:<id>`; a missing index like `f401.txt` is
    `not_found`."""
    before = layerstack(sandbox)
    _exec_ok(
        sandbox,
        "mkdir -p src bundle && "
        "for i in $(seq -w 1 400); do printf 'body-%s\\n' \"$i\" > src/f$i.txt; done && "
        "tar czf bundle/src.tgz src",
        timeout=240,
    )
    _exec_ok(sandbox, "mkdir -p unpacked && tar xzf bundle/src.tgz -C unpacked", timeout=240)
    assert_manifest_delta(sandbox, before, 2)

    for index in ("001", "200", "400"):
        path = f"unpacked/src/f{index}.txt"
        assert_content(file_read(sandbox, path), f"body-{index}")
    assert_single_owner(sandbox, "unpacked/src/f001.txt", prefix="workspace_session:")
    assert_error(file_read(sandbox, "unpacked/src/f401.txt"), "not_found")


@e2e_test(
    timeout_ms=25_000,
    id='phase0.958c5cf15d5881fb1b61c322',
    title='Complex Two Hundred Parts Then Exec Build Concatenates All',
    description='Validates the behavior exercised by Complex Two Hundred Parts Then Exec Build Concatenates All.',
    features=('runtime.file',),
    validations={'assert-complex-two-hundred-parts-then-exec-build-concatenates-all': 'The assertions for complex two hundred parts then exec build concatenates all hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_two_hundred_parts_then_exec_build_concatenates_all(sandbox):
    """[complex] 200 sessionless `file_write`s create
    `parts/part_001.txt`...`part_200.txt` (2 known lines each) plus a `build.sh`
    that concatenates and validates them; one-shot exec runs `sh build.sh`.
    Expected: exec `status = ok`, `exit_code = 0`, `output` reports the
    expected `400` total lines / PASS marker; sessionless `file_read` of the
    exec-produced `all.txt` returns the concatenation, and
    `file_blame all.txt` shows owner `workspace_session:<one-shot id>`."""
    before = layerstack(sandbox)
    expected_lines = []
    for index in range(1, 201):
        body = f"part-{index:03d}-a\npart-{index:03d}-b\n"
        expected_lines.extend(body.splitlines())
        assert_ok(file_write(sandbox, f"parts/part_{index:03d}.txt", body))

    script = (
        "#!/bin/sh\n"
        "set -eu\n"
        "cat parts/part_*.txt > all.txt\n"
        "lines=$(wc -l < all.txt | tr -d ' ')\n"
        "[ \"$lines\" = \"400\" ]\n"
        "printf '400 PASS\\n'\n"
    )
    assert_ok(file_write(sandbox, "build.sh", script))
    output = _exec_ok(sandbox, "sh build.sh", timeout=240)
    assert "400 PASS" in output["output"], output
    assert_manifest_delta(sandbox, before, 202)
    assert_content(file_read(sandbox, "all.txt"), "\n".join(expected_lines))
    assert_single_owner(sandbox, "all.txt", prefix="workspace_session:")


@e2e_test(
    timeout_ms=3_000,
    id='phase0.a38f0995804dfabb59437495',
    title='Complex Multimeg Exec File Supports Windowed Reads',
    description='Validates the behavior exercised by Complex Multimeg Exec File Supports Windowed Reads.',
    features=('runtime.file',),
    validations={'assert-complex-multimeg-exec-file-supports-windowed-reads': 'The assertions for complex multimeg exec file supports windowed reads hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_multimeg_exec_file_supports_windowed_reads(sandbox):
    """[complex] One-shot exec generates a multi-MB file
    (`seq 1 500000 > big/seq.txt`, ~3.4 MB); then windowed sessionless reads.
    Expected: `file_read --offset 250000 --limit 100` returns
    `start_line = 250000`, `num_lines = 100`, lines `250000..250099`,
    `total_lines = 500000`, `truncated = true`, `next_offset = 250100`;
    `--offset 600000` (past EOF) returns an empty content window with correct
    totals, not `not_found`; the read never fails merely because the whole
    file is large."""
    before = layerstack(sandbox)
    _exec_ok(
        sandbox,
        "mkdir -p big && seq 1 500000 > big/seq.txt",
        timeout=240,
        yield_time_ms=120_000,
    )
    assert_manifest_delta(sandbox, before, 1)
    window = assert_ok(file_read(sandbox, "big/seq.txt", offset=250000, limit=100))
    assert window["start_line"] == 250000, window
    assert window["num_lines"] == 100, window
    assert window["total_lines"] == 500000, window
    assert window["truncated"] is True, window
    assert window["next_offset"] == 250100, window
    assert window["content"].splitlines() == [str(i) for i in range(250000, 250100)]

    past = assert_ok(file_read(sandbox, "big/seq.txt", offset=600000, limit=100))
    assert past["content"] == "", past
    assert past["num_lines"] == 0, past
    assert past["total_lines"] == 500000, past


@e2e_test(
    timeout_ms=3_000,
    id='phase0.b5349b9fed3f38e9bfbdb9a1',
    title='Complex Single Wide Line Fails Read But File Published',
    description='Validates the behavior exercised by Complex Single Wide Line Fails Read But File Published.',
    features=('runtime.file',),
    validations={'assert-complex-single-wide-line-fails-read-but-file-published': 'The assertions for complex single wide line fails read but file published hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_single_wide_line_fails_read_but_file_published(sandbox):
    """[complex] One-shot exec generates a single ~300 KB line
    (`head -c 300000 /dev/zero | tr '\0' x > wide.txt`); then sessionless
    `file_read wide.txt` and a follow-up one-shot exec `wc -c wide.txt`.
    Expected: `file_read` fails `invalid_request` with
    `selected read output exceeds the maximum of 262144 bytes`
    (`OutputTooLarge`, not `FileTooLarge`); the follow-up exec exits 0
    reporting 300000 bytes, proving the file itself published intact."""
    before = layerstack(sandbox)
    _exec_ok(sandbox, "head -c 300000 /dev/zero | tr '\\0' x > wide.txt", timeout=240)
    assert_manifest_delta(sandbox, before, 1)
    assert_error(
        file_read(sandbox, "wide.txt"),
        "invalid_request",
        "selected read output exceeds the maximum of 262144 bytes",
    )
    output = _exec_ok(sandbox, "wc -c < wide.txt")
    assert "300000" in output["output"], output


@e2e_test(
    timeout_ms=3_000,
    id='phase0.5f0c93e7586efe0e47e80867',
    title='Complex Large Text File Rejects Edit But Allows Small Read Window',
    description='Validates the behavior exercised by Complex Large Text File Rejects Edit But Allows Small Read Window.',
    features=('runtime.file',),
    validations={'assert-complex-large-text-file-rejects-edit-but-allows-small-read-window': 'The assertions for complex large text file rejects edit but allows small read window hold.'},
    execution_surface='cli',
)
@pytest.mark.slow
def test_complex_large_text_file_rejects_edit_but_allows_small_read_window(sandbox):
    """[complex] One-shot exec generates a >4 MiB text file
    (`yes padding-line | head -n 500000 > big/pad.txt`); then sessionless
    `file_edit` on it and a small windowed `file_read`.
    Expected: `file_edit` fails `invalid_request` with `file is too large`
    (`MAX_EDIT_BYTES` = 4 MiB, rejected before transform, no layer committed);
    `file_read --offset 1 --limit 5` on the same file still succeeds with
    `total_lines = 500000`."""
    before = layerstack(sandbox)
    _exec_ok(
        sandbox,
        "mkdir -p big && yes padding-line | head -n 500000 > big/pad.txt",
        timeout=240,
        yield_time_ms=120_000,
    )
    after_create = assert_manifest_delta(sandbox, before, 1)
    assert_error(
        file_edit(sandbox, "big/pad.txt", [edit("padding-line", "changed")], timeout=240),
        "invalid_request",
        "file is too large",
    )
    after_failed_edit = layerstack(sandbox)
    assert after_failed_edit["manifest_version"] == after_create["manifest_version"]
    window = assert_ok(file_read(sandbox, "big/pad.txt", offset=1, limit=5))
    assert window["content"].splitlines() == ["padding-line"] * 5, window
    assert window["total_lines"] == 500000, window
