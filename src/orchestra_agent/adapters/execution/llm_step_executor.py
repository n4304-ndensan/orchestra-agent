from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestra_agent.domain.step import Step
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.llm_client import ILlmClient, LlmGenerateRequest, LlmMessage
from orchestra_agent.ports.mcp_client import IMcpClient
from orchestra_agent.ports.step_executor import IStepExecutor


class LlmStepExecutor(IStepExecutor):
    """
    Executes a single orchestration step through a structured LLM action plan.
    """

    def __init__(
        self,
        llm_client: ILlmClient,
        workspace_root: Path,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> None:
        self._llm_client = llm_client
        self._workspace_root = workspace_root.resolve()
        self._temperature = temperature
        self._max_tokens = max_tokens

    def execute(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        allowed_tools = self._allowed_tools(mcp_client, resolved_input)
        payload = {
            "workflow": {
                "workflow_id": workflow.workflow_id,
                "objective": workflow.objective,
                "feedback_history": workflow.feedback_history,
            },
            "step": {
                "step_id": step.step_id,
                "name": step.name,
                "description": step.description,
                "tool_ref": step.tool_ref,
                "resolved_input": resolved_input,
            },
            "step_results": step_results,
            "available_mcp_tools": sorted(allowed_tools),
            "workspace_root": str(self._workspace_root),
        }
        request = LlmGenerateRequest(
            messages=(
                LlmMessage(role="system", content=self._system_prompt()),
                LlmMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
            ),
            response_format="json_object",
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        raw = self._llm_client.generate(request)
        parsed = self._extract_json(raw)
        return self._apply_actions(parsed, mcp_client, allowed_tools)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a step execution orchestrator.\n"
            "Return ONLY JSON with this shape:\n"
            '{"actions":[{"type":"call_mcp_tool","tool_ref":"...","input":{}},'
            '{"type":"write_file","path":"relative/path.txt","content":"..."},'
            '{"type":"set_result","result":{}}]}\n'
            "Rules:\n"
            "1) Use only the provided available_mcp_tools.\n"
            "2) write_file path must stay inside workspace_root.\n"
            "3) Use set_result when the final result should differ from the last tool result.\n"
            "4) Keep output valid JSON with no commentary."
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

    @staticmethod
    def _allowed_tools(mcp_client: IMcpClient, resolved_input: dict[str, Any]) -> set[str]:
        override = resolved_input.get("allowed_mcp_tools")
        if isinstance(override, list) and all(isinstance(item, str) for item in override):
            return set(override)
        return set(mcp_client.list_tools())
