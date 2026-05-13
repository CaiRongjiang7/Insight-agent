from __future__ import annotations

import re
from collections import Counter

from .models import CompressedError, IncidentReport, IncidentType


class ErrorSemanticCompressor:
    PYTHON_FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([^\n]+)')
    DEP_CONFLICT = re.compile(r"(requires|conflict|ResolutionImpossible|Cannot install|version solving failed)", re.IGNORECASE)
    MODULE_NOT_FOUND = re.compile(r"(ModuleNotFoundError|No module named|Cannot find module)", re.IGNORECASE)
    TEST_HINT = re.compile(r"(pytest|FAILED|test_[A-Za-z0-9_]+)", re.IGNORECASE)
    SPECIAL_TOKEN = re.compile(r"(ModuleNotFoundError|ResolutionImpossible|AssertionError|ImportError|TypeError|ValueError)")
    VERSION_TOKEN = re.compile(r"[A-Za-z0-9_.-]+(?:==|>=|<=|~=|!=)[A-Za-z0-9_.-]+")
    WORD_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{2,}")

    def compress(self, incident: IncidentReport) -> CompressedError:
        text = "\n".join([incident.title, incident.description, incident.logs])
        key_frames = self._extract_frames(text)
        error_name = self._extract_error_name(text)
        error_type = self._classify(text, error_name)
        keywords = self._extract_keywords(text)
        cluster = self._root_cause_cluster(error_type, error_name, keywords, key_frames)
        summary = self._build_summary(error_type, error_name, key_frames, keywords, cluster)
        confidence = self._confidence(error_type, key_frames, keywords)
        return CompressedError(
            error_type=error_type,
            error_name=error_name,
            keywords=keywords,
            key_stack_frames=key_frames,
            root_cause_cluster=cluster,
            semantic_summary=summary,
            confidence=confidence,
        )

    def _extract_frames(self, text: str) -> list[str]:
        frames = []
        for file_name, line_no, func_name in self.PYTHON_FRAME.findall(text):
            frames.append(f"{file_name}:{line_no}:{func_name.strip()}")
        return frames[:6]

    def _extract_error_name(self, text: str) -> str:
        for pattern in (
            r"([A-Za-z]+Error)",
            r"([A-Za-z]+Exception)",
            r"(ResolutionImpossible)",
            r"(ModuleNotFoundError)",
        ):
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return "UnknownFailure"

    def _classify(self, text: str, error_name: str) -> IncidentType:
        lowered = text.lower()
        if self.DEP_CONFLICT.search(text) or "requirements" in lowered or "pyproject" in lowered:
            return IncidentType.DEPENDENCY_CONFLICT
        if "cross module" in lowered or ("service" in lowered and "repository" in lowered and len(self._extract_frames(text)) > 1):
            return IncidentType.CROSS_MODULE_DEFECT
        if self.TEST_HINT.search(text) or error_name == "AssertionError":
            return IncidentType.TEST_REGRESSION
        if self.MODULE_NOT_FOUND.search(text) or "traceback" in lowered or "ci" in lowered:
            return IncidentType.CI_FAILURE
        return IncidentType.UNKNOWN

    def _extract_keywords(self, text: str) -> list[str]:
        specials = [token.lower() for token in self.SPECIAL_TOKEN.findall(text)]
        versions = self.VERSION_TOKEN.findall(text)
        words = [token.lower() for token in self.WORD_TOKEN.findall(text)]
        filtered = [
            token
            for token in words
            if token not in {"traceback", "error", "failed", "line", "file", "test", "tests", "python"}
        ]
        counter = Counter((specials * 3) + versions + filtered)
        return [keyword for keyword, _ in counter.most_common(8)]

    def _root_cause_cluster(
        self,
        error_type: IncidentType,
        error_name: str,
        keywords: list[str],
        key_frames: list[str],
    ) -> str:
        anchors = [error_type.value, error_name.lower()]
        if keywords:
            anchors.extend(keywords[:2])
        if key_frames:
            anchors.append(key_frames[0].split(":")[0].split("/")[-1])
        return "::".join(anchors[:4])

    def _build_summary(
        self,
        error_type: IncidentType,
        error_name: str,
        key_frames: list[str],
        keywords: list[str],
        cluster: str,
    ) -> str:
        frame_part = ", ".join(key_frames[:2]) if key_frames else "no explicit stack frame"
        keyword_part = ", ".join(keywords[:4]) if keywords else "no salient keywords"
        return (
            f"Detected {error_type.value} with primary error `{error_name}`; "
            f"frames: {frame_part}; keywords: {keyword_part}; cluster: {cluster}."
        )

    def _confidence(self, error_type: IncidentType, key_frames: list[str], keywords: list[str]) -> float:
        confidence = 0.35
        if error_type != IncidentType.UNKNOWN:
            confidence += 0.25
        if key_frames:
            confidence += 0.2
        if len(keywords) >= 3:
            confidence += 0.15
        return min(confidence, 0.95)
