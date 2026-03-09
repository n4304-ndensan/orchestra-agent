from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from orchestra_agent.domain.serialization import workflow_to_xml_root
from orchestra_agent.domain.workflow import ReplanContext, Workflow
from orchestra_agent.ports.workflow_repository import IWorkflowRepository


class XmlWorkflowRepository(IWorkflowRepository):
    """
    Filesystem-backed workflow repository.

    Layout:
    - <root>/<workflow_id>/workflow.xml                     (latest)
    - <root>/<workflow_id>/versions/workflow_v{n}.xml       (versioned)
    - <root>/<workflow_id>/feedback/feedback_v{n}.txt       (latest feedback for version n)
    - <root>/<workflow_id>/workflow.lock                    (immutable marker)
    """

    _lock_file_name = "workflow.lock"

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def save(self, workflow: Workflow) -> None:
        workflow_dir = self._root_dir / workflow.workflow_id
        if self.is_locked(workflow.workflow_id):
            raise PermissionError(
                f"Workflow '{workflow.workflow_id}' is locked and cannot be modified."
            )
        versions_dir = workflow_dir / "versions"
        feedback_dir = workflow_dir / "feedback"
        versions_dir.mkdir(parents=True, exist_ok=True)
        feedback_dir.mkdir(parents=True, exist_ok=True)

        self._write_workflow_xml(workflow, workflow_dir / "workflow.xml")
        self._write_workflow_xml(
            workflow,
            versions_dir / f"workflow_v{workflow.version}.xml",
        )

        if workflow.feedback_history:
            latest_feedback = workflow.feedback_history[-1]
            feedback_path = feedback_dir / f"feedback_v{workflow.version}.txt"
            feedback_path.write_text(latest_feedback, encoding="utf-8")

    def get(self, workflow_id: str, version: int | None = None) -> Workflow | None:
        workflow_dir = self._root_dir / workflow_id
        if not workflow_dir.is_dir():
            return None

        if version is None:
            latest_path = workflow_dir / "workflow.xml"
            if latest_path.is_file():
                return self._read_workflow_xml(latest_path)
            return self._read_latest_version(workflow_dir / "versions")

        version_path = workflow_dir / "versions" / f"workflow_v{version}.xml"
        if version_path.is_file():
            return self._read_workflow_xml(version_path)
        return None

    def import_from_xml(self, xml_path: Path) -> Workflow:
        workflow = self._read_workflow_xml(xml_path)
        self.save(workflow)
        return workflow

    def lock_workflow(self, workflow_id: str) -> None:
        workflow_dir = self._root_dir / workflow_id
        workflow_dir.mkdir(parents=True, exist_ok=True)
        lock_path = workflow_dir / self._lock_file_name
        if lock_path.is_file():
            return
        lock_path.write_text("locked", encoding="utf-8")

    def is_locked(self, workflow_id: str) -> bool:
        lock_path = self._root_dir / workflow_id / self._lock_file_name
        return lock_path.is_file()

    @staticmethod
    def _write_workflow_xml(workflow: Workflow, path: Path) -> None:
        tree = ET.ElementTree(workflow_to_xml_root(workflow))
        path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _read_workflow_xml(path: Path) -> Workflow:
        tree = ET.parse(path)
        root = tree.getroot()

        workflow_id = root.attrib.get("id")
        version_str = root.attrib.get("version")
        if workflow_id is None or not workflow_id.strip():
            raise ValueError(f"Workflow XML is missing id attribute: {path}")
        if version_str is None or not version_str.strip():
            raise ValueError(f"Workflow XML is missing version attribute: {path}")

        name = XmlWorkflowRepository._element_text(root, "name")
        objective = XmlWorkflowRepository._element_text(root, "objective")
        reference_files = XmlWorkflowRepository._items(root, "reference_files")
        constraints = XmlWorkflowRepository._items(root, "constraints")
        success_criteria = XmlWorkflowRepository._items(root, "success_criteria")
        feedback_history = XmlWorkflowRepository._items(root, "feedback_history")
        replan_context = XmlWorkflowRepository._replan_context(root)

        return Workflow(
            workflow_id=workflow_id,
            name=name,
            version=int(version_str),
            objective=objective,
            reference_files=reference_files,
            constraints=constraints,
            success_criteria=success_criteria,
            feedback_history=feedback_history,
            replan_context=replan_context,
        )

    @staticmethod
    def _read_latest_version(versions_dir: Path) -> Workflow | None:
        if not versions_dir.is_dir():
            return None
        candidates = sorted(versions_dir.glob("workflow_v*.xml"))
        if not candidates:
            return None
        return XmlWorkflowRepository._read_workflow_xml(candidates[-1])

    @staticmethod
    def _element_text(root: ET.Element, tag: str) -> str:
        element = root.find(tag)
        if element is None or element.text is None:
            return ""
        return element.text

    @staticmethod
    def _items(root: ET.Element, tag: str) -> list[str]:
        parent = root.find(tag)
        if parent is None:
            return []
        return [item.text or "" for item in parent.findall("item")]

    @staticmethod
    def _replan_context(root: ET.Element) -> ReplanContext | None:
        replan_context = root.find("replan_context")
        if replan_context is None:
            return None

        trigger = XmlWorkflowRepository._element_text(replan_context, "trigger")
        change_summary = XmlWorkflowRepository._element_text(replan_context, "change_summary")
        source_workflow_document = XmlWorkflowRepository._element_text(
            replan_context,
            "source_workflow_document",
        )
        source_step_plan_document = XmlWorkflowRepository._element_text(
            replan_context,
            "source_step_plan_document",
        )
        if not any(
            (
                trigger,
                change_summary,
                source_workflow_document,
                source_step_plan_document,
            )
        ):
            return None
        return ReplanContext(
            trigger=trigger,
            change_summary=change_summary,
            source_workflow_document=source_workflow_document,
            source_step_plan_document=source_step_plan_document,
        )
