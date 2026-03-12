from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra_agent.domain.step import Step
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.domain.serialization import workflow_to_dict
from orchestra_agent.observability import enrich_observation_event
from orchestra_agent.ports.audit_logger import IAuditLogger
from orchestra_agent.ports.llm_client import (
    ILlmClient,
    LlmAttachment,
    LlmGenerateRequest,
    LlmMessage,
)
from orchestra_agent.ports.mcp_client import IMcpClient
from orchestra_agent.ports.step_executor import IStepExecutor
from orchestra_agent.shared.error_handling import clean_exception_message, text_preview
from orchestra_agent.shared.llm_json import extract_json_payload
from orchestra_agent.shared.llm_prompting import (
    LlmLanguage,
    build_runtime_error_feedback,
    build_system_prompt,
)
from orchestra_agent.shared.llm_step_runtime_protocol import (
    STEP_RUNTIME_PROTOCOL_VERSION,
    CallMcpToolAction,
    FinishAction,
    RequestFileAttachmentsAction,
    WriteFileAction,
    parse_runtime_action,
)
from orchestra_agent.shared.tool_input_normalization import normalize_tool_input
from .llm_step_executor_support import (
    McpToolCatalogResolver,
    WorkspaceFileInventory,
    WorkspacePathManager,
)


@dataclass(slots=True)
class _ExecutionState:
    indexed_files: list[dict[str, Any]]
    attached_files: list[LlmAttachment]
    requested_paths: list[str]
    messages: list[LlmMessage]
    written_files: list[str] = field(default_factory=list)
    mcp_results: list[dict[str, Any]] = field(default_factory=list)
    last_mcp_result: dict[str, Any] | None = None
    last_error: Exception | None = None


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
    _path_value_keys = {
        "destination",
        "destination_path",
        "file",
        "file_path",
        "final_output",
        "output",
        "output_file",
        "path",
        "save_as",
        "source",
        "source_file",
        "target",
        "target_file",
        "workbook",
        "workbook_path",
    }
    _path_list_keys = {
        "attached_files",
        "input_files",
        "paths",
        "requested_attachment_paths",
        "reference_files",
        "source_files",
        "target_files",
        "output_files",
    }

    def __init__(
        self,
        llm_client: ILlmClient,
        workspace_root: Path,
        language: LlmLanguage = "en",
        remembers_context: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        max_agent_turns: int = 10,
        max_workspace_files: int = 200,
        audit_logger: IAuditLogger | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._workspace_root = workspace_root.resolve()
        self._language = language
        self._remembers_context = remembers_context
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_agent_turns = max_agent_turns
        self._audit_logger = audit_logger
        self._workspace_paths = WorkspacePathManager(
            workspace_root=self._workspace_root,
            path_value_keys=self._path_value_keys,
            path_list_keys=self._path_list_keys,
        )
        self._tool_catalog_resolver = McpToolCatalogResolver()
        self._workspace_inventory = WorkspaceFileInventory(
            workspace_root=self._workspace_root,
            max_files=max_workspace_files,
            ignored_dirs=self._ignored_dirs,
            path_resolver=self._workspace_paths.resolve_workspace_path,
        )

    def execute(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        available_tools, tool_catalog_warning = self._tool_catalog_resolver.resolve(
            mcp_client,
            resolved_input,
        )
        allowed_tools = {tool["name"] for tool in available_tools}
        state = self._initialize_execution_state(
            workflow=workflow,
            step=step,
            resolved_input=resolved_input,
            step_results=step_results,
            available_tools=available_tools,
            tool_catalog_warning=tool_catalog_warning,
        )

        for turn_index in range(self._max_agent_turns):
            request = LlmGenerateRequest(
                messages=self._request_messages(state.messages, turn_index=turn_index),
                response_format="json_object",
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            raw = self._llm_client.generate(request)
            parsed: Any | None = None
            try:
                parsed = self._extract_json(raw)
                state.messages.append(
                    LlmMessage(role="assistant", content=self._json_text(parsed))
                )
                if self._is_legacy_action_payload(parsed):
                    result = self._handle_legacy_payload(
                        parsed=parsed,
                        workflow=workflow,
                        step=step,
                        resolved_input=resolved_input,
                        state=state,
                        mcp_client=mcp_client,
                        allowed_tools=allowed_tools,
                    )
                    if result is None:
                        continue
                    return result

                action = parse_runtime_action(parsed)
                result = self._apply_runtime_action(
                    action=action,
                    workflow=workflow,
                    step=step,
                    resolved_input=resolved_input,
                    state=state,
                    mcp_client=mcp_client,
                    allowed_tools=allowed_tools,
                )
                if result is not None:
                    return result
            except Exception as exc:  # noqa: BLE001
                self._handle_runtime_failure(
                    exc,
                    raw_response=raw,
                    parsed=parsed,
                    state=state,
                )
                continue

        if state.last_error is not None:
            reason = text_preview(clean_exception_message(state.last_error))
            raise RuntimeError(
                "LLM step executor exceeded the maximum execution rounds after runtime corrections. "
                f"Last error: {reason}"
            ) from state.last_error
        raise RuntimeError("LLM step executor exceeded the maximum execution rounds.")

    def _initialize_execution_state(
        self,
        *,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        available_tools: list[dict[str, Any]],
        tool_catalog_warning: str | None,
    ) -> _ExecutionState:
        indexed_files = self._refresh_workspace_index(resolved_input)
        attached_files = list(self._workspace_paths.workflow_attachments(workflow, resolved_input))
        requested_paths: list[str] = []
        messages = [
            LlmMessage(role="system", content=self._system_prompt()),
            LlmMessage(
                role="user",
                content=self._json_text(
                    self._build_payload(
                        workflow=workflow,
                        step=step,
                        resolved_input=resolved_input,
                        step_results=step_results,
                        available_tools=available_tools,
                        tool_catalog_warning=tool_catalog_warning,
                        indexed_files=indexed_files,
                        attached_files=attached_files,
                        requested_paths=requested_paths,
                    )
                ),
                attachments=tuple(attached_files),
            ),
        ]
        return _ExecutionState(
            indexed_files=indexed_files,
            attached_files=attached_files,
            requested_paths=requested_paths,
            messages=messages,
        )

    def _handle_legacy_payload(
        self,
        *,
        parsed: Any,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        state: _ExecutionState,
        mcp_client: IMcpClient,
        allowed_tools: set[str],
    ) -> dict[str, Any] | None:
        if self._handle_attachment_payload(
            parsed=parsed,
            workflow=workflow,
            step=step,
            resolved_input=resolved_input,
            state=state,
        ):
            return None
        return self._apply_actions(parsed, mcp_client, allowed_tools)

    def _apply_runtime_action(
        self,
        *,
        action: RequestFileAttachmentsAction | FinishAction | CallMcpToolAction | WriteFileAction,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        state: _ExecutionState,
        mcp_client: IMcpClient,
        allowed_tools: set[str],
    ) -> dict[str, Any] | None:
        if isinstance(action, RequestFileAttachmentsAction):
            self._apply_attachment_request(
                workflow=workflow,
                step=step,
                resolved_input=resolved_input,
                request_actions=[
                    {
                        "paths": action.paths,
                        **(
                            {"reason": action.reason}
                            if isinstance(action.reason, str)
                            else {}
                        ),
                        **action.extensions,
                    }
                ],
                state=state,
            )
            return None
        if isinstance(action, FinishAction):
            return self._build_execution_result(
                explicit_result=action.result,
                last_mcp_result=state.last_mcp_result,
                written_files=state.written_files,
                mcp_results=state.mcp_results,
            )
        if isinstance(action, CallMcpToolAction):
            tool_ref, normalized_input = self._prepare_mcp_tool_call(
                {
                    "tool_ref": action.tool_ref,
                    "input": action.input,
                },
                allowed_tools,
            )
            result = self._call_mcp_tool(
                tool_ref=tool_ref,
                normalized_input=normalized_input,
                mcp_client=mcp_client,
            )
            state.last_mcp_result = result
            state.mcp_results.append(result)
            state.messages.append(
                LlmMessage(
                    role="user",
                    content=self._json_text(
                        self._sanitize_for_llm(
                            {
                                "tool_result": {
                                    "tool_ref": tool_ref,
                                    "input": normalized_input,
                                    "result": result,
                                }
                            }
                        )
                    ),
                )
            )
            return None
        if isinstance(action, WriteFileAction):
            written_path = self._write_file(
                {
                    "path": action.path,
                    "content": action.content,
                }
            )
            state.written_files.append(written_path)
            state.messages.append(
                LlmMessage(
                    role="user",
                    content=self._json_text(
                        self._sanitize_for_llm(
                            {
                                "write_file_result": {
                                    "path": written_path,
                                }
                            }
                        )
                    ),
                )
            )
            return None
        raise ValueError(f"Unsupported LLM action type: {action.type}")

    def _handle_runtime_failure(
        self,
        exc: Exception,
        *,
        raw_response: str,
        parsed: Any,
        state: _ExecutionState,
    ) -> None:
        state.last_error = exc
        state.messages.append(
            LlmMessage(
                role="user",
                content=self._runtime_error_message(
                    exc,
                    raw_response=raw_response,
                    parsed=parsed,
                ),
            )
        )

    def _build_payload(
        self,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
        available_tools: list[dict[str, Any]],
        tool_catalog_warning: str | None,
        indexed_files: list[dict[str, Any]],
        attached_files: list[LlmAttachment],
        requested_paths: list[str],
    ) -> dict[str, Any]:
        return {
            "workflow": self._sanitize_for_llm(workflow_to_dict(workflow)),
            "step": {
                "step_id": step.step_id,
                "name": step.name,
                "description": step.description,
                "step_runtime": step.tool_ref,
                "instruction": self._step_instruction(step, resolved_input),
                "resolved_input": self._sanitize_for_llm(resolved_input),
                "depends_on": list(step.depends_on),
                "risk_level": step.risk_level.value,
                "requires_approval": step.requires_approval,
                "backup_scope": step.backup_scope.value,
            },
            "step_results": self._sanitize_for_llm(step_results),
            "step_runtime_protocol": {
                "version": STEP_RUNTIME_PROTOCOL_VERSION,
                "actions": [
                    "call_mcp_tool",
                    "request_file_attachments",
                    "write_file",
                    "finish",
                ],
                "compact_multi_action_batch": {
                    "supported": True,
                    "max_actions": 4,
                    "allowed_actions": ["call_mcp_tool", "write_file", "finish"],
                    "request_file_attachments_requires_separate_turn": True,
                    "finish_must_be_last": True,
                    "use_when": (
                        "Only batch actions when later actions do not depend on intermediate "
                        "tool or file results."
                    ),
                },
                "finish_result_contract": (
                    "finish.result is required and becomes the summarized handoff to later steps."
                ),
            },
            "available_mcp_tools": available_tools,
            "mcp_tool_catalog_warning": tool_catalog_warning,
            "workspace_root": self._workspace_root.as_posix(),
            "workspace_file_index": indexed_files,
            "attached_files": self._sanitize_for_llm(
                [attachment.path for attachment in attached_files],
                key="attached_files",
            ),
            "requested_attachment_paths": requested_paths,
        }

    def _request_messages(
        self,
        messages: list[LlmMessage],
        *,
        turn_index: int,
    ) -> tuple[LlmMessage, ...]:
        if not self._remembers_context or turn_index == 0:
            return tuple(messages)
        return (messages[-1],)

    def _system_prompt(self) -> str:
        return build_system_prompt(
            "\n".join(
                [
                    "You are the AI execution controller for one already-planned workflow step.",
                    "Work on the current step only. Do not re-plan the workflow.",
                    "",
                    "Return exactly one JSON object per turn using one of these shapes:",
                    '{"type":"call_mcp_tool","tool_ref":"...","input":{}}',
                    '{"type":"request_file_attachments","paths":["relative/path.ext"],"reason":"..."}',
                    '{"type":"write_file","path":"relative/path.txt","content":"..."}',
                    '{"type":"finish","result":{}}',
                    '{"actions":[{"type":"call_mcp_tool","tool_ref":"...","input":{}},{"type":"finish","result":{}}]}',
                    "",
                    "Execution priorities",
                    "- First understand workflow objective, constraints, success criteria, current step instruction, resolved_input, and prior step_results.",
                    "- Then inspect available_mcp_tools, attached_files, and workspace_file_index.",
                    "- Choose the smallest correct next action for the current step.",
                    "- Prefer read or inspection actions before mutation when the required state is still uncertain.",
                    "",
                    "Rules",
                    "- Each turn must contain exactly one JSON object.",
                    "- Prefer a single action object. Use actions[] only for a short deterministic sequence when later actions do not depend on intermediate results.",
                    "- Use only provided available_mcp_tools[].name values.",
                    "- Use exact argument names from the selected MCP tool contract. Do not invent aliases or cross-server arguments.",
                    "- After a tool call or file write, you will receive a user message with the execution result. Use that result to decide the next action unless the sequence was safely batched.",
                    "- Return finish only when the current step objective is actually complete.",
                    "- finish.result must always be an object and should summarize status, key outcomes, important findings, output files, changed files, and unresolved issues that later steps need to know.",
                    "- If the current step is not complete, do not return finish.",
                    "- If step.step_runtime is orchestra.ai_review, prefer analysis and a direct finish result unless tool evidence is clearly required.",
                    "- Apply real changes only through MCP tool calls or write_file.",
                    "- Use write_file only for UTF-8 text files inside workspace_root. Do not use write_file for Excel files, binary files, or other structured artifacts that should be created through MCP tools.",
                    "- Use workspace-relative paths for files inside workspace_root whenever possible.",
                    "- If you need local file contents as true file attachments, return only request_file_attachments.",
                    "- request_file_attachments cannot be mixed with execution actions and should request only files present in workspace_file_index.",
                    "- In actions[], finish must be last and may appear at most once.",
                    "- Keep actions[] short, usually 2 to 4 actions maximum.",
                    "- Do not ask clarifying questions. Use the provided payload, tool calls, or attachment requests instead.",
                    "- Do not return a workflow step plan, top-level steps array, or any non-runtime schema.",
                    "- If you receive runtime_error, repair only the last runtime action for the current step.",
                    "- Keep output valid JSON with no commentary.",
                ]
            ),
            language=self._language,
            prompt_kind="executor",
            remembers_context=self._remembers_context,
        )

    @staticmethod
    def _extract_json(raw_text: str) -> Any:
        return extract_json_payload(raw_text, label="LLM step executor output")

    def _runtime_error_message(
        self,
        exc: Exception,
        *,
        raw_response: str,
        parsed: Any,
    ) -> str:
        preview_source = raw_response
        if isinstance(parsed, dict):
            preview_source = self._json_text(parsed)
        return build_runtime_error_feedback(
            language=self._language,
            kind=self._runtime_error_kind(exc, parsed),
            error_message=text_preview(clean_exception_message(exc), limit=240),
            model_output=text_preview(preview_source, limit=320),
        )

    @staticmethod
    def _runtime_error_kind(exc: Exception, parsed: Any) -> str:
        if isinstance(parsed, dict) and "steps" in parsed:
            return "returned_step_plan"
        message = clean_exception_message(exc).lower()
        if "valid json" in message:
            return "invalid_json"
        if "tool '" in message or "allowed mcp tool set" in message:
            return "invalid_tool"
        if "workspace sandbox" in message:
            return "workspace_sandbox"
        return "runtime_action_error"

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
        self._validate_action_sequence(raw_actions)

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

        return self._build_execution_result(
            explicit_result=explicit_result,
            last_mcp_result=last_mcp_result,
            written_files=written_files,
            mcp_results=mcp_results,
        )

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
            tool_ref, normalized_input = self._prepare_mcp_tool_call(raw_action, allowed_tools)
            result = self._call_mcp_tool(
                tool_ref=tool_ref,
                normalized_input=normalized_input,
                mcp_client=mcp_client,
            )
            mcp_results.append(result)
            return explicit_result, result
        if action_type == "write_file":
            written_files.append(self._write_file(raw_action))
            return explicit_result, last_mcp_result
        if action_type == "finish":
            result = raw_action.get("result")
            if not isinstance(result, dict):
                raise ValueError("finish requires object 'result'.")
            return result, last_mcp_result
        if action_type == "set_result":
            return raw_action.get("result"), last_mcp_result
        raise ValueError(f"Unsupported LLM action type: {action_type}")

    @staticmethod
    def _validate_action_sequence(raw_actions: list[Any]) -> None:
        finish_seen = False
        for index, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, dict):
                raise ValueError("Each LLM action must be an object.")
            action_type = raw_action.get("type")
            if action_type == "request_file_attachments" and len(raw_actions) != 1:
                raise ValueError(
                    "request_file_attachments cannot be mixed with execution actions."
                )
            if action_type == "finish":
                if finish_seen:
                    raise ValueError("finish may appear at most once in actions[].")
                if index != len(raw_actions) - 1:
                    raise ValueError("finish must be the last action in actions[].")
                finish_seen = True

    def _handle_attachment_payload(
        self,
        *,
        parsed: Any,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        state: _ExecutionState,
    ) -> bool:
        request_actions = self._request_actions_from_payload(parsed)
        if not request_actions:
            return False
        self._apply_attachment_request(
            workflow=workflow,
            step=step,
            resolved_input=resolved_input,
            request_actions=request_actions,
            state=state,
        )
        return True

    def _apply_attachment_request(
        self,
        *,
        workflow: Workflow,
        step: Step,
        resolved_input: dict[str, Any],
        request_actions: list[dict[str, Any]],
        state: _ExecutionState,
    ) -> None:
        state.indexed_files = self._refresh_workspace_index(resolved_input)
        prior_attachment_count = len(state.attached_files)
        requested = self._workspace_paths.append_requested_attachments(
            request_actions=request_actions,
            indexed_paths={
                entry["path"]
                for entry in state.indexed_files
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            },
            attached_files=state.attached_files,
        )
        if not requested:
            raise ValueError("LLM requested file attachments but no new files were added.")
        self._record_event(
            {
                "event_type": "llm_attachment_requested",
                "workflow_id": workflow.workflow_id,
                "step_id": step.step_id,
                "paths": requested,
            }
        )
        state.requested_paths.extend(requested)
        state.messages.append(
            LlmMessage(
                role="user",
                content=self._json_text(
                    self._sanitize_for_llm(
                        {
                            "attachment_request_result": {
                                "attached_paths": requested,
                                "all_attached_files": [
                                    attachment.path for attachment in state.attached_files
                                ],
                            }
                        }
                    )
                ),
                attachments=tuple(state.attached_files[prior_attachment_count:]),
            )
        )

    @staticmethod
    def _prepare_mcp_tool_call(
        raw_action: dict[str, Any],
        allowed_tools: set[str],
    ) -> tuple[str, dict[str, Any]]:
        tool_ref = raw_action.get("tool_ref")
        tool_input = raw_action.get("input", {})
        if not isinstance(tool_ref, str):
            raise ValueError("call_mcp_tool requires string 'tool_ref'.")
        if tool_ref not in allowed_tools:
            raise ValueError(f"Tool '{tool_ref}' is not in the allowed MCP tool set.")
        if not isinstance(tool_input, dict):
            raise ValueError("call_mcp_tool requires object 'input'.")
        return tool_ref, normalize_tool_input(tool_ref, tool_input)

    def _call_mcp_tool(
        self,
        *,
        tool_ref: str,
        normalized_input: dict[str, Any],
        mcp_client: IMcpClient,
    ) -> dict[str, Any]:
        result = mcp_client.call_tool(tool_ref, normalized_input)
        self._workspace_inventory.invalidate()
        return result

    def _write_file(self, raw_action: dict[str, Any]) -> str:
        path = raw_action.get("path")
        content = raw_action.get("content")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("write_file requires string 'path'.")
        if not isinstance(content, str):
            raise ValueError("write_file requires string 'content'.")
        target = self._workspace_paths.resolve_workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._record_event(
            {
                "event_type": "workspace_file_written",
                "path": str(target),
                "size_bytes": len(content.encode("utf-8")),
                "source": "llm_step_executor",
            }
        )
        self._workspace_inventory.invalidate()
        return str(target)

    def _refresh_workspace_index(self, resolved_input: dict[str, Any]) -> list[dict[str, Any]]:
        return self._workspace_inventory.snapshot(resolved_input).files_for_prompt()

    def _record_event(self, event: dict[str, Any]) -> None:
        if self._audit_logger is None:
            return
        self._audit_logger.record(enrich_observation_event(event))

    @staticmethod
    def _step_instruction(step: Step, resolved_input: dict[str, Any]) -> str:
        instruction = resolved_input.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            return instruction
        return step.description

    def _sanitize_for_llm(self, value: Any, *, key: str | None = None) -> Any:
        return self._workspace_paths.sanitize_for_llm(value, key=key)

    @staticmethod
    def _json_text(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _is_legacy_action_payload(parsed: Any) -> bool:
        return isinstance(parsed, dict) and "actions" in parsed

    @staticmethod
    def _build_execution_result(
        *,
        explicit_result: Any,
        last_mcp_result: dict[str, Any] | None,
        written_files: list[str],
        mcp_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
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

    @classmethod
    def _request_actions_from_payload(cls, parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, dict) and parsed.get("type") == "request_file_attachments":
            return [parsed]

        raw_actions = cls._raw_actions(parsed)
        if not raw_actions:
            return []
        return cls._request_actions(raw_actions)

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
