import copy

import pytest

from qcr import Authority, Grant, QCRError, Registry
from qcr.adapters.services import ServiceAdapter


def snapshot():
    return {"schema_version": 1, "project_id": "outer", "observed_at": 100, "ttl_seconds": 3,
        "legacy_count": 0, "resources": [{"resource_id": "a" * 32, "group_id": "b" * 32,
        "alias": "Group/Web", "display_name": "Web", "properties": {
        "status": "starting", "runner_type": "python", "detached": False, "configuration_stale": False}}]}


def test_adapter_tracks_same_owner_changes_disconnect_and_removal():
    authority = Authority()
    caller = authority.issue("gui", [Grant("outer", "services.read")])
    registry = Registry(authority, clock=lambda: 101)
    current = snapshot()
    adapter = ServiceAdapter(registry, "outer", lambda: current)
    assert adapter.refresh()
    ref = registry.describe_resource(caller, "outer", "Group/Web").ref
    assert registry.get_properties(caller, ref)["status"].value == "starting"
    current["resources"][0]["properties"]["status"] = "running"
    assert registry.get_properties(caller, ref)["status"].value == "running"
    current["resources"][0]["alias"] = "Group/Renamed"
    adapter.refresh()
    assert registry.describe_resource(caller, "outer", "Group/Renamed").ref == ref
    current = None
    assert adapter.refresh() is False
    result = registry.get_properties(caller, ref)["status"]
    assert result.availability == "unavailable" and result.value is None
    current = dict(snapshot(), resources=[])
    adapter.refresh()
    assert registry.list_resources(caller, "outer") == []


@pytest.mark.parametrize("change", ["project", "duplicate", "secret", "identity", "status"])
def test_malformed_snapshot_cannot_replace_valid_inventory(change):
    authority = Authority()
    caller = authority.issue("gui", [Grant("outer", "services.read")])
    registry = Registry(authority)
    current = snapshot()
    adapter = ServiceAdapter(registry, "outer", lambda: current)
    adapter.refresh()
    if change == "project":
        current["project_id"] = "other"
    elif change == "duplicate":
        current["resources"].append(copy.deepcopy(current["resources"][0]))
    elif change == "secret":
        current["resources"][0]["env"] = {"TOKEN": "SECRET"}
    elif change == "identity":
        current["resources"][0]["resource_id"] = "name instead of ID"
    else:
        current["resources"][0]["properties"]["status"] = "SECRET invalid status"
    with pytest.raises(QCRError) as error:
        adapter.refresh()
    assert "SECRET" not in str(error.value)
    assert len(registry.list_resources(caller, "outer")) == 1
