"""Offline contracts for public CLI response sanitization."""

from harness.catalog.declarations import e2e_test
from harness.runner.cli import redact_value


@e2e_test(
    timeout_ms=1_000,
    id="harness.runner.cli.redaction-contract",
    title="CLI redaction preserves non-secret authority fields",
    description=(
        "Credential keys remain redacted without hiding LayerStack read and write "
        "authority observations."
    ),
    features=(),
    validations={
        "redaction-boundary": (
            "Authority fields stay observable while auth, authorization, token, "
            "password, secret, and credential keys are redacted."
        )
    },
)
def test_redaction_preserves_authority_and_hides_credentials():
    value = {
        "write_authority": "legacy_v1",
        "read_authority": "legacy_v1",
        "auth": "private-auth",
        "auth_token": "private-auth-token",
        "authorization": "Bearer private",
        "api_token": "private-api-token",
        "password": "private-password",
        "client_secret": "private-secret",
        "credential": "private-credential",
    }

    redacted = redact_value(value)

    assert redacted["write_authority"] == "legacy_v1"
    assert redacted["read_authority"] == "legacy_v1"
    assert all(
        redacted[key] == "[REDACTED]"
        for key in value
        if key not in {"write_authority", "read_authority"}
    )
