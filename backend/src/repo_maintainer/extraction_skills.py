"""
Extraction Skills — pre-processing hooks that run before LLM extraction.

Each skill enriches the LLM prompt with additional context, improving
extraction quality by grounding the model in prior knowledge.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .knowledge_base import KnowledgeBase
from .models import BugReport, KnowledgeEntry, PreparedIncident


@dataclass
class SkillContext:
    """Enriched context produced by an extraction skill."""

    skill_name: str
    matched_entries: list[KnowledgeEntry] = field(default_factory=list)
    extra_prompt_sections: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt_section(self) -> str:
        """Render matched entries as a prompt section for the LLM."""
        if not self.matched_entries:
            return ""
        lines = [
            f"### 历史相似案例（来自 {self.skill_name}）",
            "以下是知识库中与本 Bug 相似的历史案例，请参考其根因分析和预防措施：",
            "",
        ]
        for i, entry in enumerate(self.matched_entries, 1):
            lines.append(f"**案例 {i}：{entry.name}** (ID: {entry.id})")
            lines.append(f"- 描述：{entry.description}")
            if entry.keywords:
                lines.append(f"- 关键词：{', '.join(entry.keywords)}")
            if entry.error_types:
                lines.append(f"- 错误类型：{', '.join(entry.error_types)}")
            if entry.action_template:
                lines.append(f"- 处理模板：{entry.action_template[:200]}")
            lines.append("")
        lines.append(
            "注意：以上案例仅供参考，不要照搬结论。"
            "必须基于当前 Bug 报告的原始内容进行独立判断。"
        )
        return "\n".join(lines)


class KnowledgeMatchSkill:
    """
    Search the knowledge base for similar cases BEFORE LLM extraction.

    This skill:
    1. Extracts search tokens from the BugReport (title, description, keywords)
    2. Queries the knowledge base via BM25 keyword search
    3. Returns matched entries as context for the LLM prompt

    This improves extraction quality by:
    - Helping the LLM identify common root cause patterns
    - Providing reference prevention measures
    - Reducing hallucination by anchoring to known cases
    """

    name = "knowledge_match"

    def __init__(self, knowledge_base: KnowledgeBase, top_k: int = 3) -> None:
        self.knowledge_base = knowledge_base
        self.top_k = top_k

    def run(self, bug_report: BugReport | PreparedIncident) -> SkillContext:
        """
        Execute the skill: search knowledge base for similar cases.

        Args:
            bug_report: The incoming bug report to match against.

        Returns:
            SkillContext with matched entries and a prompt section.
        """
        report = self._unwrap_report(bug_report)
        tokens = self._extract_search_tokens(report)
        if not tokens:
            return SkillContext(skill_name=self.name)

        query = " ".join(tokens)
        matched = self.knowledge_base.search_by_keyword(query, limit=self.top_k)

        # Filter out low-relevance matches (entries whose ID appears in
        # the report's metadata as already-published, to avoid circular ref)
        exclude_ids = set(report.metadata.get("published_knowledge_ids", [])) if report.metadata else set()
        matched = [e for e in matched if e.id not in exclude_ids]

        ctx = SkillContext(
            skill_name=self.name,
            matched_entries=matched,
            metadata={
                "query": query,
                "match_count": len(matched),
                "excluded_count": len(exclude_ids),
            },
        )
        return ctx

    def _unwrap_report(self, bug_report: BugReport | PreparedIncident) -> BugReport:
        """Extract the underlying BugReport from either type."""
        if isinstance(bug_report, PreparedIncident):
            return bug_report.incident
        return bug_report

    def _extract_search_tokens(self, report: BugReport) -> list[str]:
        """
        Build search tokens from the bug report.

        Strategy:
        - Use keywords from metadata if available (highest quality)
        - Fall back to tokenizing title + description
        - Deduplicate and limit token count
        """
        tokens: list[str] = []

        # Priority 1: keywords from metadata (if LLM or human already tagged)
        meta_keywords = []
        if report.metadata:
            meta_keywords = report.metadata.get("keywords", [])
            if isinstance(meta_keywords, list):
                tokens.extend(str(k) for k in meta_keywords)

        # Priority 2: title words (important signal)
        if report.title:
            # Split on common delimiters, keep tokens > 2 chars
            title_tokens = [
                t for t in report.title.replace(":", " ").replace("(", " ").replace(")", " ").split()
                if len(t) > 2
            ]
            tokens.extend(title_tokens)

        # Priority 3: error type / bug category
        if report.metadata:
            error_type = report.metadata.get("error_type", "")
            bug_category = report.metadata.get("bug_category", "")
            if error_type:
                tokens.append(str(error_type))
            if bug_category:
                tokens.append(str(bug_category))

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for t in tokens:
            lower = t.lower()
            if lower not in seen:
                seen.add(lower)
                unique.append(t)

        # Limit to top 10 tokens to avoid overly broad queries
        return unique[:10]
