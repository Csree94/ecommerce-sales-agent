"""LangGraph definition for the sales agent.

Topology (audit §K, extended by milestone 3B):

    START → classify_intent → gather_context → purchase → compose_reply → persist → END

- ``purchase`` is conditionally gated: it returns ``{}`` for every
  non-purchase turn (no tool calls, no state change) and runs the
  single-attempt Inventra stock-out flow only for explicit purchase
  requests (see ``app.agents.purchase``).

- The graph contains ONLY orchestration: no HTTP, no URLs, no auth, no engine
  creation. Dependencies (InventraClient, Database) are injected via closures.
- Tools are called from ``gather_context`` via the typed tool layer; tool
  failures become structured state, never exceptions.
- ``persist`` records the completed turn (AgentRun + customer/agent Messages)
  using the injected ``Database`` and a short-lived session; persistence
  problems are recorded in state metadata, never crash the turn.
- The LLM gateway (Gemini primary / Nemotron fallback) is injected into
  ``compose_reply`` exactly like the Inventra client — provider transport
  details stay inside ``app.integrations.llm``. When every provider fails,
  compose degrades to its deterministic safety net and the turn completes.
- LangGraph-native checkpointing (PostgresSaver) remains deferred — requires
  ``langgraph-checkpoint-postgres`` and separate architectural sign-off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.classify import classify_intent
from app.agents.compose import compose_reply
from app.agents.gather import gather_context
from app.agents.persist import persist_turn
from app.agents.purchase import purchase
from app.agents.state import AgentState
from app.integrations.llm import LLMGateway


def build_graph(
    client: Any,
    database: Any = None,
    gateway: LLMGateway | None = None,
) -> CompiledStateGraph:
    """Compile the sales-agent graph with injected dependencies.

    ``client`` is the InventraClient; ``database`` the optional Database
    holder (persist is skipped cleanly when None — e.g. ad-hoc usage);
    ``gateway`` the optional LLMGateway (a config-built default is used when
    omitted — inject a fake in tests to avoid real provider calls). All are
    injected via closures so nodes never construct infrastructure and tests
    can mock at these exact seams.
    """

    if gateway is None:
        from app.integrations.llm import get_llm_gateway

        gateway = get_llm_gateway()

    async def gather(state: AgentState) -> dict[str, Any]:
        return await gather_context(state, client)

    async def purchase_node(state: AgentState) -> dict[str, Any]:
        return await purchase(state, client, database)

    async def compose(state: AgentState) -> dict[str, Any]:
        return await compose_reply(state, gateway)

    async def persist(state: AgentState) -> dict[str, Any]:
        # run_turn stamps turn_started_at; direct graph.ainvoke callers may not.
        state.metadata.setdefault("turn_started_at", datetime.now(UTC).timestamp())
        return persist_turn(state, database)

    graph = StateGraph(AgentState)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("gather_context", gather)
    graph.add_node("purchase", purchase_node)
    graph.add_node("compose_reply", compose)
    graph.add_node("persist", persist)

    graph.add_edge(START, "classify_intent")
    graph.add_edge("classify_intent", "gather_context")
    graph.add_edge("gather_context", "purchase")
    graph.add_edge("purchase", "compose_reply")
    graph.add_edge("compose_reply", "persist")
    graph.add_edge("persist", END)

    return graph.compile()


async def run_turn(
    client: Any,
    *,
    customer_message: str,
    conversation_id: str | None = None,
    database: Any = None,
    gateway: LLMGateway | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> AgentState:
    """Execute one agent turn end-to-end and return the final state.

    ``database`` is forwarded to the graph; when omitted, the turn runs
    without persistence (``metadata.persisted`` stays false). ``gateway`` is
    forwarded likewise; when omitted, the default config-built gateway is
    used (``build_graph`` resolves it lazily). ``metadata_extra`` is merged
    into state metadata (channel-specific ids the persist node may stamp on
    messages — never secrets).
    """
    graph = build_graph(client, database, gateway)
    metadata = {"turn_started_at": datetime.now(UTC).timestamp()}
    if metadata_extra:
        metadata.update(metadata_extra)
    initial = AgentState(
        customer_message=customer_message,
        conversation_id=conversation_id,
        metadata=metadata,
    )
    result = await graph.ainvoke(
        initial, config={"configurable": {"thread_id": conversation_id or "adhoc"}}
    )
    return AgentState.model_validate(result)
