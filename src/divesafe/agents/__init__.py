"""Specialist agents (Site Intelligence, Weather, Ocean, Tide/Current, Risk Assessment, Prediction).

Agents read evidence and emit structured, evidence-referenced findings. They never call data
sources directly (use `divesafe.data` connectors) and never decide the final recommendation
(see `divesafe.risk.reconcile`). Design: docs/agent-design.md.
"""
