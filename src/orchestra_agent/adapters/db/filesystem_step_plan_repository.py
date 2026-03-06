from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from orchestra_agent.domain.enums import BackupScope, RiskLevel
from orchestra_agent.domain.step import Step
from orchestra_agent.domain.step_plan import StepPlan
from orchestra_agent.ports.step_plan_repository import IStepPlanRepository


class FilesystemStepPlanRepository(IStepPlanRepository):
    """
    Filesystem-backed StepPlan repository.

    Layout:
    - <root>/<workflow_id>/<step_plan_id>/step_plan_v{n}.json
    - <root>/<workflow_id>/<step_plan_id>/step_plan_latest.json
    """

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def save(self, step_plan: StepPlan) -> None:
        plan_dir = self._plan_dir(step_plan.workflow_id, step_plan.step_plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)

        payload = self._serialize(step_plan)
        version_path = plan_dir / f"step_plan_v{step_plan.version}.json"
        latest_path = plan_dir / "step_plan_latest.json"
        version_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, step_plan_id: str, version: int | None = None) -> StepPlan | None:
        if version is not None:
            for plan_dir in self._root_dir.rglob(step_plan_id):
                if not plan_dir.is_dir():
                    continue
                candidate = plan_dir / f"step_plan_v{version}.json"
                if candidate.is_file():
                    return self._deserialize(candidate)
            return None

        latest_candidates = list(self._root_dir.rglob(f"{step_plan_id}/step_plan_latest.json"))
        if latest_candidates:
            return self._deserialize(latest_candidates[0])

        version_candidates = list(self._root_dir.rglob(f"{step_plan_id}/step_plan_v*.json"))
        if not version_candidates:
            return None
        latest_version_file = sorted(version_candidates)[-1]
        return self._deserialize(latest_version_file)

    def _plan_dir(self, workflow_id: str, step_plan_id: str) -> Path:
        return self._root_dir / workflow_id / step_plan_id

    @staticmethod
    def _serialize(step_plan: StepPlan) -> dict[str, Any]:
        return {
            "step_plan_id": step_plan.step_plan_id,
            "workflow_id": step_plan.workflow_id,
            "version": step_plan.version,
            "steps": [
                {
                    "step_id": step.step_id,
                    "name": step.name,
                    "description": step.description,
                    "tool_ref": step.tool_ref,
                    "resolved_input": step.resolved_input,
                    "depends_on": step.depends_on,
                    "risk_level": step.risk_level.value,
                    "requires_approval": step.requires_approval,
                    "run": step.run,
                    "skip": step.skip,
                    "backup_scope": step.backup_scope.value,
                }
                for step in step_plan.steps
            ],
        }

    @staticmethod
    def _deserialize(path: Path) -> StepPlan:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"StepPlan JSON must be an object: {path}")

        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError(f"StepPlan JSON missing steps array: {path}")

        steps: list[Step] = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                raise ValueError(f"Step entry must be object: {path}")

            resolved_input_raw = raw.get("resolved_input", {})
            depends_on_raw = raw.get("depends_on", [])
            if not isinstance(resolved_input_raw, dict):
                raise ValueError(f"resolved_input must be object: {path}")
            if not isinstance(depends_on_raw, list):
                raise ValueError(f"depends_on must be list: {path}")
            if not all(isinstance(item, str) for item in depends_on_raw):
                raise ValueError(f"depends_on must contain strings: {path}")

            steps.append(
                Step(
                    step_id=str(raw.get("step_id", "")),
                    name=str(raw.get("name", "")),
                    description=str(raw.get("description", "")),
                    tool_ref=str(raw.get("tool_ref", "")),
                    resolved_input=cast(dict[str, Any], resolved_input_raw),
                    depends_on=cast(list[str], depends_on_raw),
                    risk_level=RiskLevel(str(raw.get("risk_level", RiskLevel.LOW.value))),
                    requires_approval=bool(raw.get("requires_approval", False)),
                    run=bool(raw.get("run", True)),
                    skip=bool(raw.get("skip", False)),
                    backup_scope=BackupScope(
                        str(raw.get("backup_scope", BackupScope.NONE.value))
                    ),
                )
            )

        return StepPlan(
            step_plan_id=str(payload.get("step_plan_id", "")),
            workflow_id=str(payload.get("workflow_id", "")),
            version=int(payload.get("version", 1)),
            steps=steps,
        )

