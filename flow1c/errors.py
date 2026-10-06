"""Shared operation failure preserving the existing CLI error contract."""


class WorkflowError(RuntimeError):
    pass
