from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.cli import main as run_cli


def test_main_runs_with_single_command_objective() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source = base / "sales.xlsx"
        source.write_text("seed", encoding="utf-8")

        exit_code = run_cli(
            [
                "sales.xlsxのC列を集計してsummary.xlsxへ",
                "--workspace",
                str(base),
                "--run-id",
                "run-main-test",
                "--no-print-plan",
            ]
        )

        assert exit_code == 0
        assert (base / "summary.xlsx").is_file()
    finally:
        shutil.rmtree(base, ignore_errors=True)
