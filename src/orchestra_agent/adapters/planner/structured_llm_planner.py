from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.domain.serialization import replan_context_to_dict
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
from orchestra_agent.shared.llm_json import extract_json_payload
from orchestra_agent.shared.mcp_tool_catalog import normalize_mcp_tool_catalog
from orchestra_agent.shared.llm_prompting import LlmLanguage, build_system_prompt


class StructuredLlmPlanner(IPlanner):
    """
    Builds a full StepPlan from a structured LLM response.
    """

    def __init__(
        self,
        llm_client: ILlmClient,
        available_tools_supplier: Callable[[], list[str]],
        available_tool_catalog_supplier: Callable[[], list[dict[str, Any]]] | None = None,
        fallback_planner: IPlanner | None = None,
        language: LlmLanguage = "en",
        temperature: float = 0.0,
        max_tokens: int = 2400,
    ) -> None:
        self._llm_client = llm_client
        self._available_tools_supplier = available_tools_supplier
        self._available_tool_catalog_supplier = available_tool_catalog_supplier
        self._fallback_planner = fallback_planner
        self._language = language
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.last_warning: str | None = None

    def compile_step_plan(self, workflow: Workflow) -> StepPlan:
        self.last_warning = None
        available_mcp_tools, tool_catalog_warning = self._safe_available_mcp_tool_catalog()
        try:
            request = LlmGenerateRequest(
                messages=(
                    LlmMessage(role="system", content=self._system_prompt()),
                    LlmMessage(
                        role="user",
                        content=json.dumps(
                            self._build_planner_payload(workflow, available_mcp_tools),
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
            plan = self._build_step_plan(workflow, parsed)
            self.last_warning = tool_catalog_warning
            return plan
        except Exception as exc:  # noqa: BLE001
            if self._fallback_planner is None:
                raise
            self.last_warning = f"Structured LLM plan rejected; fallback applied: {exc}"
            return self._fallback_planner.compile_step_plan(workflow)

    @staticmethod
    def _build_planner_payload(
        workflow: Workflow,
        available_mcp_tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a compact payload for the planner — only fields the LLM needs."""
        payload: dict[str, Any] = {
            "workflow": {
                "objective": workflow.objective,
            },
            "step_runtimes": ["orchestra.llm_execute", "orchestra.ai_review"],
            "available_mcp_tools": available_mcp_tools,
        }
        wf = payload["workflow"]
        if workflow.name:
            wf["name"] = workflow.name
        if workflow.constraints:
            wf["constraints"] = list(workflow.constraints)
        if workflow.success_criteria:
            wf["success_criteria"] = list(workflow.success_criteria)
        if workflow.reference_files:
            wf["reference_files"] = list(workflow.reference_files)
        if workflow.feedback_history:
            wf["feedback_history"] = list(workflow.feedback_history)
        if workflow.replan_context is not None:
            wf["replan_context"] = replan_context_to_dict(workflow.replan_context)
        return payload

    def _available_mcp_tool_catalog(self) -> list[dict[str, Any]]:
        if self._available_tool_catalog_supplier is not None:
            catalog = normalize_mcp_tool_catalog(self._available_tool_catalog_supplier())
            if catalog:
                return sorted(catalog, key=lambda item: item["name"])

        return sorted(
            normalize_mcp_tool_catalog(self._available_tools_supplier()),
            key=lambda item: item["name"],
        )

    def _safe_available_mcp_tool_catalog(self) -> tuple[list[dict[str, Any]], str | None]:
        try:
            return self._available_mcp_tool_catalog(), None
        except Exception as exc:  # noqa: BLE001
            return [], f"Structured LLM planner continued without MCP tool catalog: {exc}"

    @staticmethod
    def _workflow_attachments(workflow: Workflow) -> tuple[LlmAttachment, ...]:
        return tuple(LlmAttachment(path=file_path) for file_path in workflow.reference_files)

    def _system_prompt(self) -> str:
        return build_system_prompt(
            "\n".join(
                [
                    "You are the workflow planner for orchestra-agent.",
                    "Return exactly one JSON object. No markdown, no commentary.",
                    "",
                    "Schema (all fields required per step):",
                    '{"steps":[{"step_id":"s01","name":"...","description":"...","tool_ref":"orchestra.llm_execute",'
                    '"resolved_input":{},"depends_on":[],"risk_level":"LOW","requires_approval":false,'
                    '"run":true,"skip":false,"backup_scope":"NONE"}]}',
                    "",
                    "tool_ref: orchestra.llm_execute | orchestra.ai_review",
                    "risk_level: LOW (read-only) | MEDIUM (artifact creation) | HIGH (destructive/irreversible)",
                    "backup_scope: NONE (review-only) | FILE (single file mutated) | WORKSPACE (multi-file mutation)",
                    "",
                    "Key rules:",
                    "- Keep plans abstract: no MCP tool names or tool choreography in steps.",
                    "- Split steps around meaningful handoffs; 3-8 steps is typical.",
                    "- description: include business intent, target artifacts, expected outputs, success conditions.",
                    "- resolved_input: compact; use objective, target_files, source_files, expected_outputs, success_conditions, mutation_scope, review_focus as needed.",
                    "- depends_on: only prior step_ids.",
                    "- HIGH risk → requires_approval=true. First executable step that mutates artifacts → requires_approval=true.",
                    "- Included: run=true, skip=false. Omitted: run=false, skip=true.",
                    "- Never backup_scope=NONE for steps that create/modify/delete files.",
                    "- Honor replan_context.change_summary and feedback_history as corrections.",
                    "- available_mcp_tools are weak hints; their absence must not block planning.",
                    "- Return a complete plan, never a patch.",
                ]
            ),
            language=self._language,
            prompt_kind="planner",
        )

    @staticmethod
    def _extract_json(raw_text: str) -> Any:
        return extract_json_payload(raw_text, label="Structured LLM planner output")

    @staticmethod
    def _build_step_plan(
        workflow: Workflow,
        parsed: Any,
    ) -> StepPlan:
        if not isinstance(parsed, dict):
            raise ValueError("Structured LLM planner output must be an object.")

        raw_steps = parsed.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Structured LLM planner output must contain a non-empty 'steps' list.")

        steps = [StructuredLlmPlanner._build_step(item) for item in raw_steps]
        return StepPlan(
            step_plan_id=f"sp-{uuid4().hex[:10]}",
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            steps=steps,
        )

    @staticmethod
    def _build_step(raw_step: Any) -> Step:
        if not isinstance(raw_step, dict):
            raise ValueError("Each LLM-generated step must be an object.")

        return Step(
            step_id=StructuredLlmPlanner._required_str(raw_step, "step_id"),
            name=StructuredLlmPlanner._required_str(raw_step, "name"),
            description=StructuredLlmPlanner._required_str(raw_step, "description"),
            tool_ref=StructuredLlmPlanner._normalize_tool_ref(raw_step.get("tool_ref")),
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
    def _normalize_tool_ref(raw_tool_ref: Any) -> str:
        if isinstance(raw_tool_ref, str) and raw_tool_ref.strip() == "orchestra.ai_review":
            return "orchestra.ai_review"
        return "orchestra.llm_execute"

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
