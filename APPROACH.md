# EBrowserAgent — Approach and Limitations

## Problem understanding

The goal is to run browser tests described in ordinary language without
hardcoding a website's selectors or allowing an AI model to run code. A useful
result must distinguish successful browser interactions from proof that the
expected condition was met.

## Architecture

FastAPI validates a bounded request and passes it to `TestOrchestrator`. The
orchestrator opens a fresh managed Playwright session, inspects a compact page
snapshot, and calls `PlannerService`. Planner providers implement one async
protocol; Gemini and Anthropic are supported alternatives. The checked-in
`.env.example` selects Anthropic, while the code-level default when `AI_PROVIDER`
is absent remains Gemini. Provider output is parsed by the canonical Pydantic
action models before the executor can use it. Anthropic SDK 2.6.0 is installed
in the local virtual environment and is within the declared requirements range.

The `DynamicLocatorResolver`, `SafeActionExecutor`, and `AssertionVerifier`
are separate components. The resolver maps a structured target to current
page elements. The executor performs only allow-listed interactions and
re-inspects the page. The verifier independently checks the assertions in the
plan. The API returns a bounded report, and the same-origin vanilla frontend
renders it using safe text nodes.

## Locator strategy

Prefer accessible role and name, label, placeholder, exact text, and observed
allow-listed attributes. These choices are easier to explain and tend to
survive layout changes better than positional CSS. A simple CSS tag or tag
plus one approved attribute is available for cases the semantic extractors
cannot express. XPath, positional selectors, arbitrary CSS, and ambiguous
matches are rejected. When a locator no longer matches the previous snapshot,
the resolver inspects the page once and retries against the new snapshot.

## Planning, execution, and replanning

The planner receives a sanitized instruction, sanitized snapshot, and only the
keys for optional test data. It returns structured JSON, never code. The
planner service validates actions, bounds, value sources, URLs, and references.
Sensitive values are resolved from the internal request only when a fill action
uses `value_ref`; they are not included in provider requests or reports.
Fill targets must be grounded in snapshot metadata. The inspector prioritizes
fillable controls within its bounded element budget; if a fill target is not
observed, planning fails safely instead of guessing. An unresolved fill lookup
is not replanned against unchanged evidence.

The executor runs actions sequentially with per-action timeouts, a run-wide
action cap, and a configurable overall deadline (300 seconds by default;
`EBROWSER_RUN_TIMEOUT_SECONDS`, bounded to 1–3600 seconds). Navigation uses the
existing URL validator. Each action is followed by a fresh snapshot, and
remaining targets are resolved against that current state. Every planned
action occurrence has a stable identity and attempt count. Replans reconcile
matching action occurrences one-to-one; duplicate identical actions remain
separate obligations and prevent PASS until each is completed. Replanning is
bounded to three times. Browser resource teardown and request-scoped provider
teardown each have a configurable cleanup grace window
(`EBROWSER_CLEANUP_GRACE_SECONDS`, five seconds by default); browser resources
share one browser cleanup window. The execution deadline covers the run; for
cancellation-cooperative asynchronous cleanup, expected request duration is
that deadline plus up to one browser cleanup window and one provider teardown
window, with framework/network overhead. This is not a hard process termination
or absolute response-time guarantee. `asyncio.wait_for()` and
`asyncio.timeout()` rely on cooperative cancellation and cannot forcibly stop a
coroutine that suppresses cancellation indefinitely. Synchronous code that
blocks the event loop can also prevent timeout callbacks from running. Stronger
termination guarantees would require process isolation or external supervision,
which is outside this assignment's current scope.

## Independent verification and outcomes

Assertions are final postconditions: the schema requires all browser actions to
precede all assertions, which are checked only after execution obligations are
resolved. Assertions are checked against the final page state rather than
inferred from successful clicks or fills. `assert_text` on a targeted input or
textarea compares the live value exactly; other targeted text checks visible
text. The locator must resolve uniquely. Values are compared in memory and are
not included in reports. A required assertion that is demonstrably false
produces FAIL. PASS requires at least one required assertion and every required
assertion passing. If the planner supplies no required assertions, an
inspection is unreliable, or required evidence cannot be established, the
result is INCONCLUSIVE. This conservative rule can report inconclusive even
after every action completed.

## Security measures and trade-offs

- Pydantic action allow-list; no arbitrary code execution.
- Provider request separation, test-data redaction, and content digest check.
- Private-network URL rejection, DNS checks, browser request guard, WebSocket
  blocking, and validated application navigation.
- Fresh browser contexts, bounded snapshots/actions/replans, and safe error
  messages.
- Same-origin frontend; no API key or page HTML is returned to it.
- Snapshot extraction omits current input values. The independent verifier may
  read an input value for a requested assertion, but never returns or logs it;
  password fills still require `value_ref`.
- Dynamic locator uniqueness checks avoid a dangerous “choose first” fallback.
  Interactions recheck count and visibility at the action boundary; a target
  that becomes ambiguous fails safely. Password fields expose locator metadata
  without values and accept fills only through `value_ref`.

These checks are defense in depth. DNS can change between validation and
connection, not all browser-owned network paths pass through application
routing, and URL validation does not replace firewall or container egress
controls. Production exposure also needs authentication, rate limiting,
resource isolation, and operational monitoring.

## Known limitations

The inspector is a bounded first-pass snapshot, not a complete accessibility
tree. Some custom or inaccessible controls cannot be located reliably. Wait
actions currently use a bounded duration rather than arbitrary wait conditions.
The API handles one synchronous run per request; it has no persistent run
history, event stream, or cancellation endpoint. The frontend shows a waiting
state and then the final report, not live action-by-action updates.

The latest reported offline suite result is 202 passing tests; it was not rerun
for this documentation change. Offline tests use mocks and fakes, so they do
not prove general compatibility with Chromium, all Playwright locator behavior,
production network conditions, or live Gemini generation. The user-run
Anthropic planner smoke test succeeded and returned a canonical structured plan;
it did not launch a browser. The manual PHPTravels run at
`https://phptravels.net/` completed with navigation PASS, fill PASS, independent
textbox-value assertion PASS, and overall PASS / COMPLETED. No second unfamiliar
site has been confirmed. Previous live Gemini generation diagnostics returned
HTTP 503 `UNAVAILABLE` or timed out.
