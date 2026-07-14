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
(cd "$out" && node --test tests/*.test.mjs)
node run_storefront_browser.mjs --tree "$out"
```

The runner later treats plans, inventory, and `expected-final.json` as
read-only inputs. It records runtime IDs and raw owners separately from the
`A01`–`A10` labels used only for its evidence join.
