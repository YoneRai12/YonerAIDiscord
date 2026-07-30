"""外部副作用を安全に実行するDurable Action kernel。"""

from __future__ import annotations

from typing import Any

from .domain import (
    Attempt,
    Claim,
    ExecutionContext,
    Job,
    JobStatus,
    Lease,
    NonRetryableJobError,
    Outcome,
    OutcomeKind,
    Receipt,
    RetryableJobError,
    Revision,
)
from .plugin import JobsPlugin
from .executors import ExplicitExecutorRegistry, InternalNoopExecutor
from .ports import Executor, ExecutorRegistry
from .repository import JobSummary, SqliteJobRepository
from .service import DurableJobService, classify_exception
from .worker import DurableJobWorker, WorkerSnapshot


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("jobs", JobsPlugin)


__all__ = [
    "Attempt",
    "Claim",
    "DurableJobService",
    "Executor",
    "ExecutorRegistry",
    "ExecutionContext",
    "ExplicitExecutorRegistry",
    "Job",
    "JobStatus",
    "JobSummary",
    "Lease",
    "NonRetryableJobError",
    "Outcome",
    "OutcomeKind",
    "Receipt",
    "RetryableJobError",
    "Revision",
    "InternalNoopExecutor",
    "SqliteJobRepository",
    "DurableJobWorker",
    "WorkerSnapshot",
    "classify_exception",
    "setup",
]
