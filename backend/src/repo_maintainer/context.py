from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import PlatformConfig
from .models import CompressedError, ContextBundle, IncidentReport, RepoSnapshot, Skill


class LayeredContextManager:
    def __init__(self, config: PlatformConfig) -> None:
        self.config = config

    def build_context(
        self,
        incident: IncidentReport,
        compressed_error: CompressedError,
        snapshot: RepoSnapshot,
        relevant_files: list[str],
        matched_skills: list[Skill],
        short_term_memories: list[dict[str, Any]],
        long_term_memories: list[dict[str, Any]],
    ) -> ContextBundle:
        budget_allocations = self._allocate_budget()
        symbol_files_by_name = self._symbol_files_by_name(snapshot)
        working_context = {
            "repo_root": snapshot.root_dir,
            "incident_title": incident.title,
            "incident_description": incident.description,
            "semantic_summary": compressed_error.semantic_summary,
            "logs_excerpt": incident.logs[: budget_allocations["logs"] * 4],
            "relevant_file_summaries": {
                path: snapshot.file_summaries.get(path, "")
                for path in relevant_files
            },
            "dependency_slice": [
                edge
                for edge in snapshot.dependency_edges
                if edge[0] in relevant_files or edge[1] in relevant_files
            ][:20],
            "call_slice": [
                edge
                for edge in snapshot.call_edges
                if edge[0].split(":")[0] in relevant_files or bool(symbol_files_by_name.get(str(edge[1]).lower(), set()) & set(relevant_files))
            ][:20],
            "symbol_slice": [
                {
                    "symbol_id": symbol.symbol_id,
                    "name": symbol.name,
                    "kind": symbol.kind,
                    "file_path": symbol.file_path,
                    "line": symbol.line,
                }
                for symbol in snapshot.symbols
                if symbol.file_path in relevant_files
            ][:20],
            "graph_metadata": asdict(snapshot.graph_metadata) if snapshot.graph_metadata is not None else {},
            "language_breakdown": snapshot.language_breakdown,
            "data_flow_slice": [
                {"source": edge.source, "target": edge.target, "edge_type": edge.edge_type}
                for edge in snapshot.graph_edges
                if edge.edge_type == "data_flow"
                and any(path in {edge.source.split(":")[0], edge.target.split(":")[0]} for path in relevant_files)
            ][:20],
            "control_flow_slice": [
                {"source": edge.source, "target": edge.target, "edge_type": edge.edge_type}
                for edge in snapshot.graph_edges
                if edge.edge_type == "controls" and edge.source.split(":")[0] in relevant_files
            ][:20],
            "neighbor_files": self._neighbor_files(snapshot, relevant_files),
            "root_cause_paths": self._root_cause_paths(snapshot, relevant_files),
        }
        semantic_focus = list(dict.fromkeys(compressed_error.keywords + [compressed_error.root_cause_cluster]))[:8]
        return ContextBundle(
            working_context=working_context,
            short_term_memories=self._summarize_memories(short_term_memories, budget_allocations["short_term"]),
            long_term_memories=self._summarize_memories(long_term_memories, budget_allocations["long_term"]),
            matched_skills=matched_skills,
            relevant_files=relevant_files,
            budget_allocations=budget_allocations,
            semantic_focus=semantic_focus,
        )

    def _allocate_budget(self) -> dict[str, int]:
        total = self.config.token_budget.total_budget
        working = int(total * self.config.token_budget.working_ratio)
        short_term = int(total * self.config.token_budget.short_term_ratio)
        long_term = int(total * self.config.token_budget.long_term_ratio)
        skill = total - working - short_term - long_term
        return {
            "working": working,
            "short_term": short_term,
            "long_term": long_term,
            "skill": skill,
            "logs": max(working // 6, 300),
        }

    def _summarize_memories(self, memories: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        summarized: list[dict[str, Any]] = []
        remaining_chars = budget * 4
        for memory in memories:
            summary = str(memory.get("repair_summary", memory.get("summary", "")))
            brief = {
                "trace_id": memory.get("trace_id") or memory.get("id"),
                "incident_type": memory.get("incident_type"),
                "cluster": memory.get("cluster", ""),
                "repair_summary": summary[: max(0, remaining_chars)],
                "success": memory.get("success", True),
            }
            remaining_chars -= len(brief["repair_summary"])
            summarized.append(brief)
            if remaining_chars <= 0:
                break
        return summarized

    def _symbol_files_by_name(self, snapshot: RepoSnapshot) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}
        for symbol in snapshot.symbols:
            mapping.setdefault(symbol.name.lower(), set()).add(symbol.file_path)
        return mapping

    def _neighbor_files(self, snapshot: RepoSnapshot, relevant_files: list[str]) -> list[str]:
        relevant_set = set(relevant_files)
        neighbors: list[str] = []
        seen: set[str] = set(relevant_set)
        symbol_files_by_name = self._symbol_files_by_name(snapshot)

        for source, target in snapshot.call_edges:
            source_file = source.split(":")[0] if ":" in source else source
            target_files = symbol_files_by_name.get(str(target).lower(), set())
            if source_file in relevant_set:
                for target_file in sorted(target_files):
                    if target_file not in seen:
                        neighbors.append(target_file)
                        seen.add(target_file)
            if target_files & relevant_set and source_file not in seen:
                neighbors.append(source_file)
                seen.add(source_file)

        relevant_stems = {Path(path).stem.lower().replace("-", "_") for path in relevant_set}
        for source, target in snapshot.dependency_edges:
            target_name = str(target).split(".")[-1].lower().replace("-", "_")
            if source in relevant_set:
                for file_path in snapshot.files:
                    stem = Path(file_path).stem.lower().replace("-", "_")
                    if stem == target_name and file_path not in seen:
                        neighbors.append(file_path)
                        seen.add(file_path)
            if target_name in relevant_stems and source not in seen:
                neighbors.append(source)
                seen.add(source)
        return neighbors[:12]

    def _root_cause_paths(self, snapshot: RepoSnapshot, relevant_files: list[str]) -> list[dict[str, Any]]:
        if len(relevant_files) < 2:
            return []
        adjacency: dict[str, set[str]] = {}
        symbol_files_by_name = self._symbol_files_by_name(snapshot)
        for source, target in snapshot.call_edges:
            source_file = source.split(":")[0] if ":" in source else source
            for target_file in symbol_files_by_name.get(str(target).lower(), set()):
                adjacency.setdefault(source_file, set()).add(target_file)
                adjacency.setdefault(target_file, set()).add(source_file)
        for source, target in snapshot.dependency_edges:
            target_name = str(target).split(".")[-1].lower().replace("-", "_")
            for file_path in snapshot.files:
                if Path(file_path).stem.lower().replace("-", "_") != target_name:
                    continue
                adjacency.setdefault(source, set()).add(file_path)
                adjacency.setdefault(file_path, set()).add(source)

        paths: list[dict[str, Any]] = []
        seen_paths: set[tuple[str, ...]] = set()
        for start in relevant_files[:3]:
            for end in relevant_files[:5]:
                if start == end:
                    continue
                path = self._shortest_path(adjacency, start, end, max_depth=4)
                if not path or len(path) < 2:
                    continue
                path_key = tuple(path)
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                paths.append({"start": start, "end": end, "path": path, "hops": len(path) - 1})
        return paths[:6]

    def _shortest_path(
        self,
        adjacency: dict[str, set[str]],
        start: str,
        end: str,
        *,
        max_depth: int,
    ) -> list[str]:
        queue: list[tuple[str, list[str]]] = [(start, [start])]
        seen = {start}
        while queue:
            node, path = queue.pop(0)
            if node == end:
                return path
            if len(path) - 1 >= max_depth:
                continue
            for neighbor in sorted(adjacency.get(node, set())):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
        return []
