"""Core tests never write where a person's data lives.

A step given no memory path uses the default store, and for a checkout the
default is the checkout itself - so an approval gate under test that nobody
answered put its entry on the plan in the repository, as a cyclememory.json
nobody asked for. Every test gets a store of its own instead.

The run's guide reads its own words from doc_labels.json in the same place, so a
user's translation would change what every guide test sees: it gets a path of
its own too, with nothing in it.
"""

import pytest


@pytest.fixture(autouse=True)
def own_plugin_catalog(tmp_path, monkeypatch):
    from plugin_host import bundled
    monkeypatch.setattr(bundled, "registry_path", lambda: str(tmp_path / "plugin-registry.sqlite3"))


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


@pytest.fixture(autouse=True)
def own_doc_labels(tmp_path, monkeypatch):
    import runtime_paths

    path = str(tmp_path / "doc_labels.json")
    monkeypatch.setattr(runtime_paths, "doc_labels_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def own_integrations_store(tmp_path, monkeypatch):
    """A test that names no connections file gets one of its own, never the
    one beside the checkout - which is where the default points in a source
    tree, and where a person's real connections would be."""
    from integrations import store

    path = str(tmp_path / "default-integrations.json")
    monkeypatch.setattr(store, "default_path", lambda: path)
    return path
