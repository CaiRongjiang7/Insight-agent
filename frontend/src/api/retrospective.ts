import { http } from "./http";

export type CoreStatus =
  | "received" | "collecting" | "retrieving" | "generating" | "validating"
  | "pending_review" | "rejected" | "approved" | "publishing" | "published" | "failed";

export interface CoreCitation { evidence_id: string; quote: string }
export interface CoreBackedText { content: string; citations: CoreCitation[] }
export interface CoreDocument {
  title: string;
  summary: string;
  impact: { scope: string; severity: "P0" | "P1" | "P2" | "P3"; affected_users?: string | null; citations: CoreCitation[] };
  root_cause: CoreBackedText;
  fix_solution: CoreBackedText;
  prevention: string[];
  related_modules: string[];
  keywords: string[];
}

export interface CoreIncident {
  id: string;
  title: string;
  description: string;
  repo_id: string;
  repo_name: string;
  incident_type: string;
  source_type: string;
  status: CoreStatus;
  trace_id: string;
  error: string;
  failed_stage: string;
  current_version: number;
  created_at: string;
  updated_at: string;
}

export interface CoreEvidence {
  id: string;
  source_type: string;
  content: string;
  location: string;
  source_url: string;
  selected: boolean;
  selection_reason: string;
}

export interface CoreVersion {
  id: string;
  version: number;
  kind: "ai" | "human";
  document: CoreDocument;
  quality: {
    hard_errors: string[];
    soft_warnings: string[];
    field_completeness: number;
    evidence_coverage: number;
    citation_accuracy: number;
    score: number;
  };
  actor: string;
  created_at: string;
}

export interface CoreDetail {
  incident: CoreIncident;
  evidence: CoreEvidence[];
  retrospective: CoreVersion | null;
  versions: CoreVersion[];
  review_actions: Array<Record<string, unknown>>;
}

export const retrospectiveApi = {
  status() {
    return http.get("/connectors/status").then((response) => response.data);
  },
  list(limit = 100) {
    return http.get<CoreIncident[]>("/retrospective/incidents", { params: { limit } }).then((response) => response.data);
  },
  create(payload: Record<string, unknown>) {
    return http.post<CoreIncident>("/retrospective/incidents", payload).then((response) => response.data);
  },
  detail(id: string) {
    return http.get<CoreDetail>(`/retrospective/incidents/${id}`).then((response) => response.data);
  },
  run(id: string) {
    return http.post(`/retrospective/incidents/${id}/run`).then((response) => response.data);
  },
  retry(id: string) {
    return http.post(`/retrospective/incidents/${id}/retry`).then((response) => response.data);
  },
  regenerate(id: string) {
    return http.post(`/retrospective/incidents/${id}/regenerate`).then((response) => response.data);
  },
  edit(id: string, actor: string, document: CoreDocument) {
    return http.patch(`/retrospective/incidents/${id}/document`, { actor, document }).then((response) => response.data);
  },
  approve(id: string, actor: string, reason = "") {
    return http.post(`/retrospective/incidents/${id}/approve`, { actor, reason }).then((response) => response.data);
  },
  reject(id: string, actor: string, reason: string) {
    return http.post(`/retrospective/incidents/${id}/reject`, { actor, reason }).then((response) => response.data);
  },
  publish(id: string, actor: string) {
    return http.post(`/retrospective/incidents/${id}/publish`, { actor }).then((response) => response.data);
  },
  search(q: string, repoId = "", incidentType = "") {
    return http.get("/retrospective/knowledge/search", {
      params: { q, repo_id: repoId, incident_type: incidentType },
    }).then((response) => response.data);
  },
};
