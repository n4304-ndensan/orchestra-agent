# orchestra-agent

Config-driven orchestration agent framework built on LangGraph and MCP.


## What is this?

`orchestra-agent` is a lightweight orchestration engine for building high-reliability AI agents.

You configure:

- LLM provider
- MCP servers
- RAG (optional)
- Approval policy
- Safety rules

And the agent runs.

No hardcoded workflows.
No tight coupling to tools.
Human-in-the-loop by default.

## Core Concepts

- **LLM** → makes decisions
- **LangGraph** → controls workflow
- **MCP** → executes tools
- **RAG (via MCP)** → enriches context
- **Policy** → enforces safety
- **Approval** → requires human validation


## Basic Flow

1. Parse intent
2. (Optional) RAG enrichment
3. Create plan
4. Human approval
5. Execute via MCP
6. Log results


## Philosophy

- Configuration first
- Replaceable components
- Clear responsibility separation
- Safe by default



## Status

Work in progress.
