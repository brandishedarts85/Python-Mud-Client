import pytest

from mapper_adapter import (
    GenericGmcpRoomAdapter,
    ManualMapperAdapter,
    MapperAdapterCapability,
    MapperAdapterRegistry,
    MgRoomGmcpAdapter,
    adapter_choices,
    mapper_adapter_capabilities,
)


def test_generic_gmcp_room_info_normalizes_identity_coords_and_exits():
    adapter = GenericGmcpRoomAdapter()
    obs = adapter.observe_gmcp("Room.Info", {
        "num": 42,
        "name": "Market Square",
        "area": "Ankh-Morpork",
        "coord": {"x": "10", "y": 20, "z": 0},
        "exits": {"north": 43, "south": 41},
    })
    assert obs is not None
    assert obs.external_id == "42"
    assert obs.name == "Market Square"
    assert (obs.x, obs.y, obs.z) == (10, 20, 0)
    assert [(e.direction, e.destination_external_id) for e in obs.exits] == [("north", "43"), ("south", "41")]


def test_generic_adapter_requires_stable_room_identity():
    adapter = GenericGmcpRoomAdapter()
    assert adapter.observe_gmcp("Room.Info", {"name": "Same Name Everywhere"}) is None
    assert adapter.observe_gmcp("Char.Vitals", {"id": "x"}) is None


def test_manual_adapter_never_observes_protocol():
    adapter = ManualMapperAdapter()
    assert adapter.observe_gmcp("Room.Info", {"id": 1}) is None
    assert adapter.observe_msdp("ROOMVNUM", 1) is None


def test_mg_room_adapter_uses_evidenced_payload_shape():
    adapter = MgRoomGmcpAdapter()
    obs = adapter.observe_gmcp("MG.room.info", {
        "id": "5833",
        "short": "Tower of Light",
        "area": "Manetheren",
        "exits": {"up": 6306, "south": 6302, "west": 6294},
    })
    assert obs is not None
    assert obs.external_id == "5833"
    assert obs.name == "Tower of Light"
    assert obs.area == "Manetheren"
    assert [(e.direction, e.destination_external_id, e.command) for e in obs.exits] == [
        ("up", "6306", "up"),
        ("south", "6302", "south"),
        ("west", "6294", "west"),
    ]
    assert adapter.observe_gmcp("Room.Info", {"id": "5833"}) is None
    assert adapter.observe_gmcp("MG.room.info", {"short": "No stable id"}) is None


def test_default_registry_exposes_capabilities_without_magic_adapter_names():
    choices = dict(adapter_choices())
    assert choices["manual"] == "Manual only"
    assert choices["generic-gmcp"] == "Generic GMCP Room.Info"
    assert choices["mg-room"] == "MG.room GMCP (LP-MUD)"
    assert MapperAdapterCapability.ROOM_IDENTITY not in mapper_adapter_capabilities("manual")
    generic = mapper_adapter_capabilities("generic-gmcp")
    assert MapperAdapterCapability.ROOM_IDENTITY in generic
    assert MapperAdapterCapability.GMCP in generic
    assert MapperAdapterCapability.COORDINATES in generic
    mg = mapper_adapter_capabilities("mg-room")
    assert MapperAdapterCapability.ROOM_IDENTITY in mg
    assert MapperAdapterCapability.GMCP in mg
    assert MapperAdapterCapability.COORDINATES not in mg


def test_registry_supports_validated_optional_registration_and_duplicate_protection():
    registry = MapperAdapterRegistry()

    class OptionalAdapter(ManualMapperAdapter):
        key = "optional.test"
        label = "Optional test adapter"

    desc = registry.register(
        key=OptionalAdapter.key,
        label=OptionalAdapter.label,
        factory=OptionalAdapter,
        capabilities=(MapperAdapterCapability.GMCP,),
        evidence="test evidence",
    )
    assert desc.capabilities == frozenset({MapperAdapterCapability.GMCP})
    assert registry.create("optional.test").key == "optional.test"
    assert registry.choices() == (("optional.test", "Optional test adapter"),)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(key=OptionalAdapter.key, label=OptionalAdapter.label, factory=OptionalAdapter)


def test_registry_rejects_bad_identity_bad_keys_and_unknown_capabilities():
    registry = MapperAdapterRegistry()

    class WrongIdentity(ManualMapperAdapter):
        key = "actual"
        label = "Actual"

    with pytest.raises(ValueError, match="invalid mapper adapter key"):
        registry.register(key="Bad Key!", label="Bad", factory=WrongIdentity)
    with pytest.raises(ValueError, match="factory key mismatch"):
        registry.register(key="declared", label="Actual", factory=WrongIdentity)
    with pytest.raises(ValueError, match="unknown mapper adapter capability"):
        registry.register(key="actual", label="Actual", factory=WrongIdentity, capabilities=("telepathy",))
