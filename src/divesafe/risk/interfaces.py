"""Risk-engine interfaces. The engine is deterministic: no I/O, no LLM, no randomness."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from divesafe.domain import DivePlan, EvidenceItem, RiskAssessment, RuleResult


class Rule(Protocol):
    """A pure check of one risk factor. Returns None when the rule does not apply.

    A rule with a numeric limit must set `threshold_status` on its result; a limit that is not
    VALIDATED can never produce GO or CAUTION (enforced by `RuleResult`).
    """

    @property
    def rule_id(self) -> str: ...

    @property
    def citation(self) -> str: ...

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None: ...


class RiskEngine(Protocol):
    """Evaluates a plan against evidence. Authoritative: nothing downstream may relax it."""

    def assess(
        self, plan: DivePlan, evidence: Sequence[EvidenceItem], now: datetime
    ) -> RiskAssessment: ...
