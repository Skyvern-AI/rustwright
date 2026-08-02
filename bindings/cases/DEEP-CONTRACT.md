# Deep benchmark contract

The deep manifests extend benchmark manifest version 1 with one optional
case-level field:

    { "repeat": 25 }

repeat is an integer in the inclusive range 1 through 1000. Values outside
that domain are invalid. If repeat is absent, its value is 1.

## Execution semantics

For repeat 1, the runner executes every step once in array order. A goto is not
required, and behavior is exactly the legacy manifest-v1 behavior.

For repeat greater than 1:

1. The runner finds the first goto step. A repeated case without a goto is
   invalid and must be rejected before browser launch.
2. Every step before and including that first goto is the setup prefix. The
   runner executes the setup prefix once.
3. Every step after the first goto is the repeated block. The runner executes
   that block repeat times, in order, against the same page. Iterations are
   numbered from 1. Any later goto belongs to the repeated block and therefore
   runs once per iteration.

Assertions in the repeated block run in every iteration. The first failure
stops the case. A repeated-block failure has this exact prefix:

    iteration N: <error>

The Rust reference retains the original 1-based manifest step number inside
the error, for example: iteration 3: step 7: title mismatch: ... . A setup
failure is not iteration-prefixed.

Captures made by the setup prefix are retained. Captures made by the repeated
block are iteration-local while an iteration runs; after each iteration they
replace captures with the same names from earlier iterations. Consequently,
the reported capture values are those from the last iteration. Duplicate
capture names within one setup prefix or within one iteration remain errors.
If an iteration fails, values captured before its failing step replace values
from earlier iterations before the runner records the error.

The per-case ms value covers page creation, the one-time setup prefix, every
iteration of the repeated block, and page close.

## Result compatibility

repeat does not change the result schema and must not add iteration metadata:

    {
      "lang": "<language>",
      "results": [
        {
          "id": "<case-id>",
          "ok": true,
          "captures": {},
          "ms": 0.0
        }
      ]
    }

Failed cases add only the existing string error field. Wave 2 language
bindings and Playwright baselines must match the Rust reference byte-for-byte
on this schema: the same keys, value types, optional-error rule, case order,
capture names, and structural capture values.

Wave 2 must also add repeat to the machine-readable manifest schema and to
each language runner's manifest validator. Those validators must apply the
same default, inclusive 1-through-1000 domain, and
repeat-greater-than-1/missing-goto rejection rule before browser launch.
