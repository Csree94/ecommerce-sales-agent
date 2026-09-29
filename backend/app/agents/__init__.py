"""Agent orchestration (LangGraph).

Topology: START → classify_intent → gather_context → compose_reply → persist → END.

- The graph orchestrates; it contains no HTTP, no URLs, no auth, no DB code.
- Tool dispatch lives in ``gather_context`` over the read-only tools in
  ``app.tools.inventra``; tool failures become structured state.
- ``classify_intent`` is still the deterministic keyword classifier (LLM-based
  classification is a later phase). ``compose_reply`` now generates through
  the LLM gateway (audit §F: Gemini primary / Nemotron fallback) and degrades
  to a deterministic safety net when every provider fails.
- ``persist`` records the completed turn (AgentRun + customer/agent Messages)
  via the injected ``Database``; persistence problems land in state metadata,
  never crash the turn. LangGraph-native checkpointing (PostgresSaver) stays
  deferred — separate decision, ``langgraph-checkpoint-postgres`` not added.
"""

from app.agents.classify import classify_intent
from app.agents.compose import compose_reply
from app.agents.gather import gather_context
from app.agents.graph import build_graph, run_turn
from app.agents.persist import persist_turn
from app.agents.state import AgentState, IntentName, ToolErrorCode, ToolFailure

__all__ = [
    "AgentState",
    "IntentName",
    "ToolErrorCode",
    "ToolFailure",
    "build_graph",
    "classify_intent",
    "compose_reply",
    "gather_context",
    "persist_turn",
    "run_turn",
]
