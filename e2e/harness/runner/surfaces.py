"""Fixed execution-boundary adapters and immutable surface attestations.

Adapters deliberately expose one named boundary each.  A driver failure is a
failure of that boundary; it never falls back to a more convenient transport.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping


SURFACES = frozenset(
    {
        "cli",
        "console_rpc",
        "console_http_proxy",
        "gateway_rpc",
        "daemon_http",
        "direct_daemon_rpc",
    }
)


class SurfaceError(RuntimeError):
    """A named execution surface could not prove its actual boundary."""


@dataclass(frozen=True)
class SurfaceProof:
    expected: str
    observed: str
    driver: str
    boundary: str
    dispatch_outcome: str
    duration_ms: float
    product_digest: str | None = None
    evidence: Mapping[str, Any] | None = None

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "expected_surface": self.expected,
            "observed_surface": self.observed,
            "driver": self.driver,
            "boundary": self.boundary,
            "dispatch_outcome": self.dispatch_outcome,
            "duration_ms": self.duration_ms,
            "product_digest": self.product_digest,
            "evidence": dict(self.evidence or {}),
        }


class SurfaceAdapter:
    """One immutable surface name plus one injected real-boundary transport."""

    def __init__(
        self,
        surface: str,
        transport: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        product_digest: str | None = None,
    ) -> None:
        if surface not in SURFACES:
            raise SurfaceError(f"unsupported execution surface: {surface}")
        self.surface = surface
        self._transport = transport
        self.product_digest = product_digest

    def dispatch(self, request: Mapping[str, Any]) -> SurfaceProof:
        """Dispatch exactly once and require a matching, non-duplicated proof."""

        started = time.monotonic_ns()
        try:
            response = self._transport(dict(request))
        except BaseException as error:
            return SurfaceProof(
                expected=self.surface,
                observed="unavailable",
                driver=_DRIVER[self.surface],
                boundary=_BOUNDARY[self.surface],
                dispatch_outcome="transport_error",
                duration_ms=(time.monotonic_ns() - started) / 1_000_000,
                product_digest=self.product_digest,
                evidence={"error_type": type(error).__name__},
            )
        if not isinstance(response, Mapping):
            raise SurfaceError("surface driver returned an invalid proof record")
        observed = response.get("observed_surface")
        if observed != self.surface:
            raise SurfaceError(
                f"surface proof mismatch: expected {self.surface}, observed {observed!r}"
            )
        if response.get("proof_count", 1) != 1:
            raise SurfaceError("surface proof must contain exactly one boundary attestation")
        outcome = response.get("dispatch_outcome")
        if outcome not in {"succeeded", "operation_error", "transport_error"}:
            raise SurfaceError("surface proof has an unsupported dispatch outcome")
        return SurfaceProof(
            expected=self.surface,
            observed=observed,
            driver=_DRIVER[self.surface],
            boundary=_BOUNDARY[self.surface],
            dispatch_outcome=outcome,
            duration_ms=(time.monotonic_ns() - started) / 1_000_000,
            product_digest=self.product_digest,
            evidence=dict(response.get("evidence") or {}),
        )


def adapter_for(
    surface: str,
    transport: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    product_digest: str | None = None,
) -> SurfaceAdapter:
    """Construct one explicit adapter; callers cannot request a fallback chain."""

    return SurfaceAdapter(surface, transport, product_digest=product_digest)


def successful_surface_proof(
    surface: str,
    *,
    duration_ms: float = 0.0,
    product_digest: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one proof only after a real named boundary was observed."""

    if surface not in SURFACES:
        raise SurfaceError(f"unsupported execution surface: {surface}")
    return SurfaceProof(
        expected=surface,
        observed=surface,
        driver=_DRIVER[surface],
        boundary=_BOUNDARY[surface],
        dispatch_outcome="succeeded",
        duration_ms=max(0.0, float(duration_ms)),
        product_digest=product_digest,
        evidence=dict(evidence or {}),
    ).as_event_payload()


_DRIVER = {
    "cli": "subprocess",
    "console_rpc": "http_sse",
    "console_http_proxy": "http",
    "gateway_rpc": "gateway_probe",
    "daemon_http": "stdlib_http",
    "direct_daemon_rpc": "daemon_probe",
}
_BOUNDARY = {
    "cli": "product_cli_subprocess",
    "console_rpc": "console_post_api_rpc",
    "console_http_proxy": "console_daemon_http_proxy",
    "gateway_rpc": "authenticated_gateway_jsonl_rpc",
    "daemon_http": "daemon_allowlisted_http_listener",
    "direct_daemon_rpc": "authenticated_daemon_rpc",
}
