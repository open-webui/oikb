"""Composite connector — merge multiple sources into one KB manifest."""

from __future__ import annotations

from oikb.connectors import BaseConnector, ManifestEntry


class CompositeConnector(BaseConnector):
    """Route read_file calls across connectors with a pre-merged manifest.

    Used when several .oikb.yaml sources target the same kb-id. Each source
    sync alone would drop files from the others (OWUI diff is KB-wide).
    """

    def __init__(self, parts: list[tuple[BaseConnector, list[ManifestEntry]]]):
        self._parts = parts
        self._route: dict[tuple[str, str], BaseConnector] = {}
        self._manifest: list[ManifestEntry] = []

        for connector, manifest in parts:
            for entry in manifest:
                key = (entry.path, entry.filename)
                if key in self._route:
                    raise ValueError(
                        f"Duplicate manifest path across sources: {entry.display_path}"
                    )
                self._route[key] = connector
                self._manifest.append(entry)

    def build_manifest(self) -> list[ManifestEntry]:
        return list(self._manifest)

    def read_file(self, path: str, filename: str) -> bytes:
        connector = self._route.get((path, filename))
        if not connector:
            display = f"{path}/{filename}" if path else filename
            raise FileNotFoundError(f"File not in merged manifest: {display}")
        return connector.read_file(path, filename)

    def close(self) -> None:
        closed: set[int] = set()
        for connector, _ in self._parts:
            token = id(connector)
            if token not in closed:
                connector.close()
                closed.add(token)
