from __future__ import annotations

from orchestra_agent.shared.preview import mapping_preview, value_preview


def test_mapping_preview_formats_nested_values_and_truncates() -> None:
    preview = mapping_preview(
        {
            "file": "output/summary.xlsx",
            "cells": {"A1": "hello", "B2": 60, "C3": "ignored"},
            "tags": ["apac", "emea", "amer", "latam"],
            "overwrite": True,
            "extra": "ignored",
        }
    )

    assert "file=output/summary.xlsx" in preview
    assert "cells={A1=hello, B2=60, +1 more}" in preview
    assert "tags=[apac, emea, amer, +1 more]" in preview
    assert "+1 more" in preview


def test_value_preview_sanitizes_and_truncates_strings() -> None:
    raw = "line1\r\n" + ("x" * 80)

    preview = value_preview(raw)

    assert "\r" not in preview
    assert "\n" not in preview
    assert preview.endswith("...")
    assert len(preview) == 72
