from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.domain.step import Step
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.domain.workflow import Workflow
from orchestra_agent.ports.llm_client import (
    ILlmClient,
    LlmAttachment,
    LlmGenerateRequest,
    LlmMessage,
)
from orchestra_agent.ports.planner import IPlanner


class StructuredLlmPlanner(IPlanner):
    """
    Builds a full StepPlan from a structured LLM response.
    """

    _builtin_tool_refs = {"orchestra.llm_execute"}

    def __init__(
        self,
        llm_client: ILlmClient,
        available_tools_supplier: Callable[[], list[str]],
        available_tool_catalog_supplier: Callable[[], list[dict[str, str]]] | None = None,
        fallback_planner: IPlanner | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2400,
    ) -> None:
        self._llm_client = llm_client
        self._available_tools_supplier = available_tools_supplier
        self._available_tool_catalog_supplier = available_tool_catalog_supplier
        self._fallback_planner = fallback_planner
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.last_warning: str | None = None

    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        self.last_warning = None
        try:
            available_tool_catalog = self._available_tool_catalog()
            allowed_tools = {tool["name"] for tool in available_tool_catalog}
            request = LlmGenerateRequest(
                messages=(
                    LlmMessage(role="system", content=self._system_prompt()),
                    LlmMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "workflow": {
                                    "workflow_id": workflow.workflow_id,
                                    "name": workflow.name,
                                    "objective": workflow.objective,
                                    "reference_files": workflow.reference_files,
                                    "constraints": workflow.constraints,
                                    "success_criteria": workflow.success_criteria,
                                    "feedback_history": workflow.feedback_history,
                                },
                                "available_tools": available_tool_catalog,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        attachments=self._workflow_attachments(workflow),
                    ),
                ),
                response_format="json_object",
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            raw = self._llm_client.generate(request)
            parsed = self._extract_json(raw)
            return self._build_step_plan(workflow, parsed, allowed_tools)
        except Exception as exc:  # noqa: BLE001
            if self._fallback_planner is None:
                raise
            self.last_warning = f"Structured LLM plan rejected; fallback applied: {exc}"
            return self._fallback_planner.compile_step_plan(workflow)

    def _allowed_tools(self) -> set[str]:
        tools = set(self._builtin_tool_refs)
        tools.update(self._available_tools_supplier())
        return tools

    def _available_tool_catalog(self) -> list[dict[str, str]]:
        if self._available_tool_catalog_supplier is not None:
            catalog = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                }
                for tool in self._available_tool_catalog_supplier()
                if isinstance(tool, dict) and isinstance(tool.get("name"), str)
            ]
            if catalog:
                builtin_tools = [
                    {
                        "name": tool_name,
                        "description": "AI-orchestrated step runtime for multi-tool execution.",
                    }
                    for tool_name in sorted(self._builtin_tool_refs)
                ]
                return sorted(builtin_tools + catalog, key=lambda item: item["name"])

        return [
            {"name": tool_name, "description": ""}
            for tool_name in sorted(self._allowed_tools())
        ]

    @staticmethod
    def _workflow_attachments(workflow: Workflow) -> tuple[LlmAttachment, ...]:
        return tuple(LlmAttachment(path=file_path) for file_path in workflow.reference_files)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a workflow planner. Return ONLY JSON with this shape:\n"
            '{"steps":[{"step_id":"...","name":"...","description":"...","tool_ref":"...",'
            '"resolved_input":{},"depends_on":[],"risk_level":"LOW","requires_approval":true,'
            '"run":true,"skip":false,"backup_scope":"NONE"}]}\n'
            "Rules:\n"
            "1) Create the full plan, not a patch.\n"
            "2) step_id must be unique and dependency-safe.\n"
            "3) tool_ref must be one of the provided available_tools[].name values.\n"
            "4) Use orchestra.llm_execute when the step needs local workspace edits or "
            "multi-tool orchestration.\n"
            "5) Use backup_scope=WORKSPACE before mutating local files unless a smaller FILE "
            "backup is sufficient.\n"
            "6) Respect feedback_history as the latest correction source.\n"
            "7) Keep output valid JSON and do not add commentary."
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
            raise ValueError("Structured LLM planner output is not valid JSON.")
        return json.loads(stripped[start : end + 1])

    @staticmethod
    def _build_step_plan(
        workflow: Workflow,
        parsed: Any,
        allowed_tools: set[str],
    ) -> StepPlan:
        if not isinstance(parsed, dict):
            raise ValueError("Structured LLM planner output must be an object.")

        raw_steps = parsed.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Structured LLM planner output must contain a non-empty 'steps' list.")

        steps = [StructuredLlmPlanner._build_step(item, allowed_tools) for item in raw_steps]
        return StepPlan(
            step_plan_id=f"sp-{uuid4().hex[:10]}",
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            steps=steps,
        )

    @staticmethod
    def _build_step(raw_step: Any, allowed_tools: set[str]) -> Step:
        if not isinstance(raw_step, dict):
            raise ValueError("Each LLM-generated step must be an object.")

        tool_ref = StructuredLlmPlanner._required_str(raw_step, "tool_ref")
        if tool_ref not in allowed_tools:
            raise ValueError(f"tool_ref '{tool_ref}' is not in the allowed tool set.")

        return Step(
            step_id=StructuredLlmPlanner._required_str(raw_step, "step_id"),
            name=StructuredLlmPlanner._required_str(raw_step, "name"),
            description=StructuredLlmPlanner._required_str(raw_step, "description"),
            tool_ref=tool_ref,
            resolved_input=StructuredLlmPlanner._as_dict(raw_step.get("resolved_input", {})),
            depends_on=StructuredLlmPlanner._as_str_list(raw_step.get("depends_on", [])),
            risk_level=RiskLevel(StructuredLlmPlanner._optional_str(raw_step, "risk_level", "LOW")),
            requires_approval=StructuredLlmPlanner._as_bool(
                raw_step.get("requires_approval", False)
            ),
            run=StructuredLlmPlanner._as_bool(raw_step.get("run", True)),
            skip=StructuredLlmPlanner._as_bool(raw_step.get("skip", False)),
            backup_scope=BackupScope(
                StructuredLlmPlanner._optional_str(raw_step, "backup_scope", "NONE")
            ),
        )

    @staticmethod
    def _required_str(raw_step: dict[str, Any], key: str) -> str:
        value = raw_step.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Step field '{key}' must be a non-empty string.")
        return value

    @staticmethod
    def _optional_str(raw_step: dict[str, Any], key: str, default: str) -> str:
        value = raw_step.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Step field '{key}' must be a string.")
        return value

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("resolved_input must be an object.")
        return value

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("depends_on must be a list of strings.")
        return value

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("Boolean step fields must be bool.")
        return value
