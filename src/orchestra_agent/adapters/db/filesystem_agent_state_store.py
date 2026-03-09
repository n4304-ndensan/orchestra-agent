from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestra_agent.domain.agent_state import AgentState
from orchestra_agent.domain.enums import ApprovalStatus, ExecutionStatus
from orchestra_agent.domain.execution_record import ExecutionRecord
from orchestra_agent.ports.agent_state_store import IAgentStateStore


class FilesystemAgentStateStore(IAgentStateStore):
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self, run_id: str) -> AgentState | None:
        path = self._state_path(run_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Run state payload must be an object: {path}")
        return self._deserialize(payload)

    def save(self, state: AgentState) -> None:
        payload = self._serialize(state)
        with self._lock:
            self._write_json(self._state_path(state.run_id), payload)

    def append_execution(self, run_id: str, record: ExecutionRecord) -> None:
        with self._lock:
            state = self.load(run_id)
            if state is None:
                raise KeyError(f"run_id '{run_id}' not found.")
            state.append_execution(record)
            self._write_json(self._state_path(run_id), self._serialize(state))

    def _state_path(self, run_id: str) -> Path:
        return self._root_dir / f"{run_id}.json"

    @staticmethod
    def _serialize(state: AgentState) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "workflow_id": state.workflow_id,
            "workflow_version": state.workflow_version,
            "step_plan_id": state.step_plan_id,
            "step_plan_version": state.step_plan_version,
            "current_step_id": state.current_step_id,
            "approval_status": state.approval_status.value,
            "execution_history": [
                {
                    "step_id": record.step_id,
                    "status": record.status.value,
                    "started_at": record.started_at.isoformat(),
                    "finished_at": record.finished_at.isoformat() if record.finished_at else None,
                    "result": record.result,
                    "error": record.error,
                    "snapshot_ref": record.snapshot_ref,
                    "metadata": record.metadata,
                }
                for record in state.execution_history
            ],
            "snapshot_refs": list(state.snapshot_refs),
            "last_error": state.last_error,
            "metadata": deepcopy(state.metadata),
        }

    @staticmethod
    def _deserialize(payload: dict[str, Any]) -> AgentState:
        history_raw = payload.get("execution_history", [])
        if not isinstance(history_raw, list):
            raise ValueError("execution_history must be a list.")
        history = [FilesystemAgentStateStore._deserialize_record(item) for item in history_raw]

        return AgentState(
            run_id=str(payload["run_id"]),
            workflow_id=_optional_str(payload.get("workflow_id")),
            workflow_version=_optional_int(payload.get("workflow_version")),
            step_plan_id=_optional_str(payload.get("step_plan_id")),
            step_plan_version=_optional_int(payload.get("step_plan_version")),
            current_step_id=_optional_str(payload.get("current_step_id")),
            approval_status=ApprovalStatus(
                str(payload.get("approval_status", ApprovalStatus.PENDING))
            ),
            execution_history=history,
            snapshot_refs=_as_str_list(payload.get("snapshot_refs", [])),
            last_error=_optional_str(payload.get("last_error")),
            metadata=_as_dict(payload.get("metadata", {})),
        )

    @staticmethod
    def _deserialize_record(raw: Any) -> ExecutionRecord:
        if not isinstance(raw, dict):
            raise ValueError("Execution history entries must be objects.")
        finished_at_raw = raw.get("finished_at")
        return ExecutionRecord(
            step_id=str(raw["step_id"]),
            status=ExecutionStatus(str(raw["status"])),
            started_at=datetime.fromisoformat(str(raw["started_at"])),
            finished_at=(
                datetime.fromisoformat(str(finished_at_raw))
                if isinstance(finished_at_raw, str)
                else None
            ),
            result=_optional_dict(raw.get("result")),
            error=_optional_str(raw.get("error")),
            snapshot_ref=_optional_str(raw.get("snapshot_ref")),
            metadata=_as_dict(raw.get("metadata", {})),
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected string or null.")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("Expected int or null.")
    return value


def _optional_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Expected object or null.")
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected object.")
    return value


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected a list of strings.")
    return value
