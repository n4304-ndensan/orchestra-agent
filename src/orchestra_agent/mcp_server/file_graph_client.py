from __future__ import annotations

import mimetypes
import os
import time as time_module
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

from orchestra_agent.mcp_server.file_config import FileSourceProfile

type RemoteItemType = Literal["file", "folder"]

_GRAPH_ITEM_SELECT = (
    "id,name,size,eTag,lastModifiedDateTime,webUrl,parentReference,file,folder"
)
_SMALL_UPLOAD_THRESHOLD_BYTES = 4 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 10 * 327_680


class GraphFileClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        retriable: bool = False,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.retriable = retriable
        self.suggested_action = suggested_action


@dataclass(slots=True)
class GraphDriveItem:
    source_id: str
    item_id: str | None
    item_type: RemoteItemType
    name: str
    remote_path: str
    drive_id: str
    site_id: str | None
    etag: str | None
    size: int | None
    modified_at: str | None
    media_type: str | None
    web_url: str | None = None

    @classmethod
    def from_payload(
        cls,
        *,
        source: FileSourceProfile,
        payload: dict[str, Any],
        drive_id: str,
        site_id: str | None,
        remote_path: str | None = None,
    ) -> GraphDriveItem:
        name = str(payload.get("name") or "")
        item_type: RemoteItemType = "folder" if isinstance(payload.get("folder"), dict) else "file"
        resolved_path = remote_path or _remote_path_from_payload(payload)
        media_type = _media_type_from_payload(payload, name=name)
        return cls(
            source_id=source.source_id,
            item_id=_optional_str(payload.get("id")),
            item_type=item_type,
            name=name or PurePosixPath(resolved_path).name,
            remote_path=resolved_path,
            drive_id=drive_id,
            site_id=site_id,
            etag=_optional_str(payload.get("eTag")),
            size=_optional_int(payload.get("size")),
            modified_at=_optional_str(payload.get("lastModifiedDateTime")),
            media_type=media_type,
            web_url=_optional_str(payload.get("webUrl")),
        )

    def to_item_ref(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "item_type": self.item_type,
            "local_path": None,
            "remote_path": self.remote_path,
            "remote_item_id": self.item_id,
            "drive_id": self.drive_id,
            "site_id": self.site_id,
            "etag": self.etag,
            "size": self.size,
            "modified_at": self.modified_at,
            "media_type": self.media_type or "application/octet-stream",
            "web_url": self.web_url,
        }


class GraphFileClient:
    def __init__(
        self,
        source: FileSourceProfile,
        auth_profile: dict[str, Any],
        *,
        transport: httpx.BaseTransport | None = None,
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        timeout_sec: float = 30.0,
    ) -> None:
        self._source = source
        self._auth_profile = auth_profile
        self._graph_base_url = graph_base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_sec,
            follow_redirects=True,
        )
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._site_id: str | None = source.site_id
        self._drive_id: str | None = source.drive_id

    def resolve_item(  # noqa: C901
        self,
        *,
        remote_path: str | None = None,
        item_id: str | None = None,
        drive_id: str | None = None,
        allow_missing: bool = False,
        force_item_type: RemoteItemType | None = None,
    ) -> GraphDriveItem:
        effective_drive_id = drive_id or self.drive_id()
        effective_site_id = self.site_id()
        normalized_path = _normalize_remote_path(remote_path)
        if item_id is not None:
            payload = self._request_json(
                "GET",
                f"/drives/{effective_drive_id}/items/{quote(item_id, safe='')}",
                params={"$select": _GRAPH_ITEM_SELECT},
            )
            return GraphDriveItem.from_payload(
                source=self._source,
                payload=payload,
                drive_id=effective_drive_id,
                site_id=effective_site_id,
            )
        if normalized_path is None:
            raise GraphFileClientError(
                "ITEM_NOT_FOUND",
                "remote_path or item_id is required for remote resolution.",
            )
        path_endpoint = _graph_path_endpoint(normalized_path)
        try:
            payload = self._request_json(
                "GET",
                f"/drives/{effective_drive_id}{path_endpoint}",
                params={"$select": _GRAPH_ITEM_SELECT},
            )
        except GraphFileClientError as exc:
            if exc.code != "ITEM_NOT_FOUND" or not allow_missing:
                raise
            item_type = force_item_type or (
                "folder" if normalized_path == "/" else _path_item_type(normalized_path)
            )
            return GraphDriveItem(
                source_id=self._source.source_id,
                item_id=None,
                item_type=item_type,
                name=PurePosixPath(normalized_path).name if normalized_path != "/" else "",
                remote_path=normalized_path,
                drive_id=effective_drive_id,
                site_id=effective_site_id,
                etag=None,
                size=None,
                modified_at=None,
                media_type=_media_type_from_name(PurePosixPath(normalized_path).name),
            )
        return GraphDriveItem.from_payload(
            source=self._source,
            payload=payload,
            drive_id=effective_drive_id,
            site_id=effective_site_id,
            remote_path=normalized_path,
        )

    def list_children(
        self,
        folder: GraphDriveItem,
        *,
        recursive: bool = False,
        limit: int = 1000,
        include_hidden: bool = False,
    ) -> list[GraphDriveItem]:
        if folder.item_type != "folder":
            raise GraphFileClientError(
                "ITEM_TYPE_MISMATCH",
                "Remote folder listing requires a folder item.",
            )
        if folder.item_id is None:
            folder = self.resolve_item(
                remote_path=folder.remote_path,
                allow_missing=False,
                force_item_type="folder",
            )
        results: list[GraphDriveItem] = []
        queue: list[GraphDriveItem] = [folder]
        while queue and len(results) < limit:
            current = queue.pop(0)
            next_url = (
                f"{self._graph_base_url}/drives/{current.drive_id}/items/"
                f"{quote(str(current.item_id or ''), safe='')}/children"
                f"?$select={quote(_GRAPH_ITEM_SELECT, safe=',')}"
            )
            while next_url and len(results) < limit:
                response = self._request("GET", next_url, include_auth=True)
                payload = self._json_object(response)
                for raw_child in payload.get("value", []):
                    if not isinstance(raw_child, dict):
                        continue
                    child = GraphDriveItem.from_payload(
                        source=self._source,
                        payload=raw_child,
                        drive_id=current.drive_id,
                        site_id=current.site_id,
                    )
                    if not include_hidden and child.name.startswith("."):
                        continue
                    results.append(child)
                    if recursive and child.item_type == "folder":
                        queue.append(child)
                    if len(results) >= limit:
                        break
                next_link = payload.get("@odata.nextLink")
                next_url = str(next_link) if isinstance(next_link, str) and next_link else ""
        return results

    def find_items(  # noqa: C901
        self,
        *,
        query: str,
        base_paths: list[str],
        recursive: bool,
        item_types: set[str],
        extension_filter: set[str],
        limit: int,
    ) -> list[GraphDriveItem]:
        normalized_query = query.strip().lower()
        results: list[GraphDriveItem] = []
        for base_path in base_paths:
            if len(results) >= limit:
                break
            try:
                folder = self.resolve_item(
                    remote_path=base_path,
                    allow_missing=False,
                    force_item_type="folder",
                )
            except GraphFileClientError as exc:
                if exc.code == "ITEM_NOT_FOUND":
                    continue
                raise
            for item in self.list_children(
                folder,
                recursive=recursive,
                limit=limit - len(results),
            ):
                if item.item_type not in item_types:
                    continue
                if (
                    extension_filter
                    and item.item_type == "file"
                    and PurePosixPath(item.remote_path).suffix.lower() not in extension_filter
                ):
                    continue
                haystack = f"{item.name} {item.remote_path}".lower()
                if normalized_query and normalized_query not in haystack:
                    continue
                results.append(item)
                if len(results) >= limit:
                    break
        return results

    def read_bytes(self, item: GraphDriveItem) -> bytes:
        if item.item_type != "file":
            raise GraphFileClientError(
                "ITEM_TYPE_MISMATCH",
                "Remote text reads require a file target.",
            )
        item_id = item.item_id or self.resolve_item(remote_path=item.remote_path).item_id
        if item_id is None:
            raise GraphFileClientError("ITEM_NOT_FOUND", "Remote file id could not be resolved.")
        response = self._request(
            "GET",
            f"/drives/{item.drive_id}/items/{quote(item_id, safe='')}/content",
            include_auth=True,
        )
        return response.content

    def download_to(self, item: GraphDriveItem, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.read_bytes(item))
        return destination

    def upload_file(
        self,
        *,
        remote_path: str,
        local_path: Path,
        base_etag: str | None = None,
        overwrite: bool = False,
    ) -> GraphDriveItem:
        if not local_path.is_file():
            raise GraphFileClientError(
                "ITEM_NOT_FOUND",
                f"Local temp file '{local_path}' was not found.",
            )
        normalized_path = _normalize_remote_path(remote_path)
        if normalized_path is None or normalized_path == "/":
            raise GraphFileClientError("VALIDATION_FAILED", "remote_path must target a file.")
        if base_etag is None and not overwrite:
            try:
                self.resolve_item(remote_path=normalized_path)
            except GraphFileClientError as exc:
                if exc.code != "ITEM_NOT_FOUND":
                    raise
            else:
                raise GraphFileClientError(
                    "OVERWRITE_NOT_ALLOWED",
                    f"Remote file '{normalized_path}' already exists.",
                )
        if local_path.stat().st_size <= _SMALL_UPLOAD_THRESHOLD_BYTES:
            return self._upload_small_file(
                remote_path=normalized_path,
                local_path=local_path,
                base_etag=base_etag,
                overwrite=overwrite,
            )
        return self._upload_large_file(
            remote_path=normalized_path,
            local_path=local_path,
            base_etag=base_etag,
            overwrite=overwrite,
        )

    def site_id(self) -> str | None:
        if self._site_id is not None:
            return self._site_id
        if self._source.site_url is None:
            return None
        parsed = urlparse(self._source.site_url)
        if not parsed.hostname or not parsed.path:
            raise GraphFileClientError(
                "VALIDATION_FAILED",
                f"site_url '{self._source.site_url}' is invalid.",
            )
        payload = self._request_json(
            "GET",
            f"/sites/{parsed.hostname}:{quote(parsed.path, safe='/')}",
            params={"$select": "id,webUrl"},
        )
        self._site_id = _optional_str(payload.get("id"))
        if self._site_id is None:
            raise GraphFileClientError(
                "REMOTE_API_ERROR",
                "Graph site lookup did not return an id.",
            )
        return self._site_id

    def drive_id(self) -> str:
        if self._drive_id is not None:
            return self._drive_id
        if self._source.source_type == "onedrive_business":
            payload = self._request_json("GET", "/me/drive", params={"$select": "id"})
            self._drive_id = _optional_str(payload.get("id"))
        else:
            site_id = self.site_id()
            if site_id is None:
                raise GraphFileClientError(
                    "VALIDATION_FAILED",
                    f"Remote source '{self._source.source_id}' requires site_id or site_url.",
                )
            payload = self._request_json(
                "GET",
                f"/sites/{site_id}/drives",
                params={"$select": "id,name"},
            )
            library_name = (self._source.library_name or "").strip().lower()
            for drive in payload.get("value", []):
                if not isinstance(drive, dict):
                    continue
                if library_name and str(drive.get("name", "")).strip().lower() != library_name:
                    continue
                self._drive_id = _optional_str(drive.get("id"))
                break
        if self._drive_id is None:
            raise GraphFileClientError(
                "ITEM_NOT_FOUND",
                f"Drive could not be resolved for remote source '{self._source.source_id}'.",
                suggested_action="Configure drive_id or library_name for the remote source.",
            )
        return self._drive_id

    def _upload_small_file(
        self,
        *,
        remote_path: str,
        local_path: Path,
        base_etag: str | None,
        overwrite: bool,
    ) -> GraphDriveItem:
        headers: dict[str, str] = {}
        if base_etag is not None:
            headers["If-Match"] = base_etag
        response = self._request(
            "PUT",
            f"/drives/{self.drive_id()}{_graph_path_endpoint(remote_path)}:/content",
            include_auth=True,
            headers=headers,
            content=local_path.read_bytes(),
        )
        return GraphDriveItem.from_payload(
            source=self._source,
            payload=self._json_object(response),
            drive_id=self.drive_id(),
            site_id=self.site_id(),
            remote_path=remote_path,
        )

    def _upload_large_file(
        self,
        *,
        remote_path: str,
        local_path: Path,
        base_etag: str | None,
        overwrite: bool,
    ) -> GraphDriveItem:
        headers: dict[str, str] = {}
        if base_etag is not None:
            headers["If-Match"] = base_etag
        body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace" if overwrite else "fail",
            }
        }
        session_response = self._request(
            "POST",
            f"/drives/{self.drive_id()}{_graph_path_endpoint(remote_path)}:/createUploadSession",
            include_auth=True,
            headers=headers,
            json_body=body,
        )
        upload_url = _optional_str(self._json_object(session_response).get("uploadUrl"))
        if upload_url is None:
            raise GraphFileClientError(
                "REMOTE_API_ERROR",
                "Graph upload session did not return an uploadUrl.",
            )
        total_bytes = local_path.stat().st_size
        uploaded = 0
        with local_path.open("rb") as handle:
            while True:
                chunk = handle.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                start = uploaded
                end = uploaded + len(chunk) - 1
                response = self._request(
                    "PUT",
                    upload_url,
                    include_auth=False,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total_bytes}",
                    },
                    content=chunk,
                    allow_status={200, 201, 202},
                )
                uploaded = end + 1
                if response.status_code == 202:
                    continue
                return GraphDriveItem.from_payload(
                    source=self._source,
                    payload=self._json_object(response),
                    drive_id=self.drive_id(),
                    site_id=self.site_id(),
                    remote_path=remote_path,
                )
        raise GraphFileClientError(
            "REMOTE_API_ERROR",
            "Graph upload session ended without a completion payload.",
        )

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            path_or_url,
            include_auth=True,
            params=params,
        )
        return self._json_object(response)

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        include_auth: bool,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        allow_status: set[int] | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if include_auth:
            request_headers["Authorization"] = f"Bearer {self._access_token_value()}"
        url = path_or_url
        if not path_or_url.startswith("http://") and not path_or_url.startswith("https://"):
            url = f"{self._graph_base_url}{path_or_url}"
        response = self._client.request(
            method,
            url,
            params=params,
            headers=request_headers,
            json=json_body,
            content=content,
        )
        if allow_status and response.status_code in allow_status:
            return response
        if response.is_error:
            self._raise_for_response(response)
        return response

    def _access_token_value(self) -> str:
        now = time_module.time()
        if self._access_token is not None and now < self._access_token_expires_at - 60:
            return self._access_token
        auth_mode = str(self._auth_profile.get("auth_mode", "")).strip()
        if auth_mode == "client_credentials":
            token_payload = self._request_client_credentials_token()
        elif auth_mode == "delegated_oauth":
            token_payload = self._request_delegated_token()
        elif auth_mode == "managed_identity":
            token_payload = self._request_managed_identity_token()
        else:
            raise GraphFileClientError(
                "AUTH_REQUIRED",
                f"Unsupported auth_mode '{auth_mode}'.",
            )
        self._access_token = token_payload["access_token"]
        self._access_token_expires_at = now + token_payload["expires_in"]
        return self._access_token

    def _request_client_credentials_token(self) -> dict[str, Any]:
        tenant_id = self._required_profile_str("tenant_id")
        client_id = self._required_profile_str("client_id")
        client_secret = self._auth_profile.get("client_secret")
        if not isinstance(client_secret, str) or not client_secret.strip():
            secret_env = self._auth_profile.get("client_secret_env_var")
            if isinstance(secret_env, str) and secret_env.strip():
                client_secret = os.getenv(secret_env.strip())
        if not isinstance(client_secret, str) or not client_secret.strip():
            raise GraphFileClientError(
                "AUTH_REQUIRED",
                "Graph client secret is missing.",
                suggested_action="Set client_secret or client_secret_env_var for the auth profile.",
            )
        response = self._client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        if response.is_error:
            self._raise_for_response(response)
        payload = self._json_object(response)
        return {
            "access_token": self._required_payload_str(payload, "access_token"),
            "expires_in": _optional_int(payload.get("expires_in")) or 3600,
        }

    def _request_delegated_token(self) -> dict[str, Any]:
        access_token = self._auth_profile.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            access_token_env = self._auth_profile.get("access_token_env_var")
            if isinstance(access_token_env, str) and access_token_env.strip():
                access_token = os.getenv(access_token_env.strip())
        if not isinstance(access_token, str) or not access_token.strip():
            raise GraphFileClientError(
                "AUTH_REQUIRED",
                "Delegated OAuth access token is missing.",
                suggested_action="Set access_token or access_token_env_var for the auth profile.",
            )
        return {"access_token": access_token, "expires_in": 300}

    def _request_managed_identity_token(self) -> dict[str, Any]:
        client_id = _optional_str(self._auth_profile.get("client_id"))
        identity_endpoint = os.getenv("IDENTITY_ENDPOINT")
        if identity_endpoint:
            headers = {"X-IDENTITY-HEADER": os.getenv("IDENTITY_HEADER", "")}
            params = {
                "resource": "https://graph.microsoft.com/",
                "api-version": "2019-08-01",
            }
            if client_id is not None:
                params["client_id"] = client_id
        else:
            identity_endpoint = "http://169.254.169.254/metadata/identity/oauth2/token"
            headers = {"Metadata": "true"}
            params = {
                "resource": "https://graph.microsoft.com/",
                "api-version": "2018-02-01",
            }
            if client_id is not None:
                params["client_id"] = client_id
        response = self._client.get(identity_endpoint, params=params, headers=headers)
        if response.is_error:
            self._raise_for_response(response)
        payload = self._json_object(response)
        return {
            "access_token": self._required_payload_str(payload, "access_token"),
            "expires_in": _optional_int(payload.get("expires_in")) or 3600,
        }

    def _required_profile_str(self, key: str) -> str:
        value = self._auth_profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise GraphFileClientError(
            "AUTH_REQUIRED",
            f"Auth profile field '{key}' is required.",
        )

    @staticmethod
    def _required_payload_str(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        raise GraphFileClientError(
            "REMOTE_API_ERROR",
            f"Graph response did not include '{key}'.",
        )

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GraphFileClientError(
                "REMOTE_API_ERROR",
                "Graph response was not valid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise GraphFileClientError(
                "REMOTE_API_ERROR",
                "Graph response must be a JSON object.",
            )
        return payload

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:  # noqa: C901
        message = response.text
        detail: dict[str, Any] = {
            "status_code": response.status_code,
        }
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or message)
                error_code = str(error_payload.get("code") or "")
                if error_code:
                    detail["remote_error_code"] = error_code
        normalized_message = message.strip() or response.reason_phrase
        if response.status_code == 401:
            raise GraphFileClientError("AUTH_FAILED", normalized_message, detail=detail)
        if response.status_code == 403:
            raise GraphFileClientError("PERMISSION_DENIED", normalized_message, detail=detail)
        if response.status_code == 404:
            raise GraphFileClientError("ITEM_NOT_FOUND", normalized_message, detail=detail)
        if response.status_code == 409:
            raise GraphFileClientError("CONFLICT_DETECTED", normalized_message, detail=detail)
        if response.status_code == 412:
            raise GraphFileClientError("ETAG_MISMATCH", normalized_message, detail=detail)
        if response.status_code == 429:
            raise GraphFileClientError(
                "RATE_LIMITED",
                normalized_message,
                detail=detail,
                retriable=True,
            )
        if response.status_code >= 500:
            raise GraphFileClientError(
                "REMOTE_API_ERROR",
                normalized_message,
                detail=detail,
                retriable=True,
            )
        raise GraphFileClientError("REMOTE_API_ERROR", normalized_message, detail=detail)


def _normalize_remote_path(path: str | None) -> str | None:
    if path is None:
        return None
    stripped = path.strip()
    if stripped in {"", "."}:
        return "/"
    normalized = PurePosixPath("/" + stripped.lstrip("/")).as_posix()
    return normalized if normalized != "." else "/"


def _graph_path_endpoint(remote_path: str) -> str:
    if remote_path == "/":
        return "/root"
    encoded_path = quote(remote_path.lstrip("/"), safe="/")
    return f"/root:/{encoded_path}"


def _path_item_type(remote_path: str) -> RemoteItemType:
    return "folder" if PurePosixPath(remote_path).suffix == "" else "file"


def _remote_path_from_payload(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "")
    parent_reference = payload.get("parentReference")
    if not isinstance(parent_reference, dict):
        return f"/{name}" if name else "/"
    parent_path = parent_reference.get("path")
    if not isinstance(parent_path, str) or "root:" not in parent_path:
        return f"/{name}" if name else "/"
    root_path = parent_path.split("root:", 1)[1] or "/"
    normalized_root = _normalize_remote_path(root_path) or "/"
    if normalized_root == "/":
        return f"/{name}" if name else "/"
    return f"{normalized_root.rstrip('/')}/{name}"


def _media_type_from_payload(payload: dict[str, Any], *, name: str) -> str:
    file_payload = payload.get("file")
    if isinstance(file_payload, dict):
        mime_type = file_payload.get("mimeType")
        if isinstance(mime_type, str) and mime_type.strip():
            return mime_type
    return _media_type_from_name(name)


def _media_type_from_name(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
