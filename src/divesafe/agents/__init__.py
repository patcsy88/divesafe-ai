"""Specialist agents, bounded Tree-of-Thought scenarios and the risk-assessment proposal.

Agents read evidence and return structured, evidence-referenced output. They never fetch data
(see `divesafe.data`) and never decide: the final recommendation comes only from
`divesafe.risk.reconcile`. Design: docs/agent-design.md and docs/tot-design.md.
"""

from divesafe.agents.prompts import PROMPT_VERSION
from divesafe.agents.runner import AgentError
from divesafe.agents.stage import AgentStageResult, run_agent_stage

__all__ = ["PROMPT_VERSION", "AgentError", "AgentStageResult", "run_agent_stage"]
