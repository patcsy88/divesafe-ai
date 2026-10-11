// A small subset of the API's response, enough to render it. The UI never computes a
// recommendation: it displays `record.final_recommendation` as the API returned it.
export type Recommendation = "GO" | "CAUTION" | "NO-GO" | "INSUFFICIENT EVIDENCE";

export interface RuleResult {
  rule_id: string;
  outcome: Recommendation;
  rationale: string;
  factor: string | null;
  threshold_status: string | null;
}

export interface Finding {
  agent: string;
  summary: string;
  risk_factors: string[];
  conflicting_signals: string[];
}

export interface ScenarioItem {
  kind: string;
  summary: string;
  risk_factors: string[];
  conflicting_signals: string[];
}

export interface AssessmentRecord {
  id: string;
  created_at: string;
  plan: {
    site_id: string;
    planned_start: string;
    planned_duration_minutes: number;
    max_depth_m: number;
  };
  rule_results: RuleResult[];
  deterministic_recommendation: Recommendation;
  proposed_recommendation: Recommendation | null;
  final_recommendation: Recommendation;
  confidence: number | null;
  unevaluated_factors: string[];
  evidence_issues: string[];
  findings: Finding[];
  scenarios: ScenarioItem[];
  explanation: string;
  agent_issues: string[];
  ruleset_version: string;
  created_by: string | null;
  assigned_decider: string | null;
  human_decision: {
    decided_by: string;
    decision: Recommendation;
    decided_at: string;
    is_override: boolean;
    override_rationale: string | null;
  } | null;
}

export interface AssessmentView {
  record: AssessmentRecord;
  status: string;
  llm_tightened: boolean;
  confidence_note: string;
  outcome_note: string;
  status_note: string;
  agent_note: string;
  attributions: string[];
  notice: string;
}
