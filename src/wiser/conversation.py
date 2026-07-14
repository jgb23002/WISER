"""Conversational-memory primitives shared by the SQL AI agent and surfaces.

The agent itself stays stateless: surfaces (CLI REPL, Streamlit) own the
conversation and pass a sequence of ConversationTurn into each call.
"""

from __future__ import annotations

from dataclasses import dataclass

from wiser.agents.task_routing_agent import Route, RouteDecision

# Caps how many prior turns reach the LLM prompt; UI transcripts are uncapped.
MAX_HISTORY_TURNS = 5


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    sql: str
    answer: str


def resolve_route(decision: RouteDecision, last_route: Route | None) -> RouteDecision:
    """Sticky route: an UNKNOWN follow-up after an analytics turn stays in analytics.

    TaskRoutingAgent remains context-blind; only the dispatch layer applies this.
    """
    if decision.route is Route.UNKNOWN and last_route is Route.WEATHER_ANALYTICS:
        return RouteDecision(
            route=Route.WEATHER_ANALYTICS,
            reason="Follow-up to previous analytics question (sticky route).",
        )
    return decision
