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
  actual_conditions: {
    reported_at: string;
    reported_by: string;
    observations: Record<string, unknown>;
  } | null;
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
  // null when the decision is withheld from this key.
  overrides_to_less_severe: boolean | null;
  confidence_note: string;
  outcome_note: string;
  status_note: string;
  agent_note: string;
  attributions: string[];
  // True when this key may not see human decisions: then status says nothing about one.
  decision_withheld: boolean;
  notice: string;
}

// A run that has no assessment yet (ADR 0019). It has no recommendation and is not a result.
export interface RunStatusView {
  id: string;
  status: "RUNNING" | "FAILED";
  created_at: string;
  failure_code: string | null;
  status_note: string;
  notice: string;
}

export interface EvidenceItem {
  id: string;
  category: string;
  source: string;
  source_version: string | null;
  retrieved_at: string;
  valid_at: string;
  valid_until: string | null;
  is_forecast: boolean;
  data_kind: string;
  upstream: string[];
  upstream_known: boolean;
  quality: string;
  quality_notes: string[];
  value: Record<string, unknown>;
}

export interface EvidenceEntry {
  item: EvidenceItem;
  effective_quality: string;
  retrieval_age_minutes_at_assessment: number | null;
  future_dated: boolean;
}

export interface EvidenceView {
  assessment_id: string;
  assessed_at: string;
  evidence: EvidenceEntry[];
  evidence_issues: string[];
  // Items that were NOT used or evaluated; absent in an older API response.
  withheld_evidence?: EvidenceItem[];
  withheld_note?: string;
  unevaluated_factors: string[];
  evidence_note: string;
  notice: string;
}

export interface SiteConstraint {
  id: string;
  factor: string;
  description: string;
  threshold_status: string;
  source: string | null;
}

export interface SiteView {
  site: {
    id: string;
    name: string;
    latitude: number;
    longitude: number;
    coordinate_source: string;
    area_names: string[];
    constraints: SiteConstraint[];
  };
  hazards_note: string;
  notice: string;
}

export interface SiteListView {
  sites: SiteView[];
  notice: string;
}
