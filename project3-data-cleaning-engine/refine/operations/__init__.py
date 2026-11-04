"""Cleaning operations. Importing this package registers every operation."""

from refine.operations import drop_columns  # noqa: F401  (registers drop_columns)
from refine.operations import missing_ops  # noqa: F401  (registers the missing-value operations)
from refine.operations import duplicates_ops  # noqa: F401  (registers remove_duplicates)
from refine.operations import convert_ops  # noqa: F401  (registers convert_type)
from refine.operations import text_ops  # noqa: F401  (registers the text standardization operations)
from refine.operations.base import (
    Impact,
    Operation,
    OperationResult,
    Plan,
    Preview,
    Step,
    canonical_json,
    jsonable,
)
from refine.operations.registry import available_operations, get_operation, register

__all__ = [
    "Impact", "Operation", "OperationResult", "Plan", "Preview", "Step", "canonical_json", "jsonable",
    "available_operations", "get_operation", "register",
]
