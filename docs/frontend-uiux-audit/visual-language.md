# Visual language and anti-template rules

The goal is not “less design.” The goal is a recognizably Rumi interface whose hierarchy, states, and trust cues come from the product rather than from generic AI-dashboard decoration.

## Product character

Rumi should feel:

- capable, calm, and operational;
- dense where work demands density, spacious where decisions demand attention;
- explicit about agency, authority, data source, and settlement;
- consistent across Viewer, defaultspack, Search Home, Mobile, setup, and companion surfaces.

It should not feel like a collection of unrelated template demos.

## Remove generic AI-template signals

Do not use these as default decoration:

- gradient text or gradient icon containers;
- ambient glow around ordinary cards and buttons;
- glass/blur panels without a layering or readability need;
- extra-large rounded rectangles around every group;
- card-inside-card-inside-card hierarchy;
- rainbow status colors with no semantic system;
- tiny uppercase eyebrow labels on every section;
- sparkle/brain/robot icons as generic “AI” indicators;
- vague copy such as “seamless,” “intelligent,” “powered by AI,” “magic,” or “revolutionary.”

A gradient, blur, glow, or large radius is allowed only when it encodes a real layer, selection, brand moment, or transition that simpler styling cannot communicate.

## Hierarchy before ornament

Use this order:

1. information architecture;
2. heading and label hierarchy;
3. spacing and alignment;
4. grouping and dividers;
5. typography and density;
6. restrained color/elevation;
7. ornament only when still necessary.

If removing the glow makes the hierarchy disappear, the hierarchy was never finished.

## Radius and elevation

Define a small radius scale and use it consistently. Suggested intent:

- small: compact controls, tags, code/details;
- medium: fields, menu items, ordinary panels;
- large: primary dialogs/sheets or deliberately prominent containers;
- full/pill: statuses, segmented controls, avatars—not every button.

Elevation must represent actual stacking. Hover elevation should not make static cards jump or shift by a pixel.

## Typography

- Use the product type stack; do not introduce arbitrary font families per surface.
- Body text must remain comfortable in Japanese and Latin scripts.
- Avoid critical 9–11 px content.
- Use line-height and measure that support long Japanese, URLs, code, and translated text.
- Do not uppercase user-facing Japanese. Uppercase Latin micro-labels should be rare and secondary.
- Numeric metrics should align consistently and disclose units.

## Color and status

One semantic state has one meaning across the product:

- neutral/inactive;
- information/pending;
- success/confirmed;
- warning/attention;
- danger/error/destructive.

Do not reuse success green for selection, online, approval, safe, and “default” without accompanying text. Color is supplementary to labels, icons, and semantic state.

## Icons

- Use one icon family and a deliberate size/stroke scale per density.
- An icon must represent the action, not decorate it.
- Repeated icon-only actions require accessible names and adequate targets.
- Avoid using Save icons for Edit, stars for unrelated activation, or generic boxes for every resource.
- Align icon and text baselines; do not compensate with arbitrary per-screen offsets.

## Cards and lists

Use a card only when the item needs an independent boundary or action group. Prefer rows, sections, and lists for dense related data.

A card needs:

- a clear primary identity;
- secondary metadata with controlled hierarchy;
- state that is readable without color;
- one obvious primary action where applicable;
- predictable repeated action placement;
- responsive behavior for long names and localization.

## Operational copy

Prefer:

- “Profile could not be saved. Your changes are still here.”
- “Connected to Tobkiri Launcher on this device.”
- “This schedule will run at 09:00 Asia/Tokyo.”

Avoid:

- “Something went wrong.”
- “We’ll safely take care of it.”
- “AI magic is working.”
- “Seamlessly optimize your intelligent workflow.”

## Micro-polish checklist

Before calling a surface visually complete, verify:

- 1 px alignment across repeated rows/cards;
- icon/text baselines;
- consistent hit area, padding, and control height;
- default, hover, focus-visible, pressed, disabled, pending, success, warning, and error states;
- cursor and text-selection behavior;
- no hover-induced layout shift;
- scrollbar and overflow behavior;
- long Japanese, long IDs, URLs, and unbroken strings;
- empty, one-item, many-item, and error layouts;
- light/dark/forced-color behavior where supported;
- 320 px width, short height, 200% zoom, and large text;
- no primary action hidden behind an on-screen keyboard or safe area.
