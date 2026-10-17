# QA Frontend Input Pack

This is a v4 Normal Sandbox Pack fixture for Launcher acceptance. It declares
one `/qa-frontend-input` route with a local text input. It requests no
capabilities and contains no executable functions or remote content.

After the signed copy is admitted, installed, approved, enabled, and selected
in the active Profile, open the route in the Shell. Its input is intentionally
local state: typing does not invoke a Tool or change the Chat composer.

The committed fixture is unsigned. Run `python -m tests.qa_frontend_input_pack`
from `tobkiri_runtime` to create a signed copy and an external public-key PEM.
The signing key exists only in process memory and is discarded on exit.
