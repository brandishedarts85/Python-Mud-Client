"""UI-neutral mapper observation/adaptation seam.

Adapters translate MUD-specific protocol payloads into a small generic room
observation. They never touch Qt, sockets, session controllers, or SQLite.

The default registry is intentionally explicit: optional modules may register
adapters, but registration is validated and duplicate keys are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Callable, Iterable, Protocol

MAX_EXTERNAL_ROOM_ID = 512
MAX_OBSERVED_EXITS = 128
MAX_REGISTERED_ADAPTERS = 128
_ADAPTER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _text(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


@dataclass(frozen=True)
class ObservedExit:
    direction: str
    destination_external_id: str | None = None
    command: str | None = None
    cost: float = 1.0


@dataclass(frozen=True)
class RoomObservation:
    external_id: str
    name: str | None = None
    area: str | None = None
    x: int | None = None
    y: int | None = None
    z: int | None = None
    exits: tuple[ObservedExit, ...] = ()
    source: str = "adapter"


class MapperAdapterCapability(str, Enum):
    """Capabilities declared by an adapter, not inferred from its key/name."""

    ROOM_IDENTITY = "room-identity"
    GMCP = "gmcp"
    MSDP = "msdp"
    EXITS = "exits"
    AREA = "area"
    COORDINATES = "coordinates"


class MapperAdapter(Protocol):
    key: str
    label: str

    def observe_gmcp(self, package: str, payload: Any) -> RoomObservation | None: ...
    def observe_msdp(self, variable: str, value: Any) -> RoomObservation | None: ...


AdapterFactory = Callable[[], MapperAdapter]


@dataclass(frozen=True)
class MapperAdapterDescriptor:
    key: str
    label: str
    factory: AdapterFactory
    capabilities: frozenset[MapperAdapterCapability]
    evidence: str | None = None


class MapperAdapterRegistry:
    """Validated registry suitable for built-ins and optional adapter modules."""

    def __init__(self) -> None:
        self._descriptors: dict[str, MapperAdapterDescriptor] = {}

    @staticmethod
    def _normalize_capabilities(
        capabilities: Iterable[MapperAdapterCapability | str],
    ) -> frozenset[MapperAdapterCapability]:
        normalized: set[MapperAdapterCapability] = set()
        for capability in capabilities:
            try:
                normalized.add(
                    capability
                    if isinstance(capability, MapperAdapterCapability)
                    else MapperAdapterCapability(str(capability))
                )
            except ValueError as exc:
                raise ValueError(f"unknown mapper adapter capability: {capability}") from exc
        return frozenset(normalized)

    def register(
        self,
        *,
        key: str,
        label: str,
        factory: AdapterFactory,
        capabilities: Iterable[MapperAdapterCapability | str] = (),
        evidence: str | None = None,
    ) -> MapperAdapterDescriptor:
        key = str(key).strip()
        label = str(label).strip()
        if not _ADAPTER_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid mapper adapter key: {key!r}")
        if not label or len(label) > 128:
            raise ValueError("mapper adapter label must be 1..128 characters")
        if not callable(factory):
            raise TypeError("mapper adapter factory must be callable")
        if key in self._descriptors:
            raise ValueError(f"mapper adapter already registered: {key}")
        if len(self._descriptors) >= MAX_REGISTERED_ADAPTERS:
            raise ValueError("mapper adapter registry is full")

        descriptor = MapperAdapterDescriptor(
            key=key,
            label=label,
            factory=factory,
            capabilities=self._normalize_capabilities(capabilities),
            evidence=_text(evidence, max_len=512),
        )

        # Validate the factory at registration time so a broken optional module
        # fails locally instead of much later when the user selects it.
        instance = factory()
        if getattr(instance, "key", None) != key:
            raise ValueError(f"mapper adapter factory key mismatch for {key}")
        if getattr(instance, "label", None) != label:
            raise ValueError(f"mapper adapter factory label mismatch for {key}")
        if not callable(getattr(instance, "observe_gmcp", None)):
            raise TypeError(f"mapper adapter {key} lacks observe_gmcp()")
        if not callable(getattr(instance, "observe_msdp", None)):
            raise TypeError(f"mapper adapter {key} lacks observe_msdp()")

        self._descriptors[key] = descriptor
        return descriptor

    def unregister(self, key: str) -> None:
        """Remove an adapter from this registry.

        Primarily useful for tests and optional-module unload. The default
        built-ins are not dynamically unregistered by normal client code.
        """

        self._descriptors.pop(str(key), None)

    def descriptor(self, key: str) -> MapperAdapterDescriptor:
        try:
            return self._descriptors[str(key)]
        except KeyError as exc:
            raise ValueError(f"unknown mapper adapter: {key}") from exc

    def choices(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.key, item.label) for item in self._descriptors.values())

    def create(self, key: str) -> MapperAdapter:
        descriptor = self.descriptor(key)
        instance = descriptor.factory()
        # A plugin changing identity after registration is treated as invalid.
        if getattr(instance, "key", None) != descriptor.key or getattr(instance, "label", None) != descriptor.label:
            raise ValueError(f"mapper adapter factory identity changed: {descriptor.key}")
        return instance


class ManualMapperAdapter:
    key = "manual"
    label = "Manual only"

    def observe_gmcp(self, package: str, payload: Any) -> RoomObservation | None:
        return None

    def observe_msdp(self, variable: str, value: Any) -> RoomObservation | None:
        return None


class GenericGmcpRoomAdapter:
    """Conservative adapter for the widely-used ``Room.Info`` package.

    It deliberately requires a stable room identifier. Names alone are not
    treated as identity because many MUDs reuse room titles.
    """

    key = "generic-gmcp"
    label = "Generic GMCP Room.Info"

    _ID_KEYS = ("num", "id", "roomid", "room_id", "vnum")
    _NAME_KEYS = ("name", "title")
    _AREA_KEYS = ("area", "zone")

    @staticmethod
    def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in mapping:
                return mapping[key]
        return None

    @staticmethod
    def _coord(payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if value is None and isinstance(payload.get("coord"), dict):
            value = payload["coord"].get(key)
        if value is None and isinstance(payload.get("coords"), dict):
            value = payload["coords"].get(key)
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def observe_gmcp(self, package: str, payload: Any) -> RoomObservation | None:
        if package.casefold() != "room.info" or not isinstance(payload, dict):
            return None
        external_id = _text(self._first(payload, self._ID_KEYS), max_len=MAX_EXTERNAL_ROOM_ID)
        if external_id is None:
            return None
        name = _text(self._first(payload, self._NAME_KEYS), max_len=512)
        area = _text(self._first(payload, self._AREA_KEYS), max_len=256)
        exits: list[ObservedExit] = []
        raw_exits = payload.get("exits")
        if isinstance(raw_exits, dict):
            for direction, destination in list(raw_exits.items())[:MAX_OBSERVED_EXITS]:
                d = _text(direction, max_len=128)
                if not d:
                    continue
                dest_id = _text(destination, max_len=MAX_EXTERNAL_ROOM_ID)
                exits.append(ObservedExit(d, dest_id, d))
        elif isinstance(raw_exits, (list, tuple)):
            for direction in list(raw_exits)[:MAX_OBSERVED_EXITS]:
                d = _text(direction, max_len=128)
                if d:
                    exits.append(ObservedExit(d, None, d))
        return RoomObservation(
            external_id=external_id,
            name=name,
            area=area,
            x=self._coord(payload, "x"),
            y=self._coord(payload, "y"),
            z=self._coord(payload, "z"),
            exits=tuple(exits),
            source=self.key,
        )

    def observe_msdp(self, variable: str, value: Any) -> RoomObservation | None:
        return None


class MgRoomGmcpAdapter:
    """Adapter for the ``MG.room.info`` GMCP shape used by LP-MUD drivers.

    This shape is based on concrete reference-client evidence (MudPyC's
    Morgengrauen/Midnight Sun drivers), not inferred from the generic adapter.
    The payload uses ``id``, ``short``, optional ``area``, and an ``exits``
    mapping of direction -> destination id.
    """

    key = "mg-room"
    label = "MG.room GMCP (LP-MUD)"

    def observe_gmcp(self, package: str, payload: Any) -> RoomObservation | None:
        if package.casefold() != "mg.room.info" or not isinstance(payload, dict):
            return None
        external_id = _text(payload.get("id"), max_len=MAX_EXTERNAL_ROOM_ID)
        if external_id is None:
            return None
        exits: list[ObservedExit] = []
        raw_exits = payload.get("exits")
        if isinstance(raw_exits, dict):
            for direction, destination in list(raw_exits.items())[:MAX_OBSERVED_EXITS]:
                d = _text(direction, max_len=128)
                if not d:
                    continue
                exits.append(
                    ObservedExit(
                        direction=d,
                        destination_external_id=_text(destination, max_len=MAX_EXTERNAL_ROOM_ID),
                        command=d,
                    )
                )
        return RoomObservation(
            external_id=external_id,
            name=_text(payload.get("short"), max_len=512),
            area=_text(payload.get("area"), max_len=256),
            exits=tuple(exits),
            source=self.key,
        )

    def observe_msdp(self, variable: str, value: Any) -> RoomObservation | None:
        return None


DEFAULT_MAPPER_ADAPTER_REGISTRY = MapperAdapterRegistry()
DEFAULT_MAPPER_ADAPTER_REGISTRY.register(
    key=ManualMapperAdapter.key,
    label=ManualMapperAdapter.label,
    factory=ManualMapperAdapter,
    capabilities=(),
    evidence="built-in manual mode",
)
DEFAULT_MAPPER_ADAPTER_REGISTRY.register(
    key=GenericGmcpRoomAdapter.key,
    label=GenericGmcpRoomAdapter.label,
    factory=GenericGmcpRoomAdapter,
    capabilities=(
        MapperAdapterCapability.ROOM_IDENTITY,
        MapperAdapterCapability.GMCP,
        MapperAdapterCapability.EXITS,
        MapperAdapterCapability.AREA,
        MapperAdapterCapability.COORDINATES,
    ),
    evidence="generic Room.Info contract",
)
DEFAULT_MAPPER_ADAPTER_REGISTRY.register(
    key=MgRoomGmcpAdapter.key,
    label=MgRoomGmcpAdapter.label,
    factory=MgRoomGmcpAdapter,
    capabilities=(
        MapperAdapterCapability.ROOM_IDENTITY,
        MapperAdapterCapability.GMCP,
        MapperAdapterCapability.EXITS,
        MapperAdapterCapability.AREA,
    ),
    evidence="MudPyC LP-MUD/Morgengrauen MG.room.info driver and payload sample",
)


def register_mapper_adapter(
    *,
    key: str,
    label: str,
    factory: AdapterFactory,
    capabilities: Iterable[MapperAdapterCapability | str] = (),
    evidence: str | None = None,
) -> MapperAdapterDescriptor:
    """Register an optional mapper adapter in the default client registry."""

    return DEFAULT_MAPPER_ADAPTER_REGISTRY.register(
        key=key,
        label=label,
        factory=factory,
        capabilities=capabilities,
        evidence=evidence,
    )


def adapter_choices() -> tuple[tuple[str, str], ...]:
    return DEFAULT_MAPPER_ADAPTER_REGISTRY.choices()


def mapper_adapter_descriptor(key: str) -> MapperAdapterDescriptor:
    return DEFAULT_MAPPER_ADAPTER_REGISTRY.descriptor(key)


def mapper_adapter_capabilities(key: str) -> frozenset[MapperAdapterCapability]:
    return mapper_adapter_descriptor(key).capabilities


def create_mapper_adapter(key: str) -> MapperAdapter:
    return DEFAULT_MAPPER_ADAPTER_REGISTRY.create(key)
