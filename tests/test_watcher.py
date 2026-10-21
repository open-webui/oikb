from unittest.mock import Mock

from watchdog.events import (
    FileClosedEvent,
    FileClosedNoWriteEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from oikb.watcher import _DebouncedHandler


def handler():
    return _DebouncedHandler(Mock(), debounce_seconds=60)


def test_read_only_access_does_not_schedule_sync(tmp_path):
    h = handler()
    path = str(tmp_path / "doc.txt")

    h.dispatch(FileOpenedEvent(path))
    h.dispatch(FileClosedNoWriteEvent(path))

    assert h._timer is None


def test_changes_schedule_sync(tmp_path):
    path = str(tmp_path / "doc.txt")
    events = [
        FileCreatedEvent(path),
        FileModifiedEvent(path),
        FileDeletedEvent(path),
        FileMovedEvent(path, str(tmp_path / "moved.txt")),
        FileClosedEvent(path),
    ]
    for event in events:
        h = handler()
        h.dispatch(event)
        assert h._timer is not None, event.event_type
        h._timer.cancel()
