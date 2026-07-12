# Ephemeral Sandbox E2E Control Room

This repository owns the external E2E suite. It reaches the product only through
the public `bin/` entrypoints and the Docker gateway network boundary.

Run collection from any directory with canonical absolute roots:

```sh
PYTHONPATH=/absolute/path/to/ephemeral-sandbox-test/e2e python3 -m harness.catalog.collect \
  --test-repository-root /absolute/path/to/ephemeral-sandbox-test \
  --product-root /absolute/path/to/ephemeral-sandbox \
  --output /absolute/path/to/ephemeral-sandbox-test/.e2e-state/catalog/catalog.json \
  --ledger /absolute/path/to/ephemeral-sandbox-test/e2e/metadata/stable-id-ledger.json
```

The `.e2e-state/` and `.benchmark-state/` leaves are disposable runtime state and
are intentionally ignored by Git.
