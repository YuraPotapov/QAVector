"""Core tests never write where a person's data lives.

A step given no memory path uses the default store, and for a checkout the
default is the checkout itself - so an approval gate under test that nobody
answered put its entry on the plan in the repository, as a cyclememory.json
nobody asked for. Every test gets a store of its own instead.
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "real_memory_default: a test of where the "
                            "default memory store is, which must see the real one")


@pytest.fixture(autouse=True)
def own_memory_store(request, tmp_path, monkeypatch):
    from cycle import memory

    if request.node.get_closest_marker("real_memory_default"):
        return None

    store = str(tmp_path / "default-cyclememory.json")
    monkeypatch.setattr(memory, "default_path", lambda: store)
    return store
