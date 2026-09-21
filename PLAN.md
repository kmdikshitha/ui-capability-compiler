# PLAN.md — Computer-Use Automation System

Build spec for Claude Code. This file is authoritative: when something here conflicts with a default instinct, follow this file. Read it fully before writing code.

**What this is:** interface.ai take-home. An LLM discovers how to complete a task in a legacy bank UI that has no API, the run compiles into a typed versioned capability artifact, and that artifact replays deterministically afterward with no model in the loop — plus real human handoff on the same live browser session, and safety guardrails throughout.

**Time box:** 2 days of build. Do not gold-plate. When an hour budget is hit, stub the rest at a clean seam and record it in the cuts list.

---

## 0. Non-negotiables

These are graded directly. Violating any of them is a failed submission, not a style difference.

1. **`src/replay/` must never import `anthropic` or `src/agent/`.** Replay is the no-model path. There is a test that enforces this (§10.4). This is the thesis of the whole project.
2. **Business outcomes are not failures.** "No such member" returns cleanly with data. The brief names conflating these as the most common mistake it sees.
3. **The human takes over the *same* live browser session.** Not a screenshot and a request to go do it manually. The browser process outlives the decision loop.
4. **No secrets or raw PII in artifacts, logs, evidence, or intervention requests.** Everything written to disk passes through one redaction function.
5. **The discovery run must be real.** At least one genuine LLM-driven run against the live app, with evidence committed.
6. **No scaling infrastructure.** No queues, no brokers, no Docker Compose orchestration, no database. Single process per role, flat files. The brief explicitly penalizes premature infrastructure.

---

## 1. Stack (locked — do not substitute)

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Env / deps | `uv` |
| Browser | Playwright, **sync API**, Chromium |
| Perception | `locator.aria_snapshot()` (ARIA/accessibility tree) |
| Resolution | `get_by_role(role, name=...)` + ranked fallbacks |
| LLM | Anthropic SDK, Claude Sonnet, tool use, prompt caching on system + tools |
| Schemas | Pydantic v2 |
| Artifact on disk | canonical JSON (`sort_keys=True`, `separators=(",",":")`) |
| Mock app | FastAPI + Jinja2 |
| Operator console | FastAPI + server-rendered HTML forms (no JS build) |
| CLI | Typer |
| IPC | flat files + polling (write-temp-then-rename) |
| Logging | stdlib `logging` + JSON formatter + redaction `Filter` |
| Tests | pytest |

Direct dependencies, and nothing else without a reason written into REPORT.md:
`playwright · anthropic · pydantic · fastapi · uvicorn · jinja2 · typer · pyyaml · pytest`

**Why sync Playwright:** the operator console is a separate process, so the CLI does one thing at a time. Concurrency lives at the process level, not the coroutine level. Note this boundary in REPORT.md.

---

## 2. Repo layout

```
README.md
REPORT.md
PLAN.md                      # this file
allowlist.yaml
.env.example                 # ANTHROPIC_API_KEY=
pyproject.toml

mock_app/
  main.py                    # FastAPI app, routes, fault injection
  data.py                    # seeded members
  templates/                 # Jinja2, deliberately hostile markup
  tenants/
    heritage.yaml            # tenant A config (base)
    community.yaml           # tenant B config (variant)

src/
  __init__.py
  cli.py                     # Typer: discover | replay | console | validate
  types.py                   # shared primitives: Action, Observation, Element, Locator

  surface/
    protocol.py              # Surface Protocol: observe() / act()
    playwright_driver.py     # the only module that imports playwright
    normalize.py             # ARIA snapshot -> Observation

  agent/
    loop.py                  # observe -> decide -> act
    tools.py                 # tool definitions handed to Claude
    prompts.py

  artifact/
    models.py                # Pydantic: CapabilityArtifact and friends
    compiler.py              # transcript -> artifact
    store.py                 # load / save / version / content hash
    overrides.py             # tenant override merge + contract-hash guard

  replay/
    executor.py              # the deterministic path
    resolver.py              # ranked locator ladder
    checkpoints.py
    outcomes.py              # ReplayResult and the taxonomy

  guardrails/
    allowlist.py
    risk.py
    redaction.py

  escalation/
    state.py                 # controller token + transitions
    requests.py              # InterventionRequest read/write
    console.py               # FastAPI operator console

  evidence/
    recorder.py              # run dirs, step traces, screenshots, tracing

artifacts/
  member.open_subaccount.v1.json
tenant-overrides/
  community.yaml
runs/                        # gitignored working dir
evidence/                    # curated, committed
  discovery-run/
  replay-success/
  replay-business-outcome/
  replay-hard-failure/
  escalation/
  tenant-variant/
tests/
```

**Dependency direction — enforce it:**

```
guardrails/   imports: types only
artifact/     imports: types only
surface/      imports: types, playwright
replay/       imports: types, artifact, surface, guardrails, escalation
agent/        imports: types, surface, guardrails, anthropic
escalation/   imports: types only
```

Nothing imports upward. `replay/` importing `agent/` or `anthropic` is a build error (test in §10.4).

---

## 3. Core types (`src/types.py`)

```python
from typing import Literal, Protocol
from pydantic import BaseModel

Role = Literal["button","textbox","link","heading","cell","row","checkbox",
               "combobox","dialog","alert","table","region","text"]

class Element(BaseModel):
    ref: str                      # opaque handle valid only within one Observation
    role: Role
    name: str                     # accessible name
    value: str | None = None
    bounds: tuple[int,int,int,int] | None = None   # x, y, w, h
    frame_path: list[str] = []    # iframe chain, outermost first
    enabled: bool = True

class Observation(BaseModel):
    url: str
    title: str
    elements: list[Element]
    text_digest: str              # visible text, truncated, for outcome detection
    screenshot_path: str | None = None

ActionType = Literal["click","type","navigate","read","select"]

class Action(BaseModel):
    type: ActionType
    ref: str | None = None        # discovery uses ref; replay resolves a Locator first
    value: str | None = None
    url: str | None = None

class ActResult(BaseModel):
    ok: bool
    error: str | None = None

class Surface(Protocol):
    def observe(self) -> Observation: ...
    def act(self, action: Action) -> ActResult: ...
    def close(self) -> None: ...
```

`Surface` is the seam. `playwright_driver.py` is the only implementation and the only module importing playwright. A desktop driver would implement the same two methods against the OS accessibility API — say this in REPORT.md §4.

---

## 4. Artifact schema (`src/artifact/models.py`)

```python
LocatorStrategy = Literal["role_name","label_adjacent","table_anchor","ordinal","bbox"]
Risk            = Literal["safe","reversible","irreversible"]

class Locator(BaseModel):
    strategy: LocatorStrategy
    role: Role | None = None
    name: str | None = None
    label: str | None = None
    header: str | None = None          # table_anchor: column header
    row_key: str | None = None         # table_anchor: value identifying the row
    index: int | None = None           # ordinal
    bbox: tuple[int,int,int,int] | None = None
    frame_path: list[str] = []

class TargetSpec(BaseModel):
    primary: Locator
    fallbacks: list[Locator] = []
    rationale: str                     # WHY this targeting is robust — required, graded

class Checkpoint(BaseModel):
    type: Literal["element_visible","text_present","url_matches"]
    role: Role | None = None
    name: str | None = None
    text: str | None = None
    pattern: str | None = None
    timeout_ms: int = 8000

class Step(BaseModel):
    id: str                            # s1, s2, ...
    action: ActionType
    target: TargetSpec | None = None
    value: str | None = None           # "{{ inputs.member_id }}" or a literal
    risk: Risk = "safe"
    checkpoint: Checkpoint | None = None

class InputParam(BaseModel):
    name: str
    type: Literal["string","integer","money","date","boolean"]
    required: bool = True
    sensitive: bool = False            # drives redaction

class OutputParam(BaseModel):
    name: str
    type: Literal["string","integer","money","date","boolean"]
    extract: Locator

class BusinessOutcome(BaseModel):
    name: str                          # member_not_found, permission_denied
    detect: Checkpoint
    returns: dict[str, str] = {}

class RecoverableCondition(BaseModel):
    name: str                          # maintenance_banner, transient_slow
    detect: Checkpoint
    recover: list[Step]
    max_attempts: int = 2

class CapabilityArtifact(BaseModel):
    schema_version: int = 1
    capability_id: str                 # member.open_subaccount
    version: int
    status: Literal["draft","approved"] = "draft"
    contract_hash: str = ""            # sha256 over the CONTRACT subset — see §4.1
    description: str
    target_app: str                    # cu-core
    tenant_variant: str = "base"
    entry_url_pattern: str             # /members/:id   (canonicalized)
    preconditions: list[Checkpoint] = []
    inputs: list[InputParam]
    outputs: list[OutputParam]
    steps: list[Step]
    success: Checkpoint
    business_outcomes: list[BusinessOutcome] = []
    recoverable: list[RecoverableCondition] = []
    recorded_at: str
    recorded_by_model: str
```

### 4.1 Contract hash — **DIFFERENTIATOR, do not skip**

`contract_hash = sha256(canonical_json({inputs, outputs, success, business_outcomes}))`.

The **contract** is inputs + outputs + success condition + declared business outcomes. The **mechanics** are steps, locators, recoveries.

A tenant override may patch *mechanics only*. `overrides.py` recomputes the hash after merging and raises if it changed. In plain terms: a tenant can change *how you find a control*, never *what the capability returns*. Without this rule one tenant's override can silently make `get_balance` return the wrong field and every calling agent is none the wiser.

### 4.2 Versioning

- Artifacts are **immutable**. Never edit v1 in place.
- Filename: `artifacts/{capability_id}.v{version}.json`
- A re-record or a learned fix writes v(n+1). Both stay on disk.
- `status` is orthogonal to version: new artifacts are `draft`; a human promotes to `approved` via `cli.py approve`.

### 4.3 Serialization

Canonical JSON only: `json.dumps(obj, sort_keys=True, separators=(",",":"))`. Byte-stability is required for the contract hash. Provide `cli.py show --format yaml` for human reading; never store YAML for artifacts. Tenant override files *are* YAML (hand-edited, not hashed).

---

## 5. Mock app (`mock_app/`) — HARD CAP 2 HOURS

A deliberately legacy credit-union teller console. Ugly is correct. No CSS framework, no JS framework.

### 5.1 Hostile properties (build these ON PURPOSE — they make your job harder, which is the point)

- Main work area inside a nested `<iframe src="/frame/workarea">`
- Table-based layout, `<td>` soup, **zero** `data-testid` attributes
- Element ids regenerate per request: `id="ctl00_{random6}_txtMemberId"`
- Server-rendered, full page reloads, no XHR
- Session cookie with configurable TTL
- Labels are plain `<td>` text to the left of inputs, not `<label for=...>` — this is what forces `label_adjacent` targeting

Accessible names must still exist (aria-label or button text), otherwise the AX tree is unusable and the project stalls. Hostile ≠ impossible.

### 5.2 Flow

`/login` → `/search` → `/members/{id}` → `/members/{id}/subaccount/new` → `/members/{id}/subaccount/confirm`

### 5.3 Seeded data & fault injection

`mock_app/data.py`, ~20 invented members. No real names, no real PII.

| Trigger | Behavior |
|---|---|
| member `12345` | happy path |
| member `99999` | "No record found" → business outcome |
| member `55555` | "You do not have permission to service this account" |
| `POST /admin/inject {mode}` | arms the next request |
| mode `timeout` | session cookie invalidated → login screen mid-flow |
| mode `interstitial` | "Scheduled maintenance" banner with a `Dismiss` button |
| mode `slow` | 8s delay on next render |
| mode `server_error` | 500 page |
| mode `clear` | disarm |

Fault injection must be an **endpoint**, not a code edit — tests and evidence runs call it.

### 5.4 Tenant variants

`mock_app/tenants/{heritage,community}.yaml` — button labels, page titles, branding, and for `community` one extra confirmation step. Selected by `?tenant=` or a startup flag. Same templates, different config. This is what makes §9 cheap.

---

## 6. Discovery (`src/agent/`)

### 6.1 Tools handed to Claude

```
observe()                      -> compact Observation rendering
click(ref: str)
type(ref: str, text: str)
navigate(url: str)
read(ref: str)                 -> element value/text
finish(outputs: dict)          -> model must NAME and EXTRACT the declared outputs
escalate(reason: str)          -> model admits it is stuck
```

Only low-level primitives. **Never** give the model semantic tools like `search_member()` — that would mean you did the discovery, not the agent, and a reviewer will spot it immediately.

### 6.2 Observation rendering given to the model

Filtered + indexed text, not raw HTML and not the full snapshot:

```
url: http://localhost:8000/members/12345
title: Member Services — Heritage CU
[e1] heading "Member 12345 — A. Rivera"
[e2] textbox "Member ID" = "12345"
[e3] button "Open Sub-Account"
[e4] cell "Savings" row="Account Type"
text: Available balance 1,240.55 ...
```

Attach a screenshot **only on the turn after a failed action**. Cheap in the common case; the failure screenshot is evidence you need anyway.

### 6.3 Loop mechanics

- Max 25 steps.
- Stop on: `finish`, `escalate`, max steps, wall-clock timeout (120s), dead-end (identical `text_digest` three turns running).
- Every tool call goes through guardrails **before** execution — same function replay uses.
- Prompt caching on the system prompt + tool definitions (a run is 20+ turns re-sending the same preamble).
- `max_tokens` small — every response is a tool call, not prose.
- Every turn appended to `runs/{run_id}/transcript.jsonl`: observation digest, model reasoning, action, result, screenshot path.

---

## 7. Compiler (`src/artifact/compiler.py`)

Transcript → `CapabilityArtifact`. Deterministic; no second LLM pass.

1. **Parameterize.** For each step value, compare against the input values supplied at launch. Exact match → `{{ inputs.<name> }}`. Everything else stays literal.
   *Known limitation, state it in REPORT.md:* fails for values derived mid-flow (app generates an account number, a later step reuses it). Next step would be a second compiler pass.
2. **Build TargetSpec.** For the element acted on, emit the full ladder from the Observation it came from:
   `role_name` → `label_adjacent` → `table_anchor` → `ordinal` → `bbox`. Write a one-line `rationale`.
3. **Classify risk.** Rule table on action + accessible name:
   - `navigate`, `read` → `safe`
   - `type`, `select` → `reversible`
   - `click` where name matches `(?i)(confirm|submit|transfer|open|create|delete|post|approve)` → `irreversible`
   - other `click` → `reversible`
4. **Attach checkpoints.** After every `irreversible` step and the final step, derive a checkpoint from the *next* observation's most prominent heading or a URL change.
5. **Canonicalize the route.** `/members/12345` → `/members/:id` using the input values. Makes the artifact tenant-portable from the start.
6. **Declare outcomes.** Success = final checkpoint. Business outcomes and recoverables come from `capability_specs/{capability_id}.yaml` — a small hand-authored file naming the outcomes the *business* cares about. **Document this honestly in REPORT.md:** the model discovers the mechanics, a human declares which outcomes matter, because "no such member is a legitimate answer" is a product decision, not something observable from one happy-path run.
7. Compute `contract_hash`, set `status="draft"`, write `artifacts/{id}.v{n}.json`.

---

## 8. Replay (`src/replay/`)

### 8.1 Executor

```
load artifact  →  merge tenant override (verify contract_hash unchanged)
             →  validate inputs against artifact.inputs   (else caller_error)
             →  check preconditions
             →  for each step:
                    guardrails.check(action, risk, status)   # may block/escalate
                    locator = resolver.resolve(step.target)  # ladder, record the rung
                    surface.act(...)
                    if step.checkpoint: assert it
                    on anomaly: classify()  →  business | recoverable | hard
             →  assert success checkpoint
             →  extract outputs
             →  ReplayResult
```

No `sleep()`. Wait on checkpoint conditions with `timeout_ms` from the artifact.

### 8.2 Resolver ladder

Try in order; **record which rung resolved** into the step trace.

| Rung | Implementation |
|---|---|
| 1 `role_name` | `frame.get_by_role(role, name=name, exact=True)` |
| 2 `label_adjacent` | find text node == label, take the next input in DOM order within the same row |
| 3 `table_anchor` | find row whose cell matches `row_key`, take the cell under column `header` |
| 4 `ordinal` | nth element of that role within the frame |
| 5 `bbox` | click coordinates — recorded, never trusted alone; using it emits a warning into the trace |

Exhausting the ladder is a **hard failure**, never a guess.

### 8.3 Rung telemetry — **DIFFERENTIATOR, do not skip**

Every resolution appends `{step_id, rung_used, attempted_rungs}` to the run trace, and the run summary reports the max rung used. A capability that historically resolved at rung 1 and now resolves at rung 3 has **drifted** — flag it for re-record before it breaks outright. `cli.py drift --capability <id>` compares the latest run against the artifact's recorded baseline rungs. This is the concrete answer to §3.7's "how do you detect and manage per-tenant/version drift."

### 8.4 Result contract (`src/replay/outcomes.py`)

```python
class StepTrace(BaseModel):
    step_id: str
    action: ActionType
    rung_used: LocatorStrategy | None
    attempted_rungs: list[LocatorStrategy]
    checkpoint_ok: bool | None
    duration_ms: int
    screenshot_path: str | None

class FailureDetail(BaseModel):
    step_id: str
    expected: str
    observed: str
    evidence_dir: str

class ReplayResult(BaseModel):
    status: Literal["success","business_outcome","escalated",
                    "blocked_by_policy","caller_error","failed"]
    capability_id: str
    version: int
    tenant_variant: str
    outputs: dict[str, object] | None = None
    outcome: str | None = None           # member_not_found, ...
    failure: FailureDetail | None = None
    steps: list[StepTrace] = []
    recoveries: list[str] = []
    max_rung_used: LocatorStrategy | None = None
```

Exit codes: `0` success · `1` failed · `2` caller_error · `3` business_outcome · `5` escalated · `6` blocked_by_policy.

`recoverable` is deliberately **not** an exit code — it is an in-flight state that always resolves into one of the above. Say so in REPORT.md; it shows the taxonomy was reasoned through rather than enumerated.

### 8.5 Anomaly classification order

1. Does the observation match a declared `business_outcome`? → return it, exit 0. **Check this first.**
2. Does it match a declared `recoverable`? → run its recovery steps, retry the step, bounded by `max_attempts`.
3. Did the locator ladder exhaust, or a checkpoint time out? → hard failure.
4. Unknown dialog / unexpected state → escalate (§9).

---

## 9. Guardrails (`src/guardrails/`)

### 9.1 `allowlist.yaml`

```yaml
origins:
  - http://localhost:8000
paths:
  - ^/login$
  - ^/search$
  - ^/members/\d+$
  - ^/members/\d+/subaccount/(new|confirm)$
  - ^/frame/workarea$
actions: [click, type, navigate, read, select]
deny_paths:
  - ^/admin/
```

One function — `guardrails.check(action, risk, artifact_status, attended)` — called before **every** action, in discovery *and* replay. The shared code path is the design point; discovery is the more dangerous of the two because an LLM is exploring freely. Denials are logged, never silently skipped.

### 9.2 Risk policy

| Situation | Behavior |
|---|---|
| `safe` / `reversible`, in allowlist | proceed |
| off allowlist (origin, path, or action type) | `blocked_by_policy`, exit 6 |
| `irreversible`, artifact `draft`, unattended | block → raise intervention request |
| `irreversible`, artifact `approved` | proceed |
| `irreversible` during discovery | proceed (discovery is attended and explicitly a sandbox) |

One-line summary for REPORT.md: **discovery may explore; replay may only execute what was reviewed.**

### 9.3 Redaction

One function `redact(obj) -> obj`, applied by the logging `Filter` and by every writer to `artifacts/`, `evidence/`, `runs/`, and intervention requests.

- values of `InputParam` where `sensitive=True` → `MEMB_****45` (keep last 2)
- anything matching money patterns in evidence text → `***`
- cookies, `Authorization`, `ANTHROPIC_API_KEY` → never written at all
- **Cut:** screenshot pixel-masking. Record it explicitly in REPORT.md §6 and §7.

---

## 10. Escalation (`src/escalation/`) — start this at hour 4 of day 2, not hour 7

### 10.1 Browser lifecycle

```python
ctx = playwright.chromium.launch_persistent_context(
    user_data_dir="runs/browser-profile",
    headless=False,
    args=["--remote-debugging-port=9222"],
)
```

The browser is a separate OS process that **outlives the decision loop**. On escalation the CLI stops sending commands and waits. It does **not** exit and does **not** close the browser. The operator attaches with `connect_over_cdp("http://localhost:9222")` and gets the same tab, cookies and scroll position.

### 10.2 Control model — **DIFFERENTIATOR**

```python
Controller = Literal["agent","human","none"]
```

- `assert_control("agent")` before **every** action — not once at startup.
- Escalating: `controller = human`, write `runs/{run_id}/intervention.json`, stop acting.
- Resuming: `controller = agent`, then **re-observe from scratch**. Never assume the page is where you left it — the human may have navigated elsewhere. Re-verify the last passed checkpoint before continuing.
- Every transition appends to `runs/{run_id}/control.jsonl`:
  `{"ts":..., "from":"agent", "to":"human", "reason":"unknown_dialog", "step":"s4"}`

**`control.jsonl` is the transport, not a log.** The console writes the resume record; the CLI polls for it. Because the audit trail *is* the channel, it cannot drift from what actually happened. Say this in REPORT.md — it is the single best sentence in the design.

### 10.3 InterventionRequest

```python
class InterventionRequest(BaseModel):
    id: str
    run_id: str
    capability_id: str
    version: int
    goal: str
    step_id: str
    risk: Risk
    reason: str
    url: str
    screenshot_path: str
    cdp_url: str = "http://localhost:9222"
    created_at: str
    # NOTE: input parameter VALUES are deliberately absent — member PII.
```

That omission is your redaction policy visibly costing you something, which is more convincing than a paragraph claiming you redact. Call it out in REPORT.md.

### 10.4 Operator console

FastAPI, server-rendered, four routes:

```
GET  /                      open intervention requests
GET  /request/{id}          context + screenshot + "how to take control"
POST /request/{id}/resume   controller -> agent, append control.jsonl
POST /request/{id}/abort    run ends, status = failed
```

Mock the styling. The **control transfer must be real**. Scope note for REPORT.md: full co-browsing is out of scope per the brief; operator is local-only via CDP. That is a documented cut, not a flaw.

---

## 11. Multi-tenant (§3.7) — build only if core is green by midday day 2

- Base artifact unchanged. `tenant-overrides/community.yaml` patches individual `Step.target` entries by `step_id`.
- `overrides.py` merges, recomputes `contract_hash`, raises `ContractViolation` if it changed.
- Evidence: one run of the base artifact against tenant B **without** the override (fails at the renamed button) and one **with** it (passes). That single pair converts a paragraph of claims into a demonstration.

If you run out of time: cut this, and write the design into REPORT.md §4 instead. The brief only asks for design here.

---

## 12. Evidence (`src/evidence/`)

Every run writes `runs/{run_id}/`:

```
meta.json          goal, capability, version, tenant, model, started/ended
transcript.jsonl   discovery only: per-turn observation digest, reasoning, action
steps.jsonl        replay: StepTrace per step incl. rung_used
control.jsonl      control transfers (escalation runs)
result.json        ReplayResult
screenshots/       per step + on failure
trace.zip          Playwright tracing — start on run, stop on failure
```

Turn on Playwright tracing: `context.tracing.start(screenshots=True, snapshots=True)`. One line, and it satisfies §3.5's "richer signal on failure" with a trace a reviewer can step through in the Playwright viewer. Best effort-to-impression ratio in the project.

Curate six runs into committed `evidence/`:

1. `discovery-run/` — real LLM run, transcript, screenshots, emitted artifact
2. `replay-success/` — **different input params than discovery** (proves parameterization, not memorization)
3. `replay-business-outcome/` — member 99999 → clean `member_not_found`, exit 3
4. `replay-hard-failure/` — injected `timeout` → structured failure with expected vs observed
5. `escalation/` — injected unknown dialog → intervention → manual completion → resume → success, with `control.jsonl`
6. `tenant-variant/` — base artifact on tenant B, without and with override

---

## 13. CLI (`src/cli.py`)

```bash
uv run python -m src.cli discover \
  --goal "look up member 12345 and open a new savings sub-account, reach the confirmation screen" \
  --target http://localhost:8000 --input member_id=12345

uv run python -m src.cli replay \
  --artifact artifacts/member.open_subaccount.v1.json \
  --input member_id=54321 [--tenant community] [--unattended]

uv run python -m src.cli approve --artifact artifacts/member.open_subaccount.v1.json
uv run python -m src.cli console --port 5056
uv run python -m src.cli show --artifact ... --format yaml
uv run python -m src.cli drift --capability member.open_subaccount
```

README must state loudly: **`replay` requires no `ANTHROPIC_API_KEY`.** That is the thesis, and it doubles as "how to run without live services."

---

## 14. Tests (`tests/`) — target 15–25, no more

1. **Artifact round-trip** — build → canonical JSON → parse → identical; contract_hash stable across reserialization.
2. **Contract-hash guard** — an override touching `outputs` raises `ContractViolation`; one touching a locator does not.
3. **Taxonomy, one per bucket**, driven by `/admin/inject`:
   - member 99999 → `business_outcome`, exit 3
   - `interstitial` → recovery applied, run still succeeds, recovery recorded
   - `timeout` → `failed` with populated `FailureDetail`
   - bad input type → `caller_error`, exit 2
4. **Import boundary — DIFFERENTIATOR.** Walk `src/replay/**.py`, parse imports with `ast`, assert none resolve to `anthropic` or `src.agent`. Ten lines that mechanically prove the central claim instead of asserting it in prose.
5. **Guardrails** — off-allowlist origin denied; `irreversible` + `draft` + `unattended` blocked, exit 6.
6. **Redaction** — a sensitive input never appears verbatim in any file written under `runs/`.
7. **Resolver ladder** — rung 1 fails → rung 2 resolves; ladder exhausted → hard failure, no guessing.

**Skip agent-loop tests.** Non-deterministic, they cost real API calls, low signal. Say you skipped them and why in REPORT.md — deliberate non-testing with a reason reads as judgment.

---

## 15. Build order

### Day 1
| Hours | Task | Done when |
|---|---|---|
| 0–2 | Mock app (§5) **hard cap** | all six fault modes reachable via `/admin/inject` |
| 2–4 | `types.py`, `surface/` (§3) | `observe()` returns a sane Observation incl. iframe traversal |
| 4–8 | `agent/` discovery loop (§6) | one real green run end-to-end, transcript written |
| 8–10 | `artifact/` models + compiler (§4, §7) | a v1 artifact on disk with templated values and a ladder per step |

### Day 2
| Hours | Task | Done when |
|---|---|---|
| 0–4 | `replay/` executor, resolver, taxonomy (§8) | all four taxonomy buckets reproducible via injection |
| 4–6 | `guardrails/` + `escalation/` (§9, §10) | full escalate → manual → resume → complete cycle works |
| 6–8 | Evidence curation, README, REPORT (§12) | six evidence dirs committed, seven REPORT headings written |

**Freeze the artifact schema at end of day 1.** Changing it on day 2 invalidates the compiler, the executor and all evidence simultaneously.

### Where two days goes wrong
- Mock app eats day 1. It is a prop. Nobody grades its CSS.
- Prompt-tuning rabbit hole. One green run. Save the transcript. Move on.
- Escalation slips to the last hour. It is its own evaluation criterion.

---

## 16. REPORT.md — exactly these seven headings, 1–3 pages

1. **Architecture** — three processes and why (§ system design); the `Surface` seam; dependency direction.
2. **Artifact schema** — contract vs mechanics; templated values; ranked ladder; declared outcomes; immutable versions + draft/approved.
3. **Determinism & error handling** — *"determinism means no model makes a decision; it does not mean nothing varies"*; declared recoveries are data; the six-status result contract; why `recoverable` isn't an exit code.
4. **Heterogeneity & multi-tenant** — base + override; contract hash; rung telemetry as the drift signal; how a desktop driver slots into `Surface` without touching a single artifact.
5. **Escalation & handoff** — persistent headed browser; the controller token checked before every action; `control.jsonl` as transport not log; re-observe on resume; PII withheld from the request.
6. **Safety** — allowlist shared by both paths; risk classes; redaction; what is deliberately conservative and why.
7. **Cuts** — screenshot pixel-masking · desktop driver · co-browsing console · single capability · no LLM fallback on locator exhaustion · agent-loop tests · (multi-tenant, if cut). One line each, each with "what I'd do next."

Keep a running notes file from hour one. Write REPORT last, in one unhurried sitting — it is the cheapest hour in the project and the highest-variance one.

---

## 17. Definition of done

- [ ] `uv sync && uv run playwright install chromium` then three commands reproduce the demo
- [ ] `replay` runs green with **no** `ANTHROPIC_API_KEY` set
- [ ] Import-boundary test passes
- [ ] All four taxonomy buckets demonstrable via `/admin/inject`
- [ ] Escalate → manual → resume → complete works on the same browser session
- [ ] Six evidence directories committed
- [ ] `git log -p | grep -iE "sk-ant|api[_-]?key|secret"` is clean
- [ ] No raw member ID or balance in any committed file under `evidence/`
- [ ] REPORT.md has all seven headings, cuts included
- [ ] Repo public; email `assignments@interface.ai` with the URL on its own line, no zip
