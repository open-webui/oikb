import pytest


@pytest.fixture(autouse=True)
def _isolated_history_db(tmp_path, monkeypatch):
    """Redirect SyncHistory's default DB to a per-test temp file.

    run_sync creates a SyncHistory at the default location when callers don't
    pass one, so tests would otherwise read and write the developer's real
    ~/.oikb/history.db (state leaking between runs makes them non-idempotent).
    """
    import oikb.history

    monkeypatch.setattr(oikb.history, "_DEFAULT_DB", tmp_path / "history.db")
