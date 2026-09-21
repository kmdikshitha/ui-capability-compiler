# discovery-run

A genuine LLM-driven run against the live application. Model
`claude-sonnet-5`, called through the Anthropic SDK with tool use and prompt
caching on the system prompt and the tool definitions.

Result: **finished** after 11 steps.

## What the model was given

Only low-level primitives — `observe`, `click`, `type`, `select`, `navigate`,
`read`, `finish`, `escalate`. There is deliberately no `search_member()` or
`open_subaccount()` tool: a semantic tool would mean the flow was discovered by
whoever wrote the tool list, not by the model.

It sees the screen as an indexed accessibility-tree rendering rather than HTML.
The element ids on this surface regenerate on every request, so there is nothing
stable in the markup worth handing it.

## Files

- `transcript.jsonl` — one record per tool call: the observation it decided
  from, its reasoning where the model chose to think, the action, the guardrail
  decision, and the result.
- `emitted-artifact.json` — what the compiler produced from this transcript.
- `meta.json`, `result.json`.

Not committed: the Playwright trace and the final screenshot. Both show the member
and balance unredacted — the trace as DOM text, the screenshot as pixels — because
neither passes through `redact()`. They are written to `runs/` for local debugging.

## Two things worth looking at

**The unnamed controls.** Three steps act on form fields with no accessible name
at all. The model reached them via the label in their table row, and the
compiler recorded that as `label_adjacent` targeting rather than `role_name` —
which is why the ladder has that rung.

**The password field.** The model filled the login password box. The compiler
does not write that value into the artifact; it emits a `secrets.password`
reference that replay resolves from the environment at run time. An earlier
version inlined it, which is exactly the class of defect only a real discovery
run surfaces.

Input values are declared `sensitive`, so they appear here only masked
(`MEMB_****nn`). The transcript on disk never held them in the clear.
