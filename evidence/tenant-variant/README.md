# tenant-variant

The same base artifact against a second institution running the same vendor
product, branded and configured differently — renamed controls plus one extra
acknowledgement step.

| Run | Exit | Result |
|---|---|---|
| base artifact, **no** override | 1 | fails at the renamed control |
| base artifact, **with** `--tenant community` | 0 | succeeds |

`without-override.txt` is the first run; `console-output.txt` the second.
Nothing was re-recorded: `tenant-overrides/community.yaml` patches mechanics
only, and the merge recomputes the contract hash and would refuse anything that
changed what the capability returns.

The override was incomplete on the first attempt — two renamed buttons were
missing and the run **still passed**, because the ladder fell through to the
`ordinal` rung and the right control happened to sit in the right position. The
rung telemetry in `steps.jsonl` is what surfaced that.
