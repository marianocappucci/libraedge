from libraedge.sync.api import create_sync_app
from libraedge.sync.http import HttpSyncTransport
from libraedge.sync.receiver import SyncReceiver
from libraedge.sync.worker import OutboxWorker


def test_libraedge_exports_offline_components():
    assert all((create_sync_app, HttpSyncTransport, SyncReceiver, OutboxWorker))


def test_libraedge_has_no_libracommerce_imports():
    from pathlib import Path
    source = Path(__file__).parents[1] / "libraedge"
    assert "libracommerce" not in "".join(path.read_text() for path in source.rglob("*.py"))
