from collections.abc import Iterator
from pathlib import Path

import pytest

from nodal.events import EventStore, NetworkState, load_state
from nodal.worlds import load_world

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def world_store(tmp_path: Path) -> Iterator[EventStore]:
    store = EventStore(tmp_path / "world.sqlite3")
    load_world(FIXTURES / "world_small.yaml", store)
    yield store
    store.close()


@pytest.fixture
def world_state(world_store: EventStore) -> NetworkState:
    return load_state(world_store)
