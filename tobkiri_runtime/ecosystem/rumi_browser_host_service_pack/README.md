# Rumi Browser Host Service Pack

This pack owns typed browser session/profile/cookie/navigation/capture/download
operations. Its service entrypoints contain no tool, agent, chat, or UI import.
Calls return `host_intent` values which core Authority validates. After exact
token validation, the Viewer helper invokes this pack's isolated browser runner.

Observation and control are separate global contracts. Control never accepts a
client supplied approval flag or token. Removing this pack removes both browser
contracts while leaving the host broker and unrelated desktop capabilities intact.
Browser profile, cookie, tab, and session metadata has one atomic owner under
`RUMI_USER_DATA/browser_host`; it is not shared with the legacy tool controller.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

