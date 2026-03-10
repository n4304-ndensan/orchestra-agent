from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.mcp_server.file_service import WorkspaceFileService


@pytest.fixture()
def sandbox_dir() -> Iterator[Path]:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_write_and_read_text_file(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    write_result = service.write_text("notes/todo.txt", "hello")

    assert write_result["path"] == "notes/todo.txt"
    assert service.read_text("notes/todo.txt") == "hello"


def test_list_entries_sorted_directory_first(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / "dir-b").mkdir()
    (sandbox_dir / "a.txt").write_text("a", encoding="utf-8")
    (sandbox_dir / "dir-a").mkdir()

    entries = service.list_entries(".")
    names = [entry["name"] for entry in entries]
    assert names == ["dir-a", "dir-b", "a.txt"]


def test_write_without_overwrite_raises(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    service.write_text("same.txt", "v1")
    with pytest.raises(FileExistsError):
        service.write_text("same.txt", "v2")


def test_read_outside_workspace_is_blocked(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    with pytest.raises(PermissionError):
        service.read_text("../outside.txt")


def test_read_size_limit_is_enforced(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir, max_read_bytes=3)
    (sandbox_dir / "big.txt").write_text("1234", encoding="utf-8")
    with pytest.raises(ValueError):
        service.read_text("big.txt")


def test_find_entries_searches_recursively(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / "docs").mkdir()
    (sandbox_dir / "docs" / "meeting-notes.txt").write_text("hello", encoding="utf-8")
    (sandbox_dir / "archive").mkdir()
    (sandbox_dir / "archive" / "meeting-summary.txt").write_text("world", encoding="utf-8")

    result = service.find_entries("meeting", include_dirs=False)

    assert [match["path"] for match in result["matches"]] == [
        "archive/meeting-summary.txt",
        "docs/meeting-notes.txt",
    ]
    assert result["truncated"] is False


def test_grep_text_returns_line_matches(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / "notes").mkdir()
    (sandbox_dir / "notes" / "a.txt").write_text("first\nkeyword here\nlast", encoding="utf-8")
    (sandbox_dir / "notes" / "b.txt").write_text("nothing\nKEYWORD again", encoding="utf-8")

    result = service.grep_text("keyword", path="notes")

    assert result["matches"] == [
        {"path": "notes/a.txt", "line_number": 2, "line": "keyword here"},
        {"path": "notes/b.txt", "line_number": 2, "line": "KEYWORD again"},
    ]
    assert result["truncated"] is False


def test_copy_file_creates_destination_file(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    source = sandbox_dir / "input" / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("copied text", encoding="utf-8")

    copied = service.copy_file("input/source.txt", "output/copied.txt")

    assert copied["source"] == "input/source.txt"
    assert copied["destination"] == "output/copied.txt"
    assert (sandbox_dir / "output" / "copied.txt").read_text(encoding="utf-8") == "copied text"


def test_copy_file_requires_overwrite_flag(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / "a.txt").write_text("v1", encoding="utf-8")
    (sandbox_dir / "b.txt").write_text("v2", encoding="utf-8")

    with pytest.raises(FileExistsError):
        service.copy_file("a.txt", "b.txt")
