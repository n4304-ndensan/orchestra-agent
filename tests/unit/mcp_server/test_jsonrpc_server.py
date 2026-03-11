from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from orchestra_agent.mcp_server.jsonrpc_server import build_tool_registry


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
    assert "open_edit_session" in names
    assert "fs_copy_file" in names


def test_tool_registry_can_create_excel_and_copy_file(workspace_dir: Path) -> None:
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
