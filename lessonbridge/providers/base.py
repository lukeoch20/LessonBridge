"""District provider interface.

The MVP ships one implementation (Loudoun County). Other districts plug in by
implementing the same interface; nothing downstream knows which district it is
talking to.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from ..schemas import CalendarEventSpec, GradingPeriodSpec, SourcePayload, StandardSpec


@dataclass(frozen=True)
class SourceDescriptor:
    key: str
    doc_type: str
    title: str
    url: str | None
    refresh_interval_days: int = 30
    subject: str | None = None


class DistrictProvider(abc.ABC):
    slug: str = "base"
    name: str = "Base district"
    state: str = "VA"

    @abc.abstractmethod
    def sources(self) -> list[SourceDescriptor]:
        """Every public source this provider knows how to retrieve."""

    @abc.abstractmethod
    def fetch(self, key: str, *, allow_network: bool = True) -> SourcePayload:
        """Return the payload for a source key (live when possible, else snapshot)."""

    @abc.abstractmethod
    def get_academic_calendar(self, school_year: str) -> list[CalendarEventSpec]: ...

    @abc.abstractmethod
    def get_grading_periods(self, school_year: str) -> list[GradingPeriodSpec]: ...

    @abc.abstractmethod
    def get_standards(self, subject: str, grade: int) -> list[StandardSpec]: ...

    @abc.abstractmethod
    def get_curriculum(self, subject: str, grade: int) -> dict: ...

    @abc.abstractmethod
    def get_pacing_guide(self, subject: str, grade: int) -> dict | None: ...

    @abc.abstractmethod
    def get_schools(self) -> list[dict]: ...


_REGISTRY: dict[str, type[DistrictProvider]] = {}


def register(cls: type[DistrictProvider]) -> type[DistrictProvider]:
    _REGISTRY[cls.slug] = cls
    return cls


def get_provider(slug: str = "lcps") -> DistrictProvider:
    if not _REGISTRY:
        from . import loudoun  # noqa: F401  (registers itself)
    try:
        return _REGISTRY[slug]()
    except KeyError as exc:
        raise KeyError(f"No district provider registered for {slug!r}. Known: {sorted(_REGISTRY)}") from exc
