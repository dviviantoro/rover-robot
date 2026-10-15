"""The Knowledge page's "Document" view, and what it is allowed to open.

`CoreService._raw_document` reads a file because a browser asked for a source name the
store holds. Its docstring says exactly that -- and `_document` used to call it for ANY
name it was sent, held or not, so `{"type": "document", "source": "/some/path/.env"}`
came back with the file. The check that makes the docstring true is that something is
stored under the name.
"""
from __future__ import annotations

import json

import pytest

from robot.core.app import CoreService


class FakeStore:
    """Only what `_document` asks of the store: `document(source, limit)`."""

    def __init__(self, held: dict[str, list[str]]) -> None:
        self.held = held

    def document(self, source: str, limit: int = 400) -> dict:
        texts = self.held.get(source, [])
        chunks = [{"id": i, "text": t, "lang": "id", "added_at": 1.0, "chars": len(t)}
                  for i, t in enumerate(texts)]
        return {"source": source, "chunks": chunks, "total_chunks": len(texts),
                "truncated": False, "chars": sum(len(t) for t in texts),
                "languages": ["id"] if texts else [], "added_first": None,
                "added_last": None, "terms": [], "terms_basis": "frequent"}


def service(held: dict[str, list[str]]) -> CoreService:
    svc = CoreService.__new__(CoreService)
    svc.store = FakeStore(held)
    return svc


class TestItOnlyOpensWhatTheStoreHolds:
    async def test_a_path_the_store_does_not_hold_is_not_read(self, tmp_path):
        secret = tmp_path / "secret.env"
        secret.write_text("CORE_WS_TOKEN=do-not-return-this\n")
        out = await service({})._document(str(secret))
        assert out["error"] == "no such source"
        assert "do-not-return-this" not in json.dumps(out)

    async def test_a_relative_traversal_is_not_read_either(self, tmp_path, monkeypatch):
        (tmp_path / "knowledge").mkdir()
        (tmp_path / "outside.txt").write_text("outside the knowledge folder")
        monkeypatch.chdir(tmp_path)
        out = await service({})._document("../outside.txt")
        assert out["error"] == "no such source"
        assert "outside the knowledge folder" not in json.dumps(out)

    async def test_a_held_source_is_shown_from_its_file_when_there_is_one(
            self, tmp_path, monkeypatch):
        (tmp_path / "knowledge").mkdir()
        (tmp_path / "knowledge" / "notes.txt").write_text("the whole original file")
        monkeypatch.chdir(tmp_path)
        out = await service({"notes.txt": ["chunk one", "chunk two"]})._document("notes.txt")
        assert out["type"] == "document"
        assert out["raw_from"] == "file"
        assert out["raw"] == "the whole original file"
        assert out["total_chunks"] == 2

    async def test_a_held_source_with_no_file_is_rebuilt_from_its_chunks(
            self, tmp_path, monkeypatch):
        """Typed-in facts and uploads have no file. `raw_from` says so, because a
        rebuilt document has lost whatever the splitter dropped."""
        monkeypatch.chdir(tmp_path)
        out = await service({"spoken": ["a fact", "another fact"]})._document("spoken")
        assert out["raw_from"] == "chunks"
        assert out["raw"] == "a fact\n\nanother fact"

    @pytest.mark.parametrize("source", [None, ""])
    async def test_no_source_is_an_error_not_a_crash(self, source):
        out = await service({})._document(source)
        assert out["error"] == "no such source"
