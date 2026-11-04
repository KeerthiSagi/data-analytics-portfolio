"""Exceptions raised by the REFINE engine.

Every error carries a short machine-readable `code` (for tests and the UI)
and a plain-language message that tells the user what to do next.
"""

from __future__ import annotations


class RefineError(Exception):
    """Base class for all REFINE errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


class LoadError(RefineError):
    """The file could not be read into a table.

    Nothing is loaded when this is raised; any previously loaded dataset
    is left as it was.
    """


class CleaningError(RefineError):
    """A cleaning step could not be previewed, applied, undone or replayed.

    The cleaning session is unchanged when this is raised. Codes include:

    * invalid_params, unknown_operation: the step's settings are wrong.
    * confirmation_required: apply was called without explicit confirmation.
    * stale_preview, preview_used, unknown_preview, other_session: the
      preview no longer matches the current data, was already applied, or
      was not issued by this session.
    * no_change: the settings are valid but the step would change nothing
      (for example no cell matches, or there are no duplicates).
    * nothing_to_undo.
    * replay_mismatch: replaying recorded steps did not reproduce the
      recorded result.
    """
