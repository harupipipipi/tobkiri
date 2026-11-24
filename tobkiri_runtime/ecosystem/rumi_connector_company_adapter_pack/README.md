# Rumi Connector Company Adapter Pack

Projects a verified, normalized connector event into Company contracts only
when public connector configuration explicitly includes the `company` route
and a `company_id`. The inbound record uses a deterministic ID, so replay does
not create duplicate Company input.

State append and coordinator routing use separate, exact authority receipts.
The adapter receives no credential reference or raw request body and imports no
connector, vendor, Company, coordinator, scheduler, or agent implementation.

