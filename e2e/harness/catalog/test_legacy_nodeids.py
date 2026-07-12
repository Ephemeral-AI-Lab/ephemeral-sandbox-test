from harness.catalog.declarations import e2e_test
from harness.catalog.mode import _legacy_nodeid


@e2e_test(
    id="harness.catalog.legacy-nodeid",
    title="Preserve a stable ID across a canonical path move",
    description="The stable-ID ledger remains authoritative when a runtime test moves into its family.",
    validations={"legacy-nodeid": "The moved node maps to its frozen ledger node."},
)
def test_squash_remount_move_preserves_ledger_identity():
    assert _legacy_nodeid(
        "runtime/workspace_session/test_squash_remount.py::test_boot_gate_enables_live_remount"
    ) == "runtime/test_squash_remount.py::test_boot_gate_enables_live_remount"
