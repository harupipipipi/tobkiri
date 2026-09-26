# Self Improvement Loop

The harness should keep making meaningful forward progress without becoming noisy.

Loop order:

1. Review the current repo, open tasks, recent failures, and browser/computer QA findings.
2. Pick one small improvement that meaningfully strengthens the harness or repo.
3. Use MiMo vision for user-perspective browser/computer evidence and MiMo Pro for the fix.
4. Implement the change and verify it with the smallest useful test or UI repro.
5. Record the result, what unblocked it, and what should happen next.
6. If a missing tool or skill blocks the next step, create the smallest version that unblocks work.

Guardrails:

- Do not claim success from weak evidence.
- Prefer reversible, well-tested changes.
- Do not stop at issue creation when the loop can safely make and verify a fix.
- Keep long-running loops quiet unless there is meaningful movement or a real blocker.
