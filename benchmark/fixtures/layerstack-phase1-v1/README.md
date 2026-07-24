# LayerStack Phase1 Stage00 corpus v1

This directory is generated once and treated as an immutable benchmark input. It
contains deterministic, gzip-compressed text payloads for the Stage00 tiny
baseline. The manifest freezes compressed and uncompressed SHA-256 digests and
the workload shape.

To reproduce the files:

```sh
python3 benchmark/tools/generate_layerstack_phase1_corpus.py
```

Any corpus change requires a new corpus version and benchmark semantic revision.
