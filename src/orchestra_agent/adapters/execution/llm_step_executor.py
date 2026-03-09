from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestra_agent.domain.step import Step
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.llm_client import (
    ILlmClient,
    LlmAttachment,
    LlmGenerateRequest,
    LlmMessage,
)
from orchestra_agent.ports.mcp_client import IMcpClient
from orchestra_agent.ports.step_executor import IStepExecutor


class LlmStepExecutor(IStepExecutor):
    """
    Executes a step through an AI-orchestrated MCP runtime loop.
    """

    _ignored_dirs = {
        ".git",
        ".mypy_cache",
        ".orchestra_snapshots",
        ".orchestra_state",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".venv-uv",
        ".uv-cache",
        "__pycache__",
    }

    def __init__(
        self,
        llm_client: ILlmClient,
        workspace_root: Path,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        max_agent_turns: int = 4,
        max_workspace_files: int = 200,
    ) -> None:
        self._llm_client = llm_client
        self._workspace_root = workspace_root.resolve()
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_agent_turns = max_agent_turns
        self._max_workspace_files = max_workspace_files

    def execute(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        available_tools = self._available_tool_catalog(mcp_client, resolved_input)
        allowed_tools = {tool["name"] for tool in available_tools}
        indexed_files = self._build_workspace_file_index(resolved_input)
        attached_files = list(self._workflow_attachments(workflow, resolved_input))
        requested_paths: list[str] = []

        for _turn in range(self._max_agent_turns):
            payload = self._build_payload(
                workflow=workflow,
                step=step,
                resolved_input=resolved_input,
                step_results=step_results,
                available_tools=available_tools,
                indexed_files=indexed_files,
                attached_files=attached_files,
                requested_paths=requested_paths,
            )
            request = LlmGenerateRequest(
                messages=(
                    LlmMessage(role="system", content=self._system_prompt()),
                    LlmMessage(
                        role="user",
                        content=json.dumps(payload, ensure_ascii=False, indent=2),
                        attachments=tuple(attached_files),
                    ),
                ),
                response_format="json_object",
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            raw = self._llm_client.generate(request)
            parsed = self._extract_json(raw)
            requested = self._consume_attachment_request(
                parsed=parsed,
                indexed_files=indexed_files,
                attached_files=attached_files,
            )
            if requested:
                requested_paths.extend(requested)
                continue
            return self._apply_actions(parsed, mcp_client, allowed_tools)

        raise RuntimeError("LLM step executor exceeded the maximum attachment request rounds.")

    def _build_payload(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        available_tools: list[dict[str, str]],
        indexed_files: list[dict[str, Any]],
        attached_files: list[LlmAttachment],
        requested_paths: list[str],
    ) -> dict[str, Any]:
        return {
            "workflow": {
                "workflow_id": workflow.workflow_id,
                "objective": workflow.objective,
                "reference_files": workflow.reference_files,
                "feedback_history": workflow.feedback_history,
            },
            "step": {
                "step_id": step.step_id,
                "name": step.name,
                "description": step.description,
                "planned_tool_ref": step.tool_ref,
                "resolved_input": resolved_input,
            },
            "step_results": step_results,
            "available_mcp_tools": available_tools,
            "workspace_root": str(self._workspace_root),
            "workspace_file_index": indexed_files,
            "attached_files": [attachment.path for attachment in attached_files],
            "requested_attachment_paths": requested_paths,
        }

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are an AI execution controller working through an MCP runtime.\n"
            "Return ONLY JSON with this shape:\n"
            '{"actions":[{"type":"call_mcp_tool","tool_ref":"...","input":{}},'
            '{"type":"request_file_attachments","paths":["relative/path.ext"],"reason":"..."},'
            '{"type":"write_file","path":"relative/path.txt","content":"..."},'
            '{"type":"set_result","result":{}}]}\n'
            "Rules:\n"
            "1) Use only the provided available_mcp_tools[].name values.\n"
            "2) Treat step.planned_tool_ref as the preferred starting tool, but you may combine "
            "multiple MCP tools when the step requires orchestration.\n"
            "3) You may call MCP tools repeatedly to search, inspect, branch, and iterate before "
            "returning the final result.\n"
            "4) If step.planned_tool_ref is orchestra.ai_review, focus on analysis and return the "
            "review result with set_result.\n"
            "5) Apply real changes through MCP tool calls and workspace writes only.\n"
            "6) If you need local file contents as true file attachments, first return ONLY "
            "request_file_attachments actions.\n"
            "7) request_file_attachments paths must come from workspace_file_index.\n"
            "8) write_file path must stay inside workspace_root.\n"
            "9) Use set_result when the final result should differ from the last tool result.\n"
            "10) Keep output valid JSON with no commentary."
        )

    @staticmethod
    def _extract_json(raw_text: str) -> Any:
        stripped = raw_text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("LLM step executor output is not valid JSON.")
        return json.loads(stripped[start : end + 1])

    def _apply_actions(
        self,
        parsed: Any,
        mcp_client: IMcpClient,
        allowed_tools: set[str],
    ) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("LLM step executor output must be an object.")

        raw_actions = parsed.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("LLM step executor 'actions' must be a list.")

        explicit_result = parsed.get("result")
        written_files: list[str] = []
        mcp_results: list[dict[str, Any]] = []
        last_mcp_result: dict[str, Any] | None = None

        for raw_action in raw_actions:
            action_type = raw_action.get("type") if isinstance(raw_action, dict) else None
            if action_type == "request_file_attachments":
                raise ValueError(
                    "request_file_attachments must be handled in a separate agent round."
                )
            explicit_result, last_mcp_result = self._apply_single_action(
                raw_action=raw_action,
                mcp_client=mcp_client,
                allowed_tools=allowed_tools,
                written_files=written_files,
                mcp_results=mcp_results,
                explicit_result=explicit_result,
                last_mcp_result=last_mcp_result,
            )

        if explicit_result is not None:
            if not isinstance(explicit_result, dict):
                raise ValueError("LLM executor result must be an object.")
            return explicit_result
        if last_mcp_result is not None:
            if written_files:
                return {
                    "last_mcp_result": last_mcp_result,
                    "written_files": written_files,
                }
            return last_mcp_result
        return {
            "written_files": written_files,
            "mcp_results": mcp_results,
        }

    def _apply_single_action(
        self,
        raw_action: Any,
        mcp_client: IMcpClient,
        allowed_tools: set[str],
        written_files: list[str],
        mcp_results: list[dict[str, Any]],
        explicit_result: Any,
        last_mcp_result: dict[str, Any] | None,
    ) -> tuple[Any, dict[str, Any] | None]:
        if not isinstance(raw_action, dict):
            raise ValueError("Each LLM action must be an object.")

        action_type = raw_action.get("type")
        if action_type == "call_mcp_tool":
            result = self._call_mcp_tool(raw_action, mcp_client, allowed_tools)
            mcp_results.append(result)
            return explicit_result, result
        if action_type == "write_file":
            written_files.append(self._write_file(raw_action))
            return explicit_result, last_mcp_result
        if action_type == "set_result":
            return raw_action.get("result"), last_mcp_result
        raise ValueError(f"Unsupported LLM action type: {action_type}")

    @staticmethod
    def _call_mcp_tool(
        raw_action: dict[str, Any],
        mcp_client: IMcpClient,
        allowed_tools: set[str],
    ) -> dict[str, Any]:
        tool_ref = raw_action.get("tool_ref")
        tool_input = raw_action.get("input", {})
        if not isinstance(tool_ref, str):
            raise ValueError("call_mcp_tool requires string 'tool_ref'.")
        if tool_ref not in allowed_tools:
            raise ValueError(f"Tool '{tool_ref}' is not in the allowed MCP tool set.")
        if not isinstance(tool_input, dict):
            raise ValueError("call_mcp_tool requires object 'input'.")
        return mcp_client.call_tool(tool_ref, tool_input)

    def _write_file(self, raw_action: dict[str, Any]) -> str:
        path = raw_action.get("path")
        content = raw_action.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("write_file requires string 'path'.")
        if not isinstance(content, str):
            raise ValueError("write_file requires string 'content'.")
        target = self._resolve_workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._workspace_root):
            raise ValueError(f"Workspace sandbox rejected path outside workspace: {raw_path}")
        return resolved

    def _available_tool_catalog(
        self,
        mcp_client: IMcpClient,
        resolved_input: dict[str, Any],
    ) -> list[dict[str, str]]:
        tool_catalog = self._describe_tools(mcp_client)
        override = resolved_input.get("allowed_mcp_tools")
        if isinstance(override, list) and all(isinstance(item, str) for item in override):
            override_set = set(override)
            filtered_catalog = [tool for tool in tool_catalog if tool["name"] in override_set]
            known_tools = {tool["name"] for tool in filtered_catalog}
            for tool_name in sorted(override_set - known_tools):
                filtered_catalog.append({"name": tool_name, "description": ""})
            return filtered_catalog
        return tool_catalog

    @staticmethod
    def _describe_tools(mcp_client: IMcpClient) -> list[dict[str, str]]:
        describe_tools = getattr(mcp_client, "describe_tools", None)
        if callable(describe_tools):
            raw_tools = describe_tools()
            described_tools: list[dict[str, str]] = []
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, dict):
                    continue
                name = raw_tool.get("name")
                if not isinstance(name, str):
                    continue
                description = raw_tool.get("description")
                described_tools.append(
                    {
                        "name": name,
                        "description": description if isinstance(description, str) else "",
                    }
                )
            if described_tools:
                return sorted(described_tools, key=lambda item: item["name"])

        return [
            {"name": tool_name, "description": ""}
            for tool_name in sorted(mcp_client.list_tools())
        ]

    def _workflow_attachments(
        self,
        workflow: Workflow,
        resolved_input: dict[str, Any],
    ) -> tuple[LlmAttachment, ...]:
        raw_files = [*workflow.reference_files]
        extra_files = resolved_input.get("llm_reference_files")
        if isinstance(extra_files, list) and all(isinstance(item, str) for item in extra_files):
            raw_files.extend(extra_files)

        seen: set[str] = set()
        attachments: list[LlmAttachment] = []
        for raw_file in raw_files:
            resolved = self._resolve_attachment_path(raw_file)
            normalized = str(resolved)
            if normalized in seen:
                continue
            seen.add(normalized)
            attachments.append(LlmAttachment(path=normalized))
        return tuple(attachments)

    def _build_workspace_file_index(self, resolved_input: dict[str, Any]) -> list[dict[str, Any]]:
        roots = self._discovery_roots(resolved_input)
        indexed_files: list[dict[str, Any]] = []
        seen: set[str] = set()

        for root in roots:
            for file_path in root.rglob("*"):
                if len(indexed_files) >= self._max_workspace_files:
                    return indexed_files
                if file_path.is_dir():
                    continue
                if self._should_ignore(file_path):
                    continue
                resolved = file_path.resolve()
                normalized = str(resolved)
                if normalized in seen:
                    continue
                seen.add(normalized)
                indexed_files.append(
                    {
                        "path": str(resolved.relative_to(self._workspace_root).as_posix()),
                        "size": resolved.stat().st_size,
                    }
                )
        return indexed_files

    def _discovery_roots(self, resolved_input: dict[str, Any]) -> list[Path]:
        raw_roots = resolved_input.get("llm_file_discovery_roots")
        if isinstance(raw_roots, list) and all(isinstance(item, str) for item in raw_roots):
            return [self._resolve_workspace_path(item) for item in raw_roots]
        return [self._workspace_root]

    def _should_ignore(self, file_path: Path) -> bool:
        relative_parts = file_path.relative_to(self._workspace_root).parts
        return any(part in self._ignored_dirs for part in relative_parts[:-1])

    def _consume_attachment_request(
        self,
        parsed: Any,
        indexed_files: list[dict[str, Any]],
        attached_files: list[LlmAttachment],
    ) -> list[str]:
        raw_actions = self._raw_actions(parsed)
        if not raw_actions:
            return []

        request_actions = self._request_actions(raw_actions)
        if not request_actions:
            return []

        indexed_paths = {
            entry["path"]
            for entry in indexed_files
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        newly_requested = self._append_requested_attachments(
            request_actions=request_actions,
            indexed_paths=indexed_paths,
            attached_files=attached_files,
        )

        if not newly_requested:
            raise ValueError("LLM requested file attachments but no new files were added.")
        return newly_requested

    @staticmethod
    def _raw_actions(parsed: Any) -> list[Any]:
        if not isinstance(parsed, dict):
            raise ValueError("LLM step executor output must be an object.")
        raw_actions = parsed.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("LLM step executor 'actions' must be a list.")
        return raw_actions

    @staticmethod
    def _request_actions(raw_actions: list[Any]) -> list[dict[str, Any]]:
        request_actions: list[dict[str, Any]] = []
        for raw_action in raw_actions:
            if not isinstance(raw_action, dict):
                raise ValueError("Each LLM action must be an object.")
            if raw_action.get("type") == "request_file_attachments":
                request_actions.append(raw_action)
                continue
            if request_actions:
                raise ValueError(
                    "request_file_attachments cannot be mixed with execution actions."
                )
            return []
        return request_actions

    def _append_requested_attachments(
        self,
        request_actions: list[dict[str, Any]],
        indexed_paths: set[str],
        attached_files: list[LlmAttachment],
    ) -> list[str]:
        existing_paths = {attachment.path for attachment in attached_files}
        newly_requested: list[str] = []

        for action in request_actions:
            paths = action.get("paths", [])
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise ValueError("request_file_attachments requires a string array 'paths'.")
            for raw_path in paths:
                normalized_path = raw_path.replace("\\", "/")
                self._validate_requested_path(normalized_path, raw_path, indexed_paths)
                resolved = self._resolve_attachment_path(normalized_path)
                attachment_path = str(resolved)
                if attachment_path in existing_paths:
                    continue
                existing_paths.add(attachment_path)
                attached_files.append(LlmAttachment(path=attachment_path))
                newly_requested.append(normalized_path)

        return newly_requested

    @staticmethod
    def _validate_requested_path(
        normalized_path: str,
        raw_path: str,
        indexed_paths: set[str],
    ) -> None:
        if normalized_path in indexed_paths:
            return
        raise ValueError(
            f"Requested attachment '{raw_path}' is not available in workspace_file_index."
        )

    def _resolve_attachment_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute() or candidate.exists():
            resolved = candidate.resolve()
        else:
            resolved = (self._workspace_root / candidate).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"LLM attachment '{resolved}' was not found.")
        return resolved
