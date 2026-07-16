# Declarative status surfaces

`status_surface` is the feature-neutral RumiTemplate primitive for live workflow state in approved shell slots. The first host is `above_composer`; the contract also reserves `below_composer`, `chat_header`, `sidebar`, and `workspace_panel` for the corresponding shell hosts.

Each surface uses API version `rumi.status_surface.v1`, an opaque `surface_id`, a registered `data_source`, safe dotted field paths, and registered `action_id` controls. Template values are display metadata only. They cannot contain URLs, HTML, JavaScript, callbacks, import paths, or authority decisions.

The catalog supplies backend-owned snapshots and revisions. Local elapsed-time display derives from the backend `started_at` value, but mutations and final state remain authoritative on the backend. Action requests contain only the surface, control, registered action, bounded scalar value, data-source ID, and source revision. Errors keep the last displayed snapshot and are announced inline.

Supported controls are `button`, `toggle_button`, `expand`, `model_select`, `provider_select`, `thinking_select`, `select`, and `menu`. Model controls reuse the canonical model catalog. An action projection must carry an executable registered command contract; an ID-only action or frontend-only callback is not considered executable. The Composer host reports success only after the backend returns `executed: true`; approval-required and non-authoritative responses retain the displayed state and surface an error. Unknown controls, versions, paths, data sources, or actions render a visible diagnostic fallback and never execute.

Surfaces are ordered by descending priority, ascending order, then ID. Duplicate IDs fail closed in the backend catalog; the frontend also deterministically retains the highest-priority declaration. The host initially shows at most three surfaces and exposes keyboard-accessible overflow expansion for compact layouts.
