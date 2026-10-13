# Public scientific-code example

This synthetic working catalog demonstrates the preview `code-library` and
`code-run` contracts. It contains generic TT-matrix code and test definitions,
not the source repository's local catalog, Raw references, project records or
executed numerical results. It is not a paper-reproduction dataset.

## Implementation and check definitions

`src/tensor_train.py` provides bounded NumPy TT-matrix contraction and adjoints.
The cores use `(left_rank, output_mode, input_mode, right_rank)` storage and
finite real values. The implementation handles one input vector at a time;
it does not implement a classifier, optimizer or published training experiment.

`tests/test_tensor_train.py` defines independent per-element matrix comparisons
and finite-difference checks. A test definition is not a record that it ran;
an available catalog node is not a scientific correctness certificate.

```bash
python3 .scripts/wg.py code-library validate
python3 .scripts/wg.py code-library search 'tensor train'
python3 .scripts/wg.py code-library read code.tt_matvec
python3 computing/code-library/tests/test_tensor_train.py
```

## Explicit execution boundary

Read-only catalog queries never execute retrieved code. `code-run` requires
an explicitly selected fixed recipe, an existing local research project and
its compute policy. New outputs remain local to that project. Successful local
checks do not reproduce a paper or automatically publish knowledge.

See `operations/CODE_LIBRARY.md` and `operations/CODE_RUN.md` for the full
contracts. Extending this example requires reviewing the declarations and
binding evidence hashes to the actual local files; do not invent Raw or run
records to make a catalog appear complete.
