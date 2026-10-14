"""Inventory Flow schema specificity; never infer fields or promote readiness."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def classify(schema: Any) -> str:
    """Distinguish intentionally empty/dynamic contracts from declared fields."""
    if not isinstance(schema, Mapping):
        return 'untyped'
    if any(key in schema for key in ('$ref', 'oneOf', 'anyOf', 'allOf')):
        return 'composite-schema'
    if isinstance(schema.get('properties'), Mapping) and schema['properties']:
        return 'explicit-fields'
    if schema.get('type') == 'object':
        return 'empty-object' if schema.get('additionalProperties') is False else 'generic-object'
    if schema.get('type') in ('array', 'string', 'integer', 'number', 'boolean', 'null'):
        return 'non-object-root'
    return 'untyped'


def inventory(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Report each exact provider operation separately, honoring local schemas."""
    rows = []
    declarations = 0
    for pack in catalog['packs']:
        for contract in pack.get('provided_contracts', []):
            declarations += 1
            defaults = contract.get('schemas', {})
            for operation in contract['operations']:
                schemas = {**defaults, **operation.get('schemas', {})}
                row = {'pack_id': pack['pack_id'], 'contract_id': contract['contract_id'],
                       'operation_id': operation['id'], 'owner': contract['owner']}
                for direction in ('input', 'output'):
                    schema = schemas.get(direction, schemas.get('event') if direction == 'output' else None)
                    fields = schema.get('properties', {}) if isinstance(schema, Mapping) else {}
                    fields = fields if isinstance(fields, Mapping) else {}
                    row[direction] = {
                        'classification': classify(schema),
                        'fields': [{'name': name, 'type': value.get('type'),
                                    'value_type': value.get('x-tobkiri-value-type'),
                                    'role': value.get('x-tobkiri-flow-role', 'undeclared')}
                                   for name, value in fields.items() if isinstance(value, Mapping)],
                    }
                rows.append(row)
    return {'schema': 'io.tobkiri.flow-contract-inventory.v1',
            'authority': 'diagnostic-only', 'declared_contracts': declarations,
            'unique_contract_ids': len({r['contract_id'] for r in rows}),
            'operation_count': len(rows),
            'classification_counts': {direction: dict(sorted(Counter(r[direction]['classification'] for r in rows).items()))
                                      for direction in ('input', 'output')},
            'note': 'Generic object can be intentional. Missing field declarations are not inferred from names. This report is not execution or release proof.',
            'operations': rows}


def main() -> int:
    """Print the diagnostic or write it to an explicitly selected file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'schemas/pack_v4_catalog.v1.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = inventory(json.loads(args.catalog.read_text(encoding='utf-8')))
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
