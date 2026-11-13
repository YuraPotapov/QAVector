"""Read boundary: scope, schema isolation, revocation and truthful freshness."""

from dataclasses import replace

import pytest

from qcr import (Authority, Caller, CapabilityDescriptor, Grant, PropertyDescriptor,
                 PropertySnapshot, QCRError, Registration, Registry, ResourceDescriptor, ResourceRef)


def registration(project="p", resource="one", alias="Group/Web", version=1, reader=None):
    ref = ResourceRef(project, resource, "service")
    props = (PropertyDescriptor("status", {"enum": ["running", "stopped"]}, "services.read"),
             PropertyDescriptor("private", {"type": "string"}, "services.private"))
    descriptor = ResourceDescriptor(ref, "services", "Web", "services.read", (alias,), props)
    cap = CapabilityDescriptor("services.%s.status" % resource, ref, version, "Read status",
                               {"type": "object"}, {"type": "string"}, "services.read")
    def read(ref, names):
        return {name: PropertySnapshot("running" if name == "status" else "secret", "owner.cache", 100, 5)
                for name in names}
    return Registration(descriptor, (cap,), reader or read)


@pytest.fixture
def world():
    authority = Authority()
    caller = authority.issue("gui", [Grant("p", "services.read")])
    registry = Registry(authority, clock=lambda: 102)
    return registry, authority, caller


def test_discovery_scope_and_each_read_enforce_access(world):
    registry, authority, caller = world
    registry.register(registration())
    registry.register(registration(project="other"))
    assert len(registry.list_resources(caller, "p")) == 1
    assert registry.list_resources(caller, "other") == []
    assert registry.list_capabilities(caller, "other") == []
    described = registry.describe_resource(caller, "p", "Group/Web")
    assert [p.name for p in described.properties] == ["status"]
    assert registry.get_properties(caller, described.ref)["status"].value == "running"
    with pytest.raises(QCRError, match="not available"):
        registry.describe_resource(caller, "other", "one")
    with pytest.raises(QCRError):
        registry.get_properties(caller, ResourceRef("other", "one", "service"))
    with pytest.raises(QCRError):
        registry.describe_capability(caller, "other", "services.one.status")
    authority.revoke(caller)
    for call in (lambda: registry.list_resources(caller, "p"),
                 lambda: registry.list_capabilities(caller, "p"),
                 lambda: registry.get_properties(caller, described.ref),
                 lambda: registry.describe_resource(caller, "p", "one"),
                 lambda: registry.describe_capability(caller, "p", "services.one.status")):
        with pytest.raises(QCRError) as error:
            call()
        assert error.value.code == "authorization_denied"


def test_context_cannot_self_assert_identity_or_permissions(world):
    registry, authority, _ = world
    for caller in ({"actor_id": "gui", "permissions": ["services.read"]}, Caller("gui"),
                   Authority().issue("gui", [Grant("p", "services.read")])):
        with pytest.raises(QCRError):
            registry.list_resources(caller, "p")
    empty = authority.issue("agent")
    registry.register(registration())
    assert registry.list_resources(empty, "p") == []


def test_resource_specific_grants_and_capability_permissions(world):
    registry, authority, _ = world
    one = registration()
    registry.register(replace(one, capabilities=one.capabilities + (
        replace(one.capabilities[0], capability_id="services.one.start", permission="services.start"),)))
    registry.register(registration(resource="two", alias="Group/Two"))
    caller = authority.issue("agent", [Grant("p", "services.read", "one")])
    assert [r.ref.resource_id for r in registry.list_resources(caller, "p")] == ["one"]
    assert [c.capability_id for c in registry.list_capabilities(caller, "p")] == ["services.one.status"]
    with pytest.raises(QCRError):
        registry.describe_capability(caller, "p", "services.one.start")


def test_unauthorized_property_never_calls_source(world):
    registry, _, caller = world
    calls = []
    item = registration(reader=lambda *args: calls.append(args))
    registry.register(item)
    with pytest.raises(QCRError) as error:
        registry.get_properties(caller, item.resource.ref, ["status", "private"])
    assert error.value.code == "authorization_denied"
    assert calls == []
    assert registry.get_properties(caller, item.resource.ref, []) == {}
    assert calls == []


@pytest.mark.parametrize("change,code", [
    ({}, "duplicate_resource"),
    ({"resource": "two"}, "duplicate_alias"),
    ({"resource": "two", "alias": "one"}, "duplicate_alias"),
])
def test_duplicate_registration_does_not_change_registry(world, change, code):
    registry, _, caller = world
    registry.register(registration())
    with pytest.raises(QCRError) as error:
        registry.register(registration(**change))
    assert error.value.code == code
    assert len(registry.list_resources(caller, "p")) == 1


def test_versions_and_atomic_adapter_refresh(world):
    registry, _, caller = world
    owner = object()
    item = registration()
    registry.replace(owner, [replace(item, capabilities=item.capabilities + (
        replace(item.capabilities[0], version=2),))])
    assert registry.describe_capability(caller, "p", "services.one.status").version == 2
    assert registry.describe_capability(caller, "p", "services.one.status", 1).version == 1
    with pytest.raises(QCRError):
        registry.replace(owner, [replace(item, capabilities=item.capabilities * 2)])
    assert registry.describe_capability(caller, "p", "services.one.status").version == 2
    registry.replace(owner, [replace(item, resource=replace(item.resource, aliases=("Renamed/Web",)))])
    assert registry.describe_resource(caller, "p", "Renamed/Web").ref == item.resource.ref
    with pytest.raises(QCRError):
        registry.describe_resource(caller, "p", "Group/Web")
    registry.replace(owner, [])
    assert registry.list_resources(caller, "p") == []


@pytest.mark.parametrize("schema", [
    {"type": "not-a-type"}, {"$ref": "https://invalid.example/secret"},
    {"$ref": "file:///etc/passwd"}, {"$ref": "#/$defs/missing"},
    {"properties": {"unused": {"$ref": "urn:missing"}}},
    {"$schema": "http://json-schema.org/draft-07/schema#"},
])
def test_invalid_or_unregistered_schema_rejected_before_registration(world, schema):
    registry, _, caller = world
    item = registration()
    with pytest.raises(QCRError) as error:
        registry.register(replace(item, capabilities=(replace(item.capabilities[0], input_schema=schema),)))
    assert error.value.code == "invalid_schema"
    assert registry.list_resources(caller, "p") == []


def test_local_refs_are_resolved_and_metadata_is_detached(world):
    _, authority, caller = world
    local = {"urn:qcr:test": {"type": "string"}}
    registry = Registry(authority, local_schemas=local, clock=lambda: 102)
    item = registration()
    prop = PropertyDescriptor("status", {"$ref": "urn:qcr:test"}, "services.read")
    item = replace(item, resource=replace(item.resource, properties=(prop,)))
    registry.register(item)
    local["urn:qcr:test"]["type"] = "number"
    prop.schema["$ref"] = "urn:missing"
    described = registry.describe_resource(caller, "p", "one")
    described.properties[0].schema["$ref"] = "urn:also-missing"
    assert registry.get_properties(caller, item.resource.ref)["status"].value == "running"


def test_internal_refs_and_literal_ref_data_are_not_remote_schemas(world):
    registry, _, _ = world
    item = registration()
    schema = {"$defs": {"value": {"type": "string"}}, "$ref": "#/$defs/value",
              "default": {"$ref": "https://literal.example/not-a-schema"}}
    registry.register(replace(item, capabilities=(replace(item.capabilities[0], output_schema=schema),)))


def test_capability_version_cannot_silently_change_resource(world):
    registry, _, _ = world
    registry.register(registration())
    other = registration(resource="two", alias="Group/Two", version=2)
    with pytest.raises(QCRError, match="one resource"):
        registry.register(replace(other, capabilities=(replace(other.capabilities[0], capability_id="services.one.status"),)))


def test_contract_document_uses_json_arrays(world):
    from qcr import document
    result = document(registration().resource)
    assert result["schema_version"] == 1
    assert result["aliases"] == ["Group/Web"]


@pytest.mark.parametrize("at,age,availability", [(100, None, "available"), (90, None, "stale"),
                                                (103, None, "stale"), (100, 1, "stale")])
def test_properties_expire_without_refreshing_or_making_up_state(world, at, age, availability):
    registry, _, caller = world
    item = registration(reader=lambda ref, names: {
        name: PropertySnapshot("running", "cache", at, 5) for name in names})
    registry.register(item)
    value = registry.get_properties(caller, item.resource.ref, ["status"], max_age_seconds=age)["status"]
    assert value.availability == availability
    assert value.value == ("running" if availability == "available" else None)


def test_errors_do_not_expose_source_secrets(world):
    registry, _, caller = world
    def broken(*args):
        raise RuntimeError("SECRET from command line")
    item = registration(reader=broken)
    registry.register(item)
    with pytest.raises(QCRError) as error:
        registry.get_properties(caller, item.resource.ref)
    assert error.value.code == "source_unavailable"
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("value", ["SECRET invalid status", float("nan"), object()])
def test_provider_values_must_match_registered_property_schema(world, value):
    registry, _, caller = world
    item = registration(reader=lambda ref, names: {"status": PropertySnapshot(value, "cache", 100, 5)})
    registry.register(item)
    with pytest.raises(QCRError):
        registry.get_properties(caller, item.resource.ref, ["status"])


@pytest.mark.parametrize("version", [True, 0, -1, "1"])
def test_invalid_versions_are_not_coerced(version):
    with pytest.raises(QCRError):
        registration(version=version)
