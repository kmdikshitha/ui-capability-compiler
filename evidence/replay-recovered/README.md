# replay-recovered

An injected maintenance interstitial. The artifact **declares** this condition
and how to clear it, so replay dismisses the banner and carries on.

Exit code: 0 (success). `result.json` lists
`recoveries: ["maintenance_banner"]`.

This is why `recoverable` is not an exit code: it is an in-flight condition that
always resolves into one of the six statuses. The caller gets `success`, and the
fact that a recovery was needed is data on the result.
