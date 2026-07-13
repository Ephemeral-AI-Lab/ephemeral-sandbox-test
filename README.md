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

## Control Room UI

Build and serve the UI and controller on one loopback origin:

Use Node `^20.19.0`, `^22.13.0`, or `>=24.0.0`, as required by the UI toolchain.

```sh
cd /absolute/path/to/ephemeral-sandbox-test/e2e/web
npm install
npm run build
cd ../..
PYTHONPATH=e2e python3 -m harness.api \
  --test-repository-root /absolute/path/to/ephemeral-sandbox-test \
  --product-root /absolute/path/to/ephemeral-sandbox
```

Open `http://127.0.0.1:5173/e2e/catalog`.

## Benchmark laboratory

`benchmark/` is the complete external EphemeralOS benchmark application, not an
optional E2E configuration directory. It owns the Python campaign service and
the React/TypeScript laboratory while accessing the product only through
explicit canonical roots, prebuilt executables, catalog export, and the
authenticated gateway protocol. See `benchmark/README.md` for build and launch
commands.
