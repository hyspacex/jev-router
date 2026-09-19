"""The decider interface and its registry.

A decider answers one question: given this request, which model and effort?
Register a new one with @register_decider("name") and name it in router.yaml
under settings.decider, settings.fallback_decider or an alias's `decider`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import AliasCfg, RouterConfig
from ..features import Features


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


@runtime_checkable
class Decider(Protocol):
    name: str

    async def decide(self, features: Features, alias_cfg: AliasCfg) -> Decision: ...


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
