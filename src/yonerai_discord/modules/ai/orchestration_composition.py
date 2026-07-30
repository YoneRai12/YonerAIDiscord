"""Durable orchestration の明示的な production 注入境界。"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from pathlib import Path

import discord

from .action_router import NaturalActionRouter
from .models import AIRequest
from .orchestration import (
    OrchestrationEngine,
    OrchestrationPlan,
    OrchestrationPolicy,
    PlanExecutionOutcome,
    PlanObserver,
)
from .orchestration_repository import (
    SqliteOrchestrationRepository,
    StartupReconciliationReceipt,
)


class OrchestrationCompositionError(RuntimeError):
    """Durable orchestration の所有権または lifecycle が不正。"""


async def _await_startup_reconciliation_completion(
    task: asyncio.Task[StartupReconciliationReceipt],
) -> None:
    """Cancellation 後もto_thread workerの完了と結果回収を保証する。"""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.exception()


class _DurableOrchestrationEngine(OrchestrationEngine):
    """取得済みengine参照にもruntime停止境界を強制する。"""

    def __init__(
        self,
        router: NaturalActionRouter,
        *,
        policy: OrchestrationPolicy,
        observer: PlanObserver | None,
        repository: SqliteOrchestrationRepository,
    ) -> None:
        super().__init__(
            router,
            policy=policy,
            observer=observer,
            repository=repository,
        )
        self._accepting = True
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def accepting(self) -> bool:
        return self._accepting

    def begin_close(self) -> None:
        self._accepting = False

    async def drain(self) -> None:
        await self._drained.wait()

    async def execute_outcome(
        self,
        plan: OrchestrationPlan,
        *,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
        observer: PlanObserver | None = None,
    ) -> PlanExecutionOutcome:
        if self._accepting is not True:
            raise OrchestrationCompositionError("durable orchestration runtime is closing")
        self._in_flight += 1
        self._drained.clear()
        try:
            if self._accepting is not True:
                raise OrchestrationCompositionError("durable orchestration runtime is closing")
            return await super().execute_outcome(
                plan,
                message=message,
                request=request,
                request_id=request_id,
                observer=observer,
            )
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._drained.set()


@dataclass(frozen=True, slots=True)
class DurableOrchestrationConfiguration:
    """環境変数を読まずに composition root から注入する設定。"""

    enabled: bool = False
    repository_path: Path | None = field(default=None, repr=False)
    lease_seconds: float = 660.0
    max_runs: int = 1_024

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        path = self.repository_path
        if path is not None and not isinstance(path, Path):
            raise TypeError("repository_path must be a Path")
        if self.enabled and path is None:
            raise ValueError("repository_path is required when durable orchestration is enabled")
        if self.enabled and path is not None and not path.is_absolute():
            raise ValueError("repository_path must be absolute when durable orchestration is enabled")
        lease = self.lease_seconds
        if (
            isinstance(lease, bool)
            or not isinstance(lease, (int, float))
            or not math.isfinite(float(lease))
            or not 1.0 <= float(lease) <= 900.0
        ):
            raise ValueError("lease_seconds is outside the bounded range")
        if isinstance(self.max_runs, bool) or not isinstance(self.max_runs, int) or not 1 <= self.max_runs <= 10_000:
            raise ValueError("max_runs is outside the bounded range")
        object.__setattr__(self, "lease_seconds", float(lease))


class DurableOrchestrationRuntime:
    """単一の engine/repository identity を所有する lifecycle object。"""

    def __init__(
        self,
        engine: OrchestrationEngine,
        repository: SqliteOrchestrationRepository,
    ) -> None:
        if not isinstance(engine, OrchestrationEngine):
            raise TypeError("engine must be an OrchestrationEngine")
        if not isinstance(repository, SqliteOrchestrationRepository):
            raise TypeError("repository must be a SqliteOrchestrationRepository")
        if engine.repository is not repository:
            raise OrchestrationCompositionError("engine repository identity does not match")
        self.engine = engine
        self.repository = repository
        self._consumer: DurableOrchestrationConsumer | None = None
        self._startup_reconciliation: StartupReconciliationReceipt | None = None
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def startup_reconciliation(self) -> StartupReconciliationReceipt | None:
        return self._startup_reconciliation

    @property
    def ready(self) -> bool:
        return (
            self._consumer is not None
            and self._startup_reconciliation is not None
            and not self._closing
            and not self._closed
            and isinstance(self.engine, _DurableOrchestrationEngine)
            and self.engine.accepting
            and self.engine.repository is self.repository
            and self.engine.registry is self.engine.router.registry
        )

    def begin_close(self) -> None:
        self._closing = True
        if isinstance(self.engine, _DurableOrchestrationEngine):
            self.engine.begin_close()

    def _acquire(self, consumer: DurableOrchestrationConsumer) -> None:
        if self._closing or self._closed:
            raise OrchestrationCompositionError("durable orchestration runtime is closing")
        if self._consumer is not None:
            raise OrchestrationCompositionError("durable orchestration runtime already has a consumer")
        if self.engine.repository is not self.repository:
            raise OrchestrationCompositionError("engine repository identity changed")
        self._consumer = consumer

    def _record_startup_reconciliation(self, receipt: StartupReconciliationReceipt) -> None:
        if not isinstance(receipt, StartupReconciliationReceipt):
            raise TypeError("receipt must be a StartupReconciliationReceipt")
        self._startup_reconciliation = receipt

    def _release(self, consumer: DurableOrchestrationConsumer) -> None:
        if self._consumer is not consumer:
            raise OrchestrationCompositionError("durable orchestration consumer identity changed")
        self._consumer = None

    def _owned_by(self, consumer: DurableOrchestrationConsumer) -> bool:
        return self._consumer is consumer

    async def close(self) -> None:
        """新規利用を閉じる。SQLite は操作ごと接続なので永続handleは持たない。"""

        async with self._close_lock:
            if self._closed:
                return
            self.begin_close()
            if self._consumer is not None:
                raise OrchestrationCompositionError("durable orchestration runtime is still published")
            if isinstance(self.engine, _DurableOrchestrationEngine):
                await self.engine.drain()
            self._closed = True


class DurableOrchestrationConsumer:
    """botへruntimeを公開し、所有した同一参照だけを回収する。"""

    _RUNTIME_ATTRIBUTE = "ai_orchestration_runtime"
    _ENGINE_ATTRIBUTE = "ai_orchestration_engine"
    _REPOSITORY_ATTRIBUTE = "ai_orchestration_repository"
    _RECONCILIATION_ATTRIBUTE = "ai_orchestration_startup_reconciliation"

    def __init__(self, owner: object) -> None:
        if owner is None:
            raise TypeError("owner is required")
        self.owner = owner
        self._runtime: DurableOrchestrationRuntime | None = None
        self._withdraw_identity_changed = False
        self._lock = asyncio.Lock()

    @property
    def runtime(self) -> DurableOrchestrationRuntime | None:
        return self._runtime

    @property
    def engine(self) -> OrchestrationEngine | None:
        runtime = self._runtime
        return None if runtime is None else runtime.engine

    @property
    def startup_reconciliation(self) -> StartupReconciliationReceipt | None:
        runtime = self._runtime
        return None if runtime is None else runtime.startup_reconciliation

    @property
    def ready(self) -> bool:
        runtime = self._runtime
        return (
            runtime is not None
            and runtime.ready
            and runtime._owned_by(self)
            and not bool(getattr(self.owner, "is_closing", False))
            and all(getattr(self.owner, name, None) is value for name, value in self._publications(runtime).items())
        )

    async def start(self, runtime: DurableOrchestrationRuntime) -> None:
        if not isinstance(runtime, DurableOrchestrationRuntime):
            raise TypeError("runtime must be a DurableOrchestrationRuntime")
        async with self._lock:
            if self._runtime is runtime and self.ready:
                return
            if self._runtime is not None:
                raise OrchestrationCompositionError("consumer already owns a runtime")
            if bool(getattr(self.owner, "is_closing", False)):
                raise OrchestrationCompositionError("runtime owner is closing")
            if any(hasattr(self.owner, name) for name in self._publication_names()):
                raise OrchestrationCompositionError("orchestration runtime attribute is already owned")
            runtime._acquire(self)
            published: list[tuple[str, object]] = []
            try:
                reconciliation_task = asyncio.create_task(asyncio.to_thread(runtime.repository.reconcile_startup))
                try:
                    receipt = await asyncio.shield(reconciliation_task)
                except asyncio.CancelledError:
                    await _await_startup_reconciliation_completion(reconciliation_task)
                    raise
                runtime._record_startup_reconciliation(receipt)
                if bool(getattr(self.owner, "is_closing", False)):
                    raise OrchestrationCompositionError("runtime owner is closing")
                publications = self._publications(runtime)
                if any(hasattr(self.owner, name) for name in publications):
                    raise OrchestrationCompositionError("orchestration runtime attribute is already owned")
                for name, value in publications.items():
                    setattr(self.owner, name, value)
                    published.append((name, value))
                self._runtime = runtime
                self._withdraw_identity_changed = False
            except BaseException:
                try:
                    for name, value in reversed(published):
                        if getattr(self.owner, name, None) is value:
                            delattr(self.owner, name)
                finally:
                    runtime._release(self)
                raise

    async def begin_close(self) -> None:
        async with self._lock:
            runtime = self._runtime
            if runtime is not None:
                runtime.begin_close()

    async def stop(self) -> None:
        async with self._lock:
            runtime = self._runtime
            if runtime is None:
                return
            runtime.begin_close()
            publications = self._publications(runtime)
            if runtime._owned_by(self):
                self._withdraw_identity_changed = any(
                    not hasattr(self.owner, name) or getattr(self.owner, name) is not value
                    for name, value in publications.items()
                )
                for name, value in publications.items():
                    if getattr(self.owner, name, None) is value:
                        delattr(self.owner, name)
                runtime._release(self)
            await runtime.close()
            self._runtime = None
            identity_changed = self._withdraw_identity_changed
            self._withdraw_identity_changed = False
            if identity_changed:
                raise OrchestrationCompositionError("published orchestration runtime identity changed")

    @classmethod
    def _publications(cls, runtime: DurableOrchestrationRuntime) -> dict[str, object]:
        receipt = runtime.startup_reconciliation
        if receipt is None:
            raise OrchestrationCompositionError("startup reconciliation is not complete")
        return {
            cls._RUNTIME_ATTRIBUTE: runtime,
            cls._ENGINE_ATTRIBUTE: runtime.engine,
            cls._REPOSITORY_ATTRIBUTE: runtime.repository,
            cls._RECONCILIATION_ATTRIBUTE: receipt,
        }

    @classmethod
    def _publication_names(cls) -> tuple[str, ...]:
        return (
            cls._RUNTIME_ATTRIBUTE,
            cls._ENGINE_ATTRIBUTE,
            cls._REPOSITORY_ATTRIBUTE,
            cls._RECONCILIATION_ATTRIBUTE,
        )


def compose_durable_orchestration(
    router: NaturalActionRouter,
    configuration: DurableOrchestrationConfiguration,
    *,
    policy: OrchestrationPolicy | None = None,
    observer: PlanObserver | None = None,
) -> DurableOrchestrationRuntime | None:
    """明示ONの場合だけ、同一identityのrepositoryとengineを構成する。"""

    if not isinstance(router, NaturalActionRouter):
        raise TypeError("router must be a NaturalActionRouter")
    if not isinstance(configuration, DurableOrchestrationConfiguration):
        raise TypeError("configuration must be a DurableOrchestrationConfiguration")
    if configuration.enabled is not True:
        return None
    effective_policy = OrchestrationPolicy() if policy is None else policy
    if not isinstance(effective_policy, OrchestrationPolicy):
        raise TypeError("policy must be an OrchestrationPolicy")
    if observer is not None and not callable(observer):
        raise TypeError("observer must be callable")
    if configuration.lease_seconds <= effective_policy.total_timeout_seconds:
        raise ValueError("repository lease must exceed the total orchestration timeout")
    path = configuration.repository_path
    if path is None:
        raise ValueError("repository_path is required when durable orchestration is enabled")
    repository = SqliteOrchestrationRepository(
        path,
        lease_seconds=configuration.lease_seconds,
        max_runs=configuration.max_runs,
    )
    engine = _DurableOrchestrationEngine(
        router,
        policy=effective_policy,
        observer=observer,
        repository=repository,
    )
    return DurableOrchestrationRuntime(engine, repository)
