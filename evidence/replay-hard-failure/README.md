# replay-hard-failure

An injected session timeout (`POST /admin/inject {"mode":"timeout"}`)
invalidates the session cookie mid-flow, so the work area renders "Session
Expired" instead of the member search.

Exit code: 1 (`failed`). `result.json` carries a `FailureDetail`
naming the step, what was expected and what was actually observed — enough to
debug without re-running. `screenshots/fail-s4.png` is the richer signal: it shows
the session-expired screen, which carries no member data.

Note what the executor does *not* do: the locator ladder is exhausted and the
run stops, rather than clicking something that looks similar.
