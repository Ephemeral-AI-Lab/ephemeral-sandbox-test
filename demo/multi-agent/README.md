# FlashCart multi-agent demo

`recipes.py` is the reviewable source of truth for the ten deterministic
FlashCart lanes. `generate_scripts.py --write` emits the checked-in payloads,
JSONL plans, scenario, and derived call budget; `--check` byte-compares them.
The 482 authored rows are real public-runtime operations when executed by the
runner—one CLI process and one parsed response per row.

`validate.py` is the pre-provision gate. It enforces lane identity, payload
hashes and containment, scoped mutations, immutable references, dependencies,
test cycles/inventory, no-padding command rules, and the derived 482-call
matrix. `update_inventory.py --write` and `update_oracle.py --from-tree
OFFLINE_TREE --write` are deliberately separate review-only paths.

To inspect the deterministic final tree without a sandbox:

```sh
out=$(mktemp -d /tmp/flashcart-offline.XXXXXX)
python3 materialize.py --out "$out"
python3 verify_oracle.py --tree "$out"
(cd "$out" && node --check tests/storefront.test.mjs)
node run_storefront_browser.mjs --tree "$out"
```

To replace the web-console creation clicks with one command:

```sh
python3 console_sandbox.py create --open
```

The launcher materializes a temporary final tree, creates one
`node:24-bookworm-slim` sandbox, opens its Terminal page, and prints the serve,
Preview, direct-preview, and cleanup commands. Use `--json` for a
machine-readable result, or `--workspace-root PATH` to choose the materialized
workspace location. The generated cleanup command destroys only the created
sandbox and removes the workspace only after validating its FlashCart marker.

For the presentation workload, trigger the real runner from the host and point
all 482 authored public CLI operations at one empty console target:

```sh
python3 console_sandbox.py live --open
```

`live` creates the `node:24-bookworm-slim` target, opens its Observability →
Events view with a 1,000-event window, and runs `run_demo.py` on the host with
the target sandbox ID and its empty bind root. The CLIs and manager credentials
stay on the host; the target is retained after the run so its Files, Traces,
Layers, Terminal, and final storefront Preview remain inspectable. Run the
printed cleanup command when the presentation is over.

The runner later treats plans, inventory, and `expected-final.json` as
read-only inputs. It records runtime IDs and raw owners separately from the
`A01`–`A10` labels used only for its evidence join.
