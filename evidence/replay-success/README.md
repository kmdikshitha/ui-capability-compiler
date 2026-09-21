# replay-success

The approved capability replayed with **different inputs than the discovery
run** — a different member and a different opening amount. That is what
separates a parameterized capability from a recorded macro.

No `ANTHROPIC_API_KEY` was set for this run.

Exit code: 0 (success).

`steps.jsonl` records which rung of the locator ladder resolved each step. The
sub-account form controls resolve at `label_adjacent` because they carry no
accessible name — the application labels them with plain `<td>` text, as legacy
screens do.

The input values are not repeated here: they are declared `sensitive` in the
capability contract, so they appear in this directory only in masked form. The
seeded fixtures are in `mock_app/data.py`.
