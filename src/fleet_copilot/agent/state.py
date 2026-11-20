"""Graph state for a copilot session."""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # session-level (persist across turns)
    session_id: str
    tenant_id: str
    transcript: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    history: list[dict[str, Any]]
    # turn-level
    turn: int
    question: str
    guard: dict[str, Any]
    plan: dict[str, Any]
    reasoner_state: dict[str, Any]
    iterations: int
    reflections: int
    revisions: int
    pending_tool_calls: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    proposals: list[dict[str, Any]]
    draft: str | None
    stop_reason: str | None
    answer: str | None
    grounding: dict[str, Any] | None
    grounding_status: str | None
    status: str


TURN_RESET: dict[str, Any] = {
    "guard": {},
    "plan": {},
    "reasoner_state": {},
    "iterations": 0,
    "reflections": 0,
    "revisions": 0,
    "pending_tool_calls": [],
    "tool_calls": [],
    "proposals": [],
    "draft": None,
    "stop_reason": None,
    "answer": None,
    "grounding": None,
    "grounding_status": None,
    "status": "running",
}
