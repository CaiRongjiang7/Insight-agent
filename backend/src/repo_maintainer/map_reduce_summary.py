"""
长上下文 Map-Reduce 压缩模块（LLM 抽取层）

当 Bug 报告原始内容超过 LLM 上下文窗口时，通过 Map-Reduce 策略：
1. Map：将长文本分块，逐块提取关键信息
2. Reduce：将所有块的摘要合并为最终结构化输出

这样可以处理包含大量日志、PR 讨论、commit 历史的超长文本。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class TextChunk:
    """文本分块"""
    index: int
    content: str
    source_type: str = "text"      # text / log / pr_body / commit_message
    char_start: int = 0
    char_end: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkSummary:
    """单块摘要结果"""
    chunk_index: int
    summary: str
    keywords: list[str] = field(default_factory=list)
    has_root_cause: bool = False
    has_fix_info: bool = False
    confidence: float = 0.0
    raw_response: str = ""


@dataclass
class MapReduceResult:
    """Map-Reduce 最终结果"""
    final_summary: str
    all_keywords: list[str]
    chunk_count: int
    token_estimate: int
    root_cause_chunks: list[int] = field(default_factory=list)
    fix_info_chunks: list[int] = field(default_factory=list)
    chunk_summaries: list[ChunkSummary] = field(default_factory=list)


class MapReduceSummarizer:
    """
    长上下文 Map-Reduce 压缩器

    用于 Bug 复盘场景中处理超长输入文本（日志、PR 讨论等）。
    将文本分块后逐块提取，最终合并为统一摘要，再传给后续 LLM 抽取步骤。
    """

    def __init__(
        self,
        chunk_size_chars: int = 3000,
        chunk_overlap_chars: int = 200,
        max_chunks: int = 20,
        map_prompt_template: str | None = None,
        reduce_prompt_template: str | None = None,
    ) -> None:
        self.chunk_size_chars = chunk_size_chars
        self.chunk_overlap_chars = chunk_overlap_chars
        self.max_chunks = max_chunks
        self.map_prompt_template = map_prompt_template or self._default_map_prompt()
        self.reduce_prompt_template = reduce_prompt_template or self._default_reduce_prompt()

    def split_text(
        self,
        text: str,
        source_type: str = "text",
    ) -> list[TextChunk]:
        """
        将长文本分块

        按段落边界切分，保留重叠区域以避免信息丢失。
        """
        if not text.strip():
            return []

        chunks: list[TextChunk] = []
        start = 0
        index = 0

        while start < len(text) and index < self.max_chunks:
            end = min(start + self.chunk_size_chars, len(text))

            # 尝试在段落边界切分
            if end < len(text):
                # 向后找段落分隔符
                split_pos = text.rfind("\n\n", start, end)
                if split_pos == -1:
                    split_pos = text.rfind("\n", start, end)
                if split_pos != -1 and split_pos > start + self.chunk_size_chars // 2:
                    end = split_pos + 1

            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(TextChunk(
                    index=index,
                    content=chunk_text,
                    source_type=source_type,
                    char_start=start,
                    char_end=end,
                ))
                index += 1

            # 下一块从 (end - overlap) 开始
            next_start = end - self.chunk_overlap_chars
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    def split_structured_content(
        self,
        pr_title: str = "",
        pr_body: str = "",
        commit_messages: list[str] | None = None,
        logs: str = "",
        chat_messages: list[str] | None = None,
    ) -> list[TextChunk]:
        """
        将结构化内容拆分为带来源标注的块

        各部分分别处理，便于后续抽取时绑定来源。
        """
        chunks: list[TextChunk] = []
        index = 0

        # PR 标题 + 描述
        if pr_title or pr_body:
            pr_content = f"## PR 描述\n标题：{pr_title}\n\n{pr_body}".strip()
            for chunk in self.split_text(pr_content, source_type="pr_body"):
                chunk.index = index
                chunks.append(chunk)
                index += 1

        # Commit 消息
        if commit_messages:
            commit_content = "## Commit Messages\n" + "\n---\n".join(
                f"- {msg}" for msg in commit_messages[:50]
            )
            for chunk in self.split_text(commit_content, source_type="commit_message"):
                chunk.index = index
                chunks.append(chunk)
                index += 1

        # 日志
        if logs and logs.strip():
            for chunk in self.split_text(logs, source_type="log"):
                chunk.index = index
                chunks.append(chunk)
                index += 1
                if index >= self.max_chunks:
                    break

        # 群聊消息
        if chat_messages:
            chat_content = "## 群聊讨论\n" + "\n".join(
                f"- {msg}" for msg in chat_messages[:30]
            )
            for chunk in self.split_text(chat_content, source_type="chat"):
                chunk.index = index
                chunks.append(chunk)
                index += 1

        return chunks[:self.max_chunks]

    def map_chunk(
        self,
        chunk: TextChunk,
        llm_call: Callable[[str], str] | None = None,
    ) -> ChunkSummary:
        """
        对单个块进行 Map 处理（提取关键信息）

        如果提供了 llm_call，则调用 LLM；否则使用启发式规则提取。
        """
        if llm_call is not None:
            prompt = self.map_prompt_template.format(
                source_type=chunk.source_type,
                chunk_index=chunk.index,
                content=chunk.content,
            )
            try:
                raw_response = llm_call(prompt)
                return self._parse_map_response(chunk.index, raw_response)
            except Exception:  # noqa: BLE001
                pass

        # 启发式规则提取（LLM 不可用时的兜底）
        return self._heuristic_map(chunk)

    def reduce(
        self,
        chunk_summaries: list[ChunkSummary],
        llm_call: Callable[[str], str] | None = None,
        original_title: str = "",
    ) -> MapReduceResult:
        """
        对所有块摘要进行 Reduce 处理（合并为最终摘要）

        如果提供了 llm_call，则调用 LLM；否则使用启发式合并。
        """
        all_keywords = list(dict.fromkeys(
            kw for summary in chunk_summaries for kw in summary.keywords
        ))
        root_cause_chunks = [s.chunk_index for s in chunk_summaries if s.has_root_cause]
        fix_info_chunks = [s.chunk_index for s in chunk_summaries if s.has_fix_info]

        if llm_call is not None:
            summaries_text = "\n\n".join(
                f"[块 {s.chunk_index}] {s.summary}" for s in chunk_summaries
            )
            prompt = self.reduce_prompt_template.format(
                title=original_title,
                chunk_count=len(chunk_summaries),
                summaries=summaries_text,
                keywords=", ".join(all_keywords[:20]),
            )
            try:
                raw_response = llm_call(prompt)
                final_summary = raw_response.strip()
            except Exception:  # noqa: BLE001
                final_summary = self._heuristic_reduce(chunk_summaries, original_title)
        else:
            final_summary = self._heuristic_reduce(chunk_summaries, original_title)

        token_estimate = len(final_summary) // 4

        return MapReduceResult(
            final_summary=final_summary,
            all_keywords=all_keywords[:30],
            chunk_count=len(chunk_summaries),
            token_estimate=token_estimate,
            root_cause_chunks=root_cause_chunks,
            fix_info_chunks=fix_info_chunks,
            chunk_summaries=chunk_summaries,
        )

    def run(
        self,
        text: str,
        source_type: str = "text",
        llm_call: Callable[[str], str] | None = None,
        original_title: str = "",
    ) -> MapReduceResult:
        """
        执行完整的 Map-Reduce 流程

        Args:
            text: 待压缩的原始文本
            source_type: 文本来源类型
            llm_call: LLM 调用函数（可选，不提供时使用启发式）
            original_title: 原始标题（用于 Reduce 提示词）

        Returns:
            MapReduceResult 包含最终摘要和关键词
        """
        chunks = self.split_text(text, source_type=source_type)

        if not chunks:
            return MapReduceResult(
                final_summary="",
                all_keywords=[],
                chunk_count=0,
                token_estimate=0,
            )

        # 如果只有一块，直接返回（不需要 Map-Reduce）
        if len(chunks) == 1:
            single_summary = self.map_chunk(chunks[0], llm_call)
            return MapReduceResult(
                final_summary=single_summary.summary,
                all_keywords=single_summary.keywords,
                chunk_count=1,
                token_estimate=len(single_summary.summary) // 4,
                root_cause_chunks=[0] if single_summary.has_root_cause else [],
                fix_info_chunks=[0] if single_summary.has_fix_info else [],
                chunk_summaries=[single_summary],
            )

        # Map 阶段
        chunk_summaries = [
            self.map_chunk(chunk, llm_call) for chunk in chunks
        ]

        # Reduce 阶段
        return self.reduce(chunk_summaries, llm_call, original_title)

    def run_structured(
        self,
        pr_title: str = "",
        pr_body: str = "",
        commit_messages: list[str] | None = None,
        logs: str = "",
        chat_messages: list[str] | None = None,
        llm_call: Callable[[str], str] | None = None,
    ) -> MapReduceResult:
        """对结构化内容执行 Map-Reduce"""
        chunks = self.split_structured_content(
            pr_title=pr_title,
            pr_body=pr_body,
            commit_messages=commit_messages,
            logs=logs,
            chat_messages=chat_messages,
        )

        if not chunks:
            return MapReduceResult(
                final_summary=pr_title or "",
                all_keywords=[],
                chunk_count=0,
                token_estimate=0,
            )

        chunk_summaries = [self.map_chunk(chunk, llm_call) for chunk in chunks]
        return self.reduce(chunk_summaries, llm_call, pr_title)

    def _heuristic_map(self, chunk: TextChunk) -> ChunkSummary:
        """启发式 Map（不依赖 LLM）"""
        text = chunk.content
        keywords = self._extract_keywords_heuristic(text)
        has_root_cause = any(
            kw in text.lower() for kw in [
                "root cause", "根本原因", "因为", "导致", "原因是",
                "caused by", "due to", "traceback", "error:", "exception:",
            ]
        )
        has_fix_info = any(
            kw in text.lower() for kw in [
                "fix", "修复", "解决", "resolved", "changed", "updated",
                "revert", "rollback", "patch", "merged",
            ]
        )
        # 截取前 300 字作为摘要
        summary = text[:300].strip()
        if len(text) > 300:
            summary += "..."

        return ChunkSummary(
            chunk_index=chunk.index,
            summary=summary,
            keywords=keywords[:10],
            has_root_cause=has_root_cause,
            has_fix_info=has_fix_info,
            confidence=0.5,
        )

    def _heuristic_reduce(
        self,
        summaries: list[ChunkSummary],
        title: str = "",
    ) -> str:
        """启发式 Reduce（不依赖 LLM）"""
        parts = []
        if title:
            parts.append(f"标题：{title}")

        # 优先包含有根本原因信息的块
        root_summaries = [s for s in summaries if s.has_root_cause]
        fix_summaries = [s for s in summaries if s.has_fix_info]

        if root_summaries:
            parts.append("【根本原因相关】")
            parts.extend(s.summary[:200] for s in root_summaries[:2])
        if fix_summaries:
            parts.append("【修复信息相关】")
            parts.extend(s.summary[:200] for s in fix_summaries[:2])

        # 补充其他块
        other_summaries = [
            s for s in summaries
            if not s.has_root_cause and not s.has_fix_info
        ]
        if other_summaries:
            parts.append("【其他信息】")
            parts.append(other_summaries[0].summary[:200])

        return "\n\n".join(parts) or (summaries[0].summary if summaries else "")

    def _extract_keywords_heuristic(self, text: str) -> list[str]:
        """启发式关键词提取"""
        # 提取技术词汇（驼峰/下划线/点号分隔）
        tech_pattern = re.compile(
            r"\b(?:"
            r"[A-Z][a-z]+(?:[A-Z][a-z]+)+"   # 驼峰词（如 ReactHook）
            r"|[a-z]+(?:_[a-z]+){1,}"          # 下划线词（如 use_effect）
            r"|[a-z]+(?:\.[a-z]+){1,}"         # 点号词（如 react.hooks）
            r"|Error|Exception|Warning|Failed"  # 错误类词
            r")\b"
        )
        tech_words = tech_pattern.findall(text)

        # 提取关键短语（fix/bug/error 附近的词）
        action_pattern = re.compile(
            r"\b(?:fix(?:ed)?|bug|error|issue|crash|fail(?:ed|ure)?|revert(?:ed)?)\s+(\w+)",
            re.IGNORECASE,
        )
        action_words = [m.group(1) for m in action_pattern.finditer(text)]

        # 合并去重，取高频词
        all_words = [w.lower() for w in tech_words + action_words]
        freq: dict[str, int] = {}
        for w in all_words:
            if len(w) > 3:
                freq[w] = freq.get(w, 0) + 1

        sorted_words = sorted(freq, key=lambda w: freq[w], reverse=True)
        return sorted_words[:10]

    def _parse_map_response(self, chunk_index: int, response: str) -> ChunkSummary:
        """解析 LLM Map 响应"""
        import json as _json

        # 尝试解析 JSON 格式
        json_match = re.search(r"\{.*\}", response, re.DOTALL)
        if json_match:
            try:
                data = _json.loads(json_match.group(0))
                return ChunkSummary(
                    chunk_index=chunk_index,
                    summary=str(data.get("summary", response[:300])),
                    keywords=list(data.get("keywords", [])),
                    has_root_cause=bool(data.get("has_root_cause", False)),
                    has_fix_info=bool(data.get("has_fix_info", False)),
                    confidence=float(data.get("confidence", 0.6)),
                    raw_response=response,
                )
            except Exception:  # noqa: BLE001
                pass

        # 降级为纯文本摘要
        return ChunkSummary(
            chunk_index=chunk_index,
            summary=response[:400].strip(),
            keywords=[],
            has_root_cause="root cause" in response.lower() or "根本原因" in response,
            has_fix_info="fix" in response.lower() or "修复" in response,
            confidence=0.5,
            raw_response=response,
        )

    @staticmethod
    def _default_map_prompt() -> str:
        return (
            "你是 Bug 复盘信息抽取专家。请从以下【{source_type}】内容（块 {chunk_index}）中提取关键信息。\n\n"
            "内容：\n{content}\n\n"
            "请以 JSON 格式输出：\n"
            "```json\n"
            "{{\n"
            '  "summary": "该块的核心信息摘要（100字内）",\n'
            '  "keywords": ["关键词1", "关键词2"],\n'
            '  "has_root_cause": true/false,\n'
            '  "has_fix_info": true/false,\n'
            '  "confidence": 0.0-1.0\n'
            "}}\n"
            "```"
        )

    @staticmethod
    def _default_reduce_prompt() -> str:
        return (
            "你是 Bug 复盘专家。以下是对 Bug 报告【{title}】进行分块分析的结果（共 {chunk_count} 块）：\n\n"
            "{summaries}\n\n"
            "关键词汇总：{keywords}\n\n"
            "请将以上信息整合为一段连贯的 Bug 复盘摘要（200字内），重点突出：\n"
            "1. 根本原因\n"
            "2. 影响范围\n"
            "3. 修复思路\n"
            "4. 预防措施\n\n"
            "只输出摘要文本，不要 JSON。"
        )


# 便捷工厂函数
def create_summarizer(
    chunk_size: int = 3000,
    max_chunks: int = 20,
) -> MapReduceSummarizer:
    """创建默认配置的 MapReduce 压缩器"""
    return MapReduceSummarizer(
        chunk_size_chars=chunk_size,
        max_chunks=max_chunks,
    )
