# replay-business-outcome

A member id that is not present in the core. The application answers "No record
found", which is a **legitimate result the caller needs**, not a crash.

Exit code: 3 (`business_outcome`), with `outcome: member_not_found`
and `failure: null` in `result.json`.

Conflating this with a hard failure is the mistake the brief names. The executor
checks declared business outcomes *first*, before it concludes that a locator is
broken.
