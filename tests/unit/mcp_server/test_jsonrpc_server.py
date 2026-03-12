from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.mcp_server.jsonrpc_server import JsonRpcError, build_tool_registry


@pytest.fixture()
def workspace_dir() -> Iterator[Path]:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_tool_registry_exposes_generic_excel_and_file_tools(workspace_dir: Path) -> None:
    registry = build_tool_registry(workspace_dir, tool_group="all")
    names = [tool["name"] for tool in registry.list_tools()]

    assert "excel.create_file" in names
    assert "file.open_text_edit_session" in names
    assert "open_edit_session" in names
    assert "fs_copy_file" in names


def test_tool_registry_can_create_excel_and_use_safe_file_tools(workspace_dir: Path) -> None:
    pytest.importorskip("openpyxl")
    registry = build_tool_registry(workspace_dir, tool_group="all")

    created = registry.call_tool(
        "excel.create_file",
        {"file": "output/HelloWorld.xlsx", "sheet": "Sheet1"},
    )
    assert created["file"] == "output/HelloWorld.xlsx"
    assert (workspace_dir / "output" / "HelloWorld.xlsx").is_file()

    source = workspace_dir / "input.txt"
    source.write_text("copy-me", encoding="utf-8")
    copied = registry.call_tool(
        "fs_copy_file",
        {"source": "input.txt", "destination": "copies/input.txt"},
    )
    assert copied["copied"]["destination"] == "copies/input.txt"
    assert (workspace_dir / "copies" / "input.txt").read_text(encoding="utf-8") == "copy-me"

    opened = registry.call_tool(
        "file.open_text_edit_session",
        {"item_ref": "copies/input.txt"},
    )
    session_id = opened["session_id"]
    registry.call_tool(
        "file.stage_replace_text",
        {"session_id": session_id, "content": "copy-me-updated"},
    )
    registry.call_tool(
        "file.preview_file_edit_session",
        {"session_id": session_id},
    )
    registry.call_tool(
        "file.validate_file_edit_session",
        {"session_id": session_id},
    )
    registry.call_tool(
        "file.commit_file_edit_session",
        {"session_id": session_id},
    )

    read_back = registry.call_tool(
        "file.read_text",
        {"item_ref": "copies/input.txt"},
    )
    assert read_back["content"] == "copy-me-updated"


def test_tool_registry_maps_file_tool_errors_to_jsonrpc(workspace_dir: Path) -> None:
    registry = build_tool_registry(workspace_dir, tool_group="files")
    (workspace_dir / ".env").write_text("TOKEN=secret", encoding="utf-8")

    with pytest.raises(JsonRpcError) as exc_info:
        registry.call_tool("file.read_text", {"item_ref": ".env"})

    assert exc_info.value.code == -32011
    assert exc_info.value.data is not None
    assert exc_info.value.data["code"] == "PERMISSION_DENIED"
