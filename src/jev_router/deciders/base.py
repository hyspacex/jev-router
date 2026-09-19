"""The decider interface and its registry.

A decider answers one question: given this request, which model and effort?
Register a new one with @register_decider("name") and name it in router.yaml
under settings.decider, settings.fallback_decider or an alias's `decider`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from ..config import AliasCfg, RouterConfig
from ..features import Features
from ..policy import PlanEntry, Shift


@dataclass
class Decision:
    model: str
    effort: str | None
    rule: str
    reason: str
    answers: dict[str, Any] = field(default_factory=dict)
    jev_ms: float | None = None
    jev_input_tokens: int | None = None
    fallback: bool = False
    state: Any = None
    state_builder: str = ""
    notes: list[str] = field(default_factory=list)
    decider: str = ""
    routing_error: str | None = None
    # The route this came from, the rungs the forwarder may try, and what
    # quota pressure did to the choice.
    route: str | None = None
    plan: list[PlanEntry] = field(default_factory=list)
    pressures: dict[str, float] = field(default_factory=dict)
    shifts: list[Shift] = field(default_factory=list)
    reordered: bool = False
    pressure_changed_the_outcome: bool = False

    def entries(self) -> list[PlanEntry]:
        """The plan, or just this decision when there is no plan."""
        if self.plan:
            return list(self.plan)
        return [PlanEntry(model=self.model, effort=self.effort, provider="")]


@runtime_checkable
class Decider(Protocol):
    name: str

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision: ...


def pressure_source(deps: dict[str, Any]) -> Callable[[], dict[str, float]]:
    """The dependency that reports quota pressure, or a source of nothing.

    A decider built without one routes exactly as it did before quota
    existed, which is what the eval scripts and the CLI want.
    """
    fn = deps.get("pressures")
    return cast(Callable[[], dict[str, float]], fn) if callable(fn) else dict


DeciderFactory = Callable[[RouterConfig, dict[str, Any]], "Decider"]

DECIDERS: dict[str, DeciderFactory] = {}


def register_decider(name: str) -> Callable[[DeciderFactory], DeciderFactory]:
    def deco(factory: DeciderFactory) -> DeciderFactory:
        if name in DECIDERS:
            raise ValueError(f"decider {name!r} is already registered")
        DECIDERS[name] = factory
        return factory

    return deco


def build_decider(name: str, config: RouterConfig, deps: dict[str, Any]) -> Decider:
    try:
        factory = DECIDERS[name]
    except KeyError:
        raise KeyError(
            f"unknown decider {name!r} (known: {', '.join(sorted(DECIDERS))})"
        ) from None
    return factory(config, deps)
