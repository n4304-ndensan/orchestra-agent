from __future__ import annotations

from orchestra_agent.cli import _normalize_cli_argv, _parse_feedback_input, build_parser
from orchestra_agent.config import AppConfig


def test_normalize_cli_argv_preserves_explicit_subcommand() -> None:
    assert _normalize_cli_argv(["status", "run-1"]) == ["status", "run-1"]


def test_normalize_cli_argv_wraps_legacy_invocation_as_run() -> None:
    assert _normalize_cli_argv(["objective text"]) == ["run", "objective text"]
    assert _normalize_cli_argv(["--workflow-xml", "workflow.xml"]) == [
        "run",
        "--workflow-xml",
        "workflow.xml",
    ]


def test_build_parser_registers_product_subcommands() -> None:
    parser = build_parser(AppConfig())

    subparsers = [
        action
        for action in parser._actions  # noqa: SLF001
        if action.__class__.__name__ == "_SubParsersAction"
    ]

    assert len(subparsers) == 1
    assert {"run", "plan", "resume", "status"} <= set(subparsers[0].choices)


def test_build_parser_accepts_llm_language_and_memory_flags() -> None:
    parser = build_parser(AppConfig())

    args = parser.parse_args(
        [
            "run",
            "--llm-language",
            "ja",
            "--llm-remembers-context",
            "objective text",
        ]
    )

    assert args.llm_language == "ja"
    assert args.llm_remembers_context is True


def test_parse_feedback_input_supports_inline_messages() -> None:
    assert _parse_feedback_input("feedback output pathを修正") == "output pathを修正"
    assert _parse_feedback_input("f write A1 instead") == "write A1 instead"
    assert _parse_feedback_input("yes") is None


def test_build_parser_accepts_custom_llm_provider_modules() -> None:
    parser = build_parser(AppConfig())

    args = parser.parse_args(
        [
            "run",
            "画像を解析してJSON化して",
            "--llm-provider",
            "private_chatgpt",
            "--llm-provider-module",
            "private_repo.orchestra.chatgpt_provider",
            "--llm-chatgpt-url",
            "https://chatgpt.com/g/private-agent",
            "--llm-chatgpt-chrome-path",
            r"C:\Chrome\chrome.exe",
            "--llm-chatgpt-profile-dir",
            ".chatgpt-profile",
            "--llm-chatgpt-port",
            "9333",
        ]
    )

    assert args.llm_provider == "private_chatgpt"
    assert args.llm_provider_module == ["private_repo.orchestra.chatgpt_provider"]
    assert args.llm_chatgpt_url == "https://chatgpt.com/g/private-agent"
    assert args.llm_chatgpt_chrome_path == r"C:\Chrome\chrome.exe"
    assert args.llm_chatgpt_profile_dir == ".chatgpt-profile"
    assert args.llm_chatgpt_port == 9333
