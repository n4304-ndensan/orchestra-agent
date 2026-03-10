from __future__ import annotations

from orchestra_agent.shared.llm_json import extract_json_payload


def test_extract_json_payload_repairs_invalid_windows_path_escapes() -> None:
    payload = extract_json_payload(
        (
            '{"type":"call_mcp_tool","tool_ref":"excel.open_file","input":'
            '{"path":"C:\\Users\\syogo\\Documents\\HelloWorld.xlsx"}}'
        ),
        label="test payload",
    )

    assert payload == {
        "type": "call_mcp_tool",
        "tool_ref": "excel.open_file",
        "input": {"path": r"C:\Users\syogo\Documents\HelloWorld.xlsx"},
    }


def test_extract_json_payload_accepts_wrapped_json_object() -> None:
    payload = extract_json_payload(
        """
        Here is the result:
        {"type":"finish","result":{"status":"ok"}}
        """,
        label="test payload",
    )

    assert payload == {"type": "finish", "result": {"status": "ok"}}
