# Structured composer input

`composer_input.input.fields` turns pack-owned JSON into a stable control panel above
the AI composer. The frontend accepts up to 16 fields and supports `select`, `text`,
and `textarea`. Unknown field types fall back to `select`; invalid fields are ignored.

```json
{
  "field_layout": "popover_above",
  "fields": [
    {
      "id": "output",
      "type": "select",
      "label": "Output",
      "default": "summary",
      "options": [
        { "value": "summary", "label": "Summary" },
        { "value": "code", "label": "Code" }
      ]
    },
    {
      "id": "constraints",
      "type": "text",
      "label": "Constraints",
      "placeholder": "Keep the existing API"
    }
  ]
}
```

When the user applies the template, values stay separate from the textarea and are
sent as structured JSON in `params.composer_fields` and `metadata.structured_input`.
The user's free-form message is never rewritten by the template UI.
