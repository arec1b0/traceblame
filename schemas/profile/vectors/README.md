# Golden vectors of profile algorithms

A golden vector pins what a profile attribute's `source.algorithm` produces for one input.
They are the evidence that lets the compat checker treat a pure rewording of an algorithm as
a minor change instead of a major one (`algorithm_reworded`, ADR-0002 section 5, issue #32).

## Layout

```
vectors/v<profile major>/<attribute>.json
```

One file per attribute, named after it, one directory per profile major (a new major starts a
new registry). The attribute must carry an `algorithm_version` in the profile, and the file's
`algorithm_version` must be the same number.

```json
{
  "attribute": "aegis.tool.args_hash",
  "algorithm_version": 1,
  "vectors": [
    {
      "id": "key-order-flat",
      "input": "{\"units\": \"metric\", \"city\": \"Paris\"}",
      "canonical": "{\"city\":\"Paris\",\"units\":\"metric\"}",
      "expected": "016ad0ce38cd3c9a",
      "note": "free text, optional"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Stable key: lowercase letters, digits, `.`, `_`, `-`. Unique in the file. |
| `input` | The algorithm's input as text. |
| `canonical` | The intermediate form, or `null` where the algorithm falls back. Diagnostic, but part of the vector. |
| `expected` | The output every implementation must produce. |
| `note` | Optional. |

The format is JSON so that Go, Python and TypeScript read it with their standard libraries.
Files are written with `\u` escapes for non-ASCII text. Strings must not contain a lone
surrogate: a test input that has one is written as text, `\\ud83d`. Inputs that are not valid
UTF-8 cannot be expressed here; the normalizer's unit tests keep them.

## Rules

- **Vectors may be added, never changed or removed.** The checker (`traceblame-contracts
  compat`, `make contract`) compares by `id` against the base revision (`CONTRACT_BASE_REF`).
  A changed or removed vector is a major change. Adding vectors emits no change and needs no
  profile revision bump.
- **A reword is minor only against base vectors.** `algorithm_reworded` needs a non-empty
  file at the base revision, with every base vector unchanged at head. Vectors added in the
  same PR as the rewording do not count: they were not run against the old computation.
- **A different output is a different algorithm.** If the correct `expected` changes, the
  computation changed: raise `algorithm_version`, which is a major change (the next profile
  major).
- **The checker does not run the vectors.** Every implementation of the algorithm runs all
  of them as a contract test in CI, and a PR that adds an implementation adds its runner.

## Runners

| Implementation | Runner | Status |
|---|---|---|
| Normalizer (Go) | `collector/processor/normalizer/content_vectors_test.go`: `jcs.Canonicalize` and `argsHash`, directly and through the processor | done (#32) |
| Python shim | added by the shim's PR (#12) | to do |
| TypeScript shim | added by the shim's PR (#13) | to do |

A shim PR must run every vector in `v1/aegis.tool.args_hash.json` through its own
implementation and fail on any mismatch, and must not merge without it. Shims also check that
the file's `algorithm_version` is the one they implement.

`packages/contracts` checks the registry itself: files parse, ids are unique, each file
belongs to a profile attribute with the same `algorithm_version`, and, for `args_hash`, that
`expected` is SHA-256 (first 16 hex characters) of `canonical`, or of `input` when `canonical`
is `null`, computed in Python independently of the normalizer.

## `aegis.tool.args_hash`

The 33 seed vectors cover:
- the RFC 8785 section 3.2.2 sample and the section 3.2.3 UTF-16 property order;
- key order (flat, nested, arrays untouched, whitespace around);
- numbers: RFC 8785 appendix B samples, integers, fractions and exponent forms;
- string escapes and unicode: `\u` escapes equal literals, NFC and NFD stay different, astral
  characters;
- top-level scalars;
- the raw-UTF-8 fallback for arguments that are not I-JSON: not JSON, empty, truncated,
  duplicate property, lone surrogate, trailing data, a number out of range, non-ASCII text.

`canonical` strings come from RFC 8785 and `internal/jcs/jcs_test.go`; `expected` was computed
with Python's `hashlib`, not with the code under test.
