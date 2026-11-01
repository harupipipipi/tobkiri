"""Official Wasmtime synthetic memory bounds probe, not custom guest evidence."""
from pathlib import Path
import json
import sys
sys.path.insert(0, '/validation-deps')
from wasmtime import Config, Engine, Instance, Module, Store, Trap, TrapCode

limit = 128 * 1024 * 1024
config = Config()
config.consume_fuel = True
config.memory_reservation = limit
engine = Engine(config)
module = Module(engine, '''(module
  (memory (export "memory") 1)
  (func (export "read") (param i32) (result i32)
    local.get 0 i32.load8_u)
  (func (export "grow") (param i32) (result i32)
    local.get 0 memory.grow))''')
results = []
with Store(engine) as store:
    store.set_limits(memory_size=limit, memories=4, instances=64,
                     tables=64, table_elements=65536)
    store.set_fuel(100000)
    instances = [Instance(store, module, []) for _ in range(2)]
    for instance in instances:
        exports = instance.exports(store)
        assert exports['read'](store, 0) == 0
        trapped = False
        try:
            exports['read'](store, 65536)
        except Trap as error:
            assert error.trap_code == TrapCode.MEMORY_OUT_OF_BOUNDS
            trapped = True
        assert trapped
        assert exports['grow'](store, 2048) == -1
        assert exports['memory'].size(store) == 1
        results.append({'in_bounds_read': True, 'out_of_bounds_trapped': True,
                        'growth_beyond_store_limit_denied': True,
                        'pages_after_denied_growth': 1})
record = {'scope': 'synthetic-official-wasmtime-memory-bounds-only',
          'memory_reservation': limit, 'memory_size_limit': limit,
          'two_instances': results, 'custom_component_executed': False,
          'production_config_changed': False}
Path('/work/reservation-probe.json').write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps(record))
