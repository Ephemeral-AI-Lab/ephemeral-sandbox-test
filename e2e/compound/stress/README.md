# Compound stress

This family groups the live retention-cap, finalize/destroy race-storm, and
layer-depth benchmark cases behind one catalog selection.

From `e2e/`, run the unified family with:

```sh
python3 -m pytest compound/stress \
  --test-repository-root /absolute/path/to/ephemeral-sandbox-test \
  --product-root /absolute/path/to/ephemeral-sandbox
```

All three cases run by default. `E2E_STORM_SECONDS`,
`E2E_EXEC_BENCH_DEPTHS`, and `E2E_EXEC_BENCH_SAMPLES` only tune workload size;
they do not enable or disable cases.
