# Human copilot mode — DISABLED

The `human-copilot` MCP lane is disabled. Do not call or wait for
`mcp__human-copilot__interactive_feedback`; a stale entry returns a disabled
result without prompting. Its absence is expected.

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

Ask for human help ONLY for an actual execution blocker: you cannot complete
the needed step yourself and need a human fact, action or authorization to
continue implementation or operation. Before asking, state the specific
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

Use the supported direct/native question channel (`request_user_input` /
`request_user_input_async` when available, or plain prose) without invoking Copilot.
Give 1–2 context lines explaining the human need and what it unlocks;
for actions needed to unblock execution, use 1–5 self-contained steps naming
the window/device, action and `→ what should change`. Do setup and open
programs yourself. Necessary
decisions carry explicit options with their stakes and a recommendation when
supported. Wait for the actual answer, then interpret it and resume work.
NOT DONE, dismissal and no response never mean passed. Do not immediately
repeat an unanswered request. Autonomous evidence may establish a result
separately, but never present it as the operator's verdict.

The toggle changes availability and routing, not the human-necessity boundary.
Re-enable with `copilot-toggle on` when requested.
