from __future__ import annotations

import enum
import time
from dataclasses import dataclass


class ComponentStatus(enum.StrEnum):
    NOT_CONFIGURED = "not_configured"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"


@dataclass
class _Component:
    configured: bool
    required: bool
    progress_timeout_seconds: float | None
    status: ComponentStatus
    progress_at: float | None = None


@dataclass(frozen=True)
class RuntimeHealthSnapshot:
    ready: bool
    components: dict[str, ComponentStatus]


class RuntimeHealth:
    """Process-local component state used by readiness supervision.

    Components are registered once in the composition root. Updates intentionally carry no
    error details, endpoints, or configuration values so the public snapshot is safe to expose.
    """

    def __init__(self) -> None:
        self._components: dict[str, _Component] = {}

    def register(
        self,
        name: str,
        *,
        configured: bool = True,
        required: bool = True,
        progress_timeout_seconds: float | None = None,
    ) -> None:
        if name in self._components:
            raise ValueError(f"Runtime health component {name!r} is already registered")
        self._components[name] = _Component(
            configured=configured,
            required=required,
            progress_timeout_seconds=progress_timeout_seconds,
            status=(ComponentStatus.STARTING if configured else ComponentStatus.NOT_CONFIGURED),
        )

    def starting(self, name: str) -> None:
        component = self._configured_component(name)
        component.status = ComponentStatus.STARTING
        component.progress_at = None

    def ready(self, name: str, *, now: float | None = None) -> None:
        component = self._configured_component(name)
        component.status = ComponentStatus.READY
        if component.progress_timeout_seconds is not None:
            component.progress_at = time.monotonic() if now is None else now

    def progress(self, name: str, *, now: float | None = None) -> None:
        self.ready(name, now=now)

    def degraded(self, name: str) -> None:
        component = self._configured_component(name)
        component.status = ComponentStatus.DEGRADED

    def snapshot(self, *, now: float | None = None) -> RuntimeHealthSnapshot:
        current_time = time.monotonic() if now is None else now
        statuses: dict[str, ComponentStatus] = {}
        runtime_ready = True
        for name, component in self._components.items():
            status = component.status
            timeout = component.progress_timeout_seconds
            if (
                status is ComponentStatus.READY
                and timeout is not None
                and (
                    component.progress_at is None or current_time - component.progress_at > timeout
                )
            ):
                status = ComponentStatus.DEGRADED
            statuses[name] = status
            if component.configured and component.required and status is not ComponentStatus.READY:
                runtime_ready = False
        return RuntimeHealthSnapshot(ready=runtime_ready, components=statuses)

    def progress_ages(self, *, now: float | None = None) -> dict[str, float]:
        current_time = time.monotonic() if now is None else now
        return {
            name: max(0.0, current_time - component.progress_at)
            for name, component in self._components.items()
            if component.progress_at is not None
        }

    def is_ready(self, *, now: float | None = None) -> bool:
        return self.snapshot(now=now).ready

    def _configured_component(self, name: str) -> _Component:
        try:
            component = self._components[name]
        except KeyError as error:
            raise KeyError(f"Unknown runtime health component: {name}") from error
        if not component.configured:
            raise ValueError(f"Runtime health component {name!r} is not configured")
        return component
