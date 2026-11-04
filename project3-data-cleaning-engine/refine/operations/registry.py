"""The operations REFINE knows, by identifier.

Recipes refer to operations by `op_id`, so an identifier must never be
reused for a different behavior.
"""

from __future__ import annotations

from refine.errors import CleaningError
from refine.operations.base import Operation

_REGISTRY: dict[str, Operation] = {}


def register(operation: Operation) -> Operation:
    if operation.op_id in _REGISTRY and type(_REGISTRY[operation.op_id]) is not type(operation):
        raise ValueError(f"Operation id {operation.op_id!r} is already registered.")
    _REGISTRY[operation.op_id] = operation
    return operation


def get_operation(op_id: str) -> Operation:
    try:
        return _REGISTRY[op_id]
    except KeyError:
        raise CleaningError("unknown_operation", f"REFINE has no cleaning operation called {op_id!r}.") from None


def available_operations() -> tuple[Operation, ...]:
    return tuple(_REGISTRY.values())
