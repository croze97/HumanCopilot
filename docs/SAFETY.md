# Human copilot mode — human help for execution blockers only

## Exhaust autonomous routes before a handoff

Use shell and API tools first. Prefer autonomous shell/API, text and browser DOM
checks for authorized implementation and verification. Read files, logs and
state; run commands and tests yourself. Make routine technical, implementation
and planning decisions using the user's intent and evidence.

Screenshots and pixel capture are disabled by default. The operator may open a
temporary allowance in Desktop Control (Services -> Agent screen capture: all,
a region, or one window); inside it, screenctx screen_crop/screen_diff may be
used and refuse elsewhere. Never open, edit or request an allowance (including
screenctx capture_policy.py writes), and do not solicit snips. If pixels are needed,
record the observation as unverified and rely on normal app use feedback.
Inspect window handles, geometry and sizes; read button/control metadata and
text through accessibility APIs (a11y) and browser DOM. No computer-vision navigation.
Existing operator-owned snip functionality is preserved for unsolicited
attachments; it is not an invitation to request one.

Call Human Copilot ONLY for an actual execution blocker: you cannot complete
the needed step yourself and need a human fact, action or authorization to
continue implementation or operation. Before a call, state the specific
observation or action needed, the concrete autonomous routes tried and why
each failed or is unavailable.
For an operator-only fact, physical action or required approval, explain why
only the operator can supply it after checking available evidence and existing
authorization. Do not manufacture a decision or approval gate.

Never hand off to save tokens, time or automation effort. A task involving a
UI does not require human visual QA. Uncertainty alone is not a handoff reason:
investigate with available tools. Do not turn it into a questionnaire or ask
for automatic sign-off. If the operator says "I don't know, you decide", use
your judgment and continue.

Do not proactively request QA, test checklists, final acceptance or visual
sign-off, even after all autonomous work is finished. Trust normal app use and
unsolicited issue reports to supply operator feedback; do not ask the operator
to test or report back. Run your own tests and verify worker results. Record
physical or unavailable verification in Beads with the exact limitation and
evidence; keep unchecked outcomes unverified. A test you cannot run is not
passed, and missing verification alone is not an execution blocker. Do not
turn awaiting_test into an operator assignment; continue available work.

## Route only necessary questions

While enabled and callable, use `mcp__human-copilot__interactive_feedback` for
the justified handoff instead of `request_user_input` / `request_user_input_async`,
including their queued follow-up inputs. This is channel routing only: no
approval bypass and no new reason to ask. If unavailable or disabled, do not
repeatedly call the stale tool; state that once and use the supported
direct/native channel for a still-necessary question.

## Send a self-contained request

The call blocks until the operator answers. They see only your request on the
desktop overlay. Each pending request gets its own card, identified by
session, number and colour. Automatic terminal windows and copilot panes are
disabled; do not open copilot-tui or another surface as a fallback. If the
overlay is unavailable, preserve the blocker and continue autonomous work.
(`HC_REQUIRE_ATTACHED=1` refuses unattached calls).

- Open with 1–2 context lines: what needs human help, why your tools cannot
  resolve it, and what the answer unlocks. Keep technical evidence concise.
- For actions needed to unblock execution, use 1–5 numbered steps, one action
  each: name the window or device, exact action, and `→ what should change`.
  About 20 words per step; clarity comes first. Table checklists put the step
  number in the first cell.
- Do setup yourself, including commands and opening programs.
  `copilot-open <cmd>` can open a terminal window on the operator's desktop.
- Every window-specific step needs `@{Window · region}`. Window is a title/class
  substring matching exactly ONE window initially; region is `top-left` through
  `bottom-right`, `center`, `full`, or `x,y,w,h` fractions. The request follows
  that window through title changes; name a popup explicitly when it becomes
  the target. Missing or ambiguous matches are errors; never guess a replacement
  for a closed target. Untargeted prose/choices stack at the top middle.
- Set `timeout` to 1800–3600 seconds for necessary multi-step assistance.

Decisions MUST use `mode: "choice"` with
`options: [{value, label, hint}]`: unique machine value, readable label, and
the option's cost/consequence. Recommend an option when evidence supports it.
The exact selected value returns. Modes: `checklist` (default), `text` (free
prose), `choice` (2–12 options), `ack` (confirmation only; the only mode where
an empty send is an answer). Prose that merely looks like choices is not a
structured decision. Markdown renders; inline links/code/emphasis flatten.

## Interpret the report

Overlay reports label each step done / FAILED / NOT DONE, with notes and any
unsolicited operator attachments. NOT DONE is neither failed nor passed. Pane answers may be
plain prose. Dismissal is a decline, never a pass; timeout or no response leaves
the requested action UNVERIFIED. Record it in Beads and continue available work;
do not immediately repeat an unanswered request. Autonomous evidence may
establish a result separately, but never present it as the operator's verdict.
