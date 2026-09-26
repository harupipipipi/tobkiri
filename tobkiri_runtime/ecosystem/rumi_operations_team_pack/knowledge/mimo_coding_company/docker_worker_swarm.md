# Docker Worker Swarm

Use isolated Ubuntu workers when browser or computer-use QA should not touch the host.

Default plan:

- Use `docker/mimo_coding_company/compose.yaml`.
- Scale the `worker` service to multiple replicas.
- Assign a different persona and target URL to each worker.
- Keep one worker focused on onboarding, one on power-user flows, one on impatient clicking, and one on keyboard-heavy paths.

Operating rule:

- The main reasoning agent decides what to test.
- Browser QA workers gather evidence.
- Reviewer summarizes only confirmed failures with repro steps.
