from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from orchestra_agent.mcp_server.file_config import (
    FileServerConfig,
    FileSourceProfile,
    ManifestAlias,
)
from orchestra_agent.mcp_server.file_service import FileToolError, WorkspaceFileService


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


def test_find_items_filters_extensions(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / "docs").mkdir()
    (sandbox_dir / "docs" / "spec.md").write_text("# Spec", encoding="utf-8")
    (sandbox_dir / "docs" / "notes.txt").write_text("notes", encoding="utf-8")
    (sandbox_dir / "docs" / "data.json").write_text("{}", encoding="utf-8")

    result = service.find_items(
        "local_workspace",
        parent="docs",
        recursive=True,
        item_types=["file"],
        extension_filter=[".md", ".txt"],
    )

    assert [item["item_ref"]["local_path"] for item in result["items"]] == [
        "docs/notes.txt",
        "docs/spec.md",
    ]


def test_read_text_item_detects_shift_jis(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    target = sandbox_dir / "sjis.txt"
    target.write_bytes("こんにちは".encode("cp932"))

    result = service.read_text_item("sjis.txt")

    assert result["content"] == "こんにちは"
    assert result["encoding"] in {"cp932", "shift_jis"}


def test_text_edit_session_commit_creates_backup(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    target = sandbox_dir / "docs" / "readme.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Title\nold line\n", encoding="utf-8")

    opened = service.open_text_edit_session("docs/readme.md")
    session_id = str(opened["session_id"])
    service.stage_patch_text(
        session_id,
        patch_type="exact_replace",
        operations=[{"search": "old line", "replace": "new line"}],
    )

    preview = service.preview_file_edit_session(session_id)
    validation = service.validate_file_edit_session(session_id)
    committed = service.commit_file_edit_session(session_id)

    assert preview["preview"]["text_diff_preview"][0]["line_modifications"] >= 1
    assert validation["valid"] is True
    assert target.read_text(encoding="utf-8") == "# Title\nnew line\n"
    assert committed["backup_ref"] is not None
    assert Path(committed["backup_ref"]["backup_path"]).name.endswith(".bak")


def test_commit_detects_conflict(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    target = sandbox_dir / "conflict.txt"
    target.write_text("before", encoding="utf-8")

    opened = service.open_text_edit_session("conflict.txt")
    session_id = str(opened["session_id"])
    service.stage_replace_text(session_id, "after")
    service.preview_file_edit_session(session_id)
    target.write_text("external change", encoding="utf-8")

    with pytest.raises(FileToolError) as exc_info:
        service.commit_file_edit_session(session_id, require_validated=False)

    assert exc_info.value.code == "CONFLICT_DETECTED"


def test_secret_like_file_read_is_denied(sandbox_dir: Path) -> None:
    service = WorkspaceFileService(sandbox_dir)
    (sandbox_dir / ".env").write_text("TOKEN=secret", encoding="utf-8")

    with pytest.raises(FileToolError) as exc_info:
        service.read_text_item(".env")

    assert exc_info.value.code == "PERMISSION_DENIED"


def test_remote_manifest_search_and_alias_resolve(
    sandbox_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"etag": '"etag-1"', "content": "# Title\nold line\n", "uploads": []}
    service = _build_remote_service(
        sandbox_dir,
        monkeypatch=monkeypatch,
        state=state,
    )

    resolved = service.resolve_item("sp_team_docs", alias="team_specs")
    found = service.find_items(
        "sp_team_docs",
        query="spec",
        recursive=True,
        item_types=["file"],
    )

    assert resolved["item_ref"]["remote_path"] == "/Architecture/Specs"
    assert found["items"][0]["item_ref"]["remote_path"] == "/Architecture/Specs/spec.md"


def test_remote_roundtrip_commit_uses_etag(
    sandbox_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"etag": '"etag-1"', "content": "# Title\nold line\n", "uploads": []}
    service = _build_remote_service(
        sandbox_dir,
        monkeypatch=monkeypatch,
        state=state,
    )

    item_ref = service.resolve_item(
        "sp_team_docs",
        path="/Architecture/Specs/spec.md",
    )["item_ref"]
    opened = service.open_text_edit_session(item_ref)
    session_id = str(opened["session_id"])
    service.stage_patch_text(
        session_id,
        patch_type="exact_replace",
        operations=[{"search": "old line", "replace": "new line"}],
    )

    preview = service.preview_file_edit_session(session_id)
    validation = service.validate_file_edit_session(session_id)
    committed = service.commit_file_edit_session(session_id)

    assert preview["preview"]["risk_preview"]["risk_flags"] == ["overwrites_existing_file"]
    assert validation["valid"] is True
    assert committed["final_item_ref"]["etag"] == '"etag-2"'
    assert state["content"] == "# Title\nnew line\n"
    assert state["uploads"] == [
        {
            "path": "/Architecture/Specs/spec.md",
            "if_match": '"etag-1"',
            "content": "# Title\nnew line\n",
        }
    ]


def test_remote_validation_detects_etag_mismatch(
    sandbox_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"etag": '"etag-1"', "content": "# Title\nold line\n", "uploads": []}
    service = _build_remote_service(
        sandbox_dir,
        monkeypatch=monkeypatch,
        state=state,
    )

    item_ref = service.resolve_item(
        "sp_team_docs",
        path="/Architecture/Specs/spec.md",
    )["item_ref"]
    opened = service.open_text_edit_session(item_ref)
    session_id = str(opened["session_id"])
    service.stage_replace_text(session_id, "# Title\nremote draft\n")
    state["etag"] = '"etag-2"'

    validation = service.validate_file_edit_session(session_id)

    assert validation["valid"] is False
    assert validation["errors"][0]["code"] == "ETAG_MISMATCH"


def _build_remote_service(
    sandbox_dir: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, object],
) -> WorkspaceFileService:
    monkeypatch.setenv("FILE_MCP_REMOTE_ENABLED", "true")
    config = FileServerConfig(
        sources=(
            FileSourceProfile(
                source_id="local_workspace",
                source_type="local_workspace",
                display_name="Local Workspace",
                workspace_root=sandbox_dir,
                temp_root=sandbox_dir / ".file_mcp_tmp",
                backup_dir=sandbox_dir / ".file_mcp_backups",
            ),
            FileSourceProfile(
                source_id="sp_team_docs",
                source_type="sharepoint_drive",
                display_name="Team SharePoint Docs",
                enabled=True,
                read_only=False,
                site_url="https://contoso.sharepoint.com/sites/TeamA",
                library_name="Shared Documents",
                auth_profile="graph_app_selected",
                search_mode="manifest_only",
                write_mode="remote_roundtrip",
                allowed_extensions=(".txt", ".md", ".json", ".yaml"),
                temp_root=sandbox_dir / ".file_mcp_tmp" / "sp_team_docs",
                backup_dir=sandbox_dir / ".file_mcp_backups" / "sp_team_docs",
            ),
        ),
        auth_profiles=(
            {
                "auth_profile_id": "graph_app_selected",
                "auth_mode": "client_credentials",
                "tenant_id": "tenant-1",
                "client_id": "client-1",
                "client_secret": "secret",
            },
        ),
        aliases=(
            ManifestAlias(
                alias="team_specs",
                source_id="sp_team_docs",
                base_folder_path="/Architecture/Specs",
            ),
        ),
    )
    transport = httpx.MockTransport(_graph_transport_handler(state))
    return WorkspaceFileService(
        sandbox_dir,
        config=config,
        graph_transport=transport,
    )


def _graph_transport_handler(state: dict[str, object]):  # noqa: C901
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if path == "/v1.0/sites/contoso.sharepoint.com:/sites/TeamA":
            return httpx.Response(
                200,
                json={
                    "id": "site-1",
                    "webUrl": "https://contoso.sharepoint.com/sites/TeamA",
                },
            )
        if path == "/v1.0/sites/site-1/drives":
            return httpx.Response(
                200,
                json={"value": [{"id": "drive-1", "name": "Shared Documents"}]},
            )
        if path == "/v1.0/drives/drive-1/root:/Architecture/Specs":
            return httpx.Response(
                200,
                json={
                    "id": "folder-specs",
                    "name": "Specs",
                    "folder": {},
                    "size": 0,
                    "eTag": '"etag-folder"',
                    "lastModifiedDateTime": "2026-03-12T00:00:00Z",
                    "parentReference": {"path": "/drives/drive-1/root:/Architecture"},
                },
            )
        if path == "/v1.0/drives/drive-1/root:/Architecture/Specs/spec.md":
            content = str(state["content"])
            return httpx.Response(
                200,
                json={
                    "id": "item-file",
                    "name": "spec.md",
                    "file": {"mimeType": "text/markdown"},
                    "size": len(content.encode("utf-8")),
                    "eTag": state["etag"],
                    "lastModifiedDateTime": "2026-03-12T00:00:00Z",
                    "parentReference": {"path": "/drives/drive-1/root:/Architecture/Specs"},
                },
            )
        if path == "/v1.0/drives/drive-1/items/folder-specs/children":
            content = str(state["content"])
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "item-file",
                            "name": "spec.md",
                            "file": {"mimeType": "text/markdown"},
                            "size": len(content.encode("utf-8")),
                            "eTag": state["etag"],
                            "lastModifiedDateTime": "2026-03-12T00:00:00Z",
                            "parentReference": {"path": "/drives/drive-1/root:/Architecture/Specs"},
                        }
                    ]
                },
            )
        if path == "/v1.0/drives/drive-1/items/item-file":
            content = str(state["content"])
            return httpx.Response(
                200,
                json={
                    "id": "item-file",
                    "name": "spec.md",
                    "file": {"mimeType": "text/markdown"},
                    "size": len(content.encode("utf-8")),
                    "eTag": state["etag"],
                    "lastModifiedDateTime": "2026-03-12T00:00:00Z",
                    "parentReference": {"path": "/drives/drive-1/root:/Architecture/Specs"},
                },
            )
        if path == "/v1.0/drives/drive-1/items/item-file/content":
            return httpx.Response(200, content=str(state["content"]).encode("utf-8"))
        if path == "/v1.0/drives/drive-1/root:/Architecture/Specs/spec.md:/content":
            uploads = state["uploads"]
            assert isinstance(uploads, list)
            uploads.append(
                {
                    "path": "/Architecture/Specs/spec.md",
                    "if_match": request.headers.get("If-Match"),
                    "content": request.content.decode("utf-8"),
                }
            )
            state["content"] = request.content.decode("utf-8")
            state["etag"] = '"etag-2"'
            return httpx.Response(
                200,
                json={
                    "id": "item-file",
                    "name": "spec.md",
                    "file": {"mimeType": "text/markdown"},
                    "size": len(request.content),
                    "eTag": state["etag"],
                    "lastModifiedDateTime": "2026-03-12T00:05:00Z",
                    "parentReference": {"path": "/drives/drive-1/root:/Architecture/Specs"},
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    return handler
