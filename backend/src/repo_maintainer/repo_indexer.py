from __future__ import annotations

from pathlib import Path
import re

from .config import GraphConfig
from .language_analysis import LanguageAnalyzerSuite
from .models import CompressedError, IncidentReport, RepoSnapshot


class RepositoryIndexer:
    FILE_REFERENCE = re.compile(
        r"(?:(?:File\s+\"(?P<quoted>[^\"]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))\")|"
        r"(?P<plain>[A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|json|toml|ya?ml|txt|md))"
        r"(?::\d+|::[A-Za-z_][A-Za-z0-9_]*|\s|$))",
        re.IGNORECASE,
    )
    SYMBOL_REFERENCE = re.compile(
        r"(?:where\s+.*?=\s+|where\s+False\s+=\s+|E\s+\+\s+where\s+.*?=\s+)"
        r"(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.IGNORECASE,
    )
    MODULE_OBJECT_REFERENCE = re.compile(r"<(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\.(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s+object")

    SUPPORTED_SUFFIXES = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
    }
    EXCLUDED_DIRS = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "runtime",
        "node_modules",
        "dist",
        "build",
    }

    def __init__(self, graph_config: GraphConfig | None = None) -> None:
        self.graph_config = graph_config or GraphConfig()
        self.language_suite = LanguageAnalyzerSuite(self.graph_config)

    def index(self, root_dir: str | Path) -> RepoSnapshot:
        root = Path(root_dir).resolve()
        self.language_suite.reset_session()
        files: list[str] = []
        summaries: dict[str, str] = {}
        dependency_edges: list[tuple[str, str]] = []
        call_edges: list[tuple[str, str]] = []
        language_breakdown: dict[str, int] = {}
        symbols = []
        graph_edges = []

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(root).parts
            if any(part in self.EXCLUDED_DIRS for part in relative_parts):
                continue
            if path.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                continue

            relative = path.relative_to(root).as_posix()
            files.append(relative)
            language_breakdown[path.suffix.lower()] = language_breakdown.get(path.suffix.lower(), 0) + 1

            text = self._safe_read(path)
            if text is None:
                continue

            summaries[relative] = self._summarize_text(text)
            analysis = self.language_suite.analyze(relative, path.suffix, text)
            dependency_edges.extend(analysis.dependency_edges)
            call_edges.extend(analysis.call_edges)
            symbols.extend(analysis.symbols)
            graph_edges.extend(analysis.graph_edges)

        graph_metadata = self.language_suite.build_graph_metadata(files, graph_edges)
        return RepoSnapshot(
            root_dir=str(root),
            files=sorted(files),
            file_summaries=summaries,
            dependency_edges=dependency_edges,
            call_edges=call_edges,
            language_breakdown=language_breakdown,
            symbols=symbols,
            graph_edges=graph_edges,
            graph_metadata=graph_metadata,
        )

    def relevant_files(
        self,
        snapshot: RepoSnapshot,
        incident: IncidentReport,
        compressed_error: CompressedError,
        limit: int = 8,
    ) -> list[str]:
        scores: dict[str, int] = {path: 0 for path in snapshot.files}
        raw_log_blob = " ".join(
            [
                incident.title,
                incident.description,
                incident.logs,
                " ".join(incident.changed_files),
                " ".join(incident.suspected_modules),
                " ".join(compressed_error.key_stack_frames),
            ]
        )
        log_blob = raw_log_blob.lower()
        referenced_paths = self._extract_referenced_paths(raw_log_blob)
        referenced_symbols = self._extract_referenced_symbols(raw_log_blob)
        referenced_modules = self._extract_referenced_modules(raw_log_blob)
        keywords = {keyword.lower() for keyword in compressed_error.keywords}
        symbols_by_file: dict[str, list] = {}
        for symbol in snapshot.symbols:
            symbols_by_file.setdefault(symbol.file_path, []).append(symbol)

        for file_path in snapshot.files:
            basename = Path(file_path).name.lower()
            stem = Path(file_path).stem.lower().replace("-", "_")
            summary = snapshot.file_summaries.get(file_path, "").lower()
            if self._matches_referenced_path(file_path, referenced_paths):
                scores[file_path] += 10
            if stem in referenced_modules:
                scores[file_path] += 8
            if basename in log_blob:
                scores[file_path] += 5
            if file_path.lower() in log_blob:
                scores[file_path] += 6
            if any(module.lower().replace(".", "/") in file_path.lower() for module in incident.suspected_modules):
                scores[file_path] += 4
            if any(changed.lower() in file_path.lower() for changed in incident.changed_files):
                scores[file_path] += 5
            if any(frame.lower().split(":")[0] in file_path.lower() for frame in compressed_error.key_stack_frames):
                scores[file_path] += 3
            overlap = sum(1 for keyword in keywords if keyword and keyword in summary)
            scores[file_path] += min(overlap, 3)
            for symbol in symbols_by_file.get(file_path, []):
                symbol_name = symbol.name.lower()
                if symbol_name in referenced_symbols:
                    scores[file_path] += 6
                elif symbol_name in log_blob:
                    scores[file_path] += 2
            scores[file_path] += self._graph_signal_score(snapshot, file_path, keywords)

        self._promote_small_test_repo_sources(snapshot, scores)
        self._expand_graph_neighbors(snapshot, scores)
        ranked = [item for item in sorted(scores.items(), key=lambda item: item[1], reverse=True) if item[1] > 0]
        return [path for path, _ in ranked[:limit]]

    def _safe_read(self, path: Path) -> str | None:
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        return None

    def _summarize_text(self, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:220]
        return ""

    def _extract_referenced_paths(self, text: str) -> set[str]:
        paths: set[str] = set()
        for match in self.FILE_REFERENCE.finditer(text):
            value = match.group("quoted") or match.group("plain") or ""
            normalized = value.replace("\\", "/").strip().lower().lstrip("./")
            if normalized:
                paths.add(normalized)
                paths.add(Path(normalized).name)
        return paths

    def _extract_referenced_symbols(self, text: str) -> set[str]:
        symbols = {match.group("symbol").lower() for match in self.SYMBOL_REFERENCE.finditer(text) if match.group("symbol")}
        for match in self.MODULE_OBJECT_REFERENCE.finditer(text):
            symbols.add(match.group("symbol").lower())
        return symbols

    def _extract_referenced_modules(self, text: str) -> set[str]:
        modules: set[str] = set()
        for match in self.MODULE_OBJECT_REFERENCE.finditer(text):
            module = match.group("module").split(".")[0].strip().lower().replace("-", "_")
            if module:
                modules.add(module)
        return modules

    def _matches_referenced_path(self, file_path: str, referenced_paths: set[str]) -> bool:
        normalized = file_path.replace("\\", "/").lower().lstrip("./")
        basename = Path(normalized).name
        return normalized in referenced_paths or basename in referenced_paths or any(normalized.endswith(f"/{item}") for item in referenced_paths)

    def _promote_small_test_repo_sources(self, snapshot: RepoSnapshot, scores: dict[str, int]) -> None:
        py_files = [path for path in snapshot.files if path.endswith(".py")]
        if not py_files or len(py_files) > 20:
            return
        has_test_signal = any(score > 0 and self._is_test_file(path) for path, score in scores.items())
        if not has_test_signal:
            return
        for path in py_files:
            if not self._is_test_file(path):
                scores[path] = max(scores.get(path, 0), 2)

    def _is_test_file(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/").lower()
        name = Path(normalized).name
        return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in normalized

    def _expand_graph_neighbors(self, snapshot: RepoSnapshot, scores: dict[str, int]) -> None:
        seed_files = [path for path, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if score > 0][:4]
        if not seed_files:
            return
        seed_set = set(seed_files)
        symbol_files_by_name: dict[str, set[str]] = {}
        for symbol in snapshot.symbols:
            symbol_files_by_name.setdefault(symbol.name.lower(), set()).add(symbol.file_path)

        for source, target in snapshot.call_edges:
            source_file = source.split(":")[0] if ":" in source else source
            target_files = symbol_files_by_name.get(str(target).lower(), set())
            if source_file in seed_set:
                for target_file in target_files:
                    if target_file not in seed_set:
                        scores[target_file] = scores.get(target_file, 0) + 2
            if target_files & seed_set and source_file not in seed_set:
                scores[source_file] = scores.get(source_file, 0) + 1

        seed_stems = {Path(path).stem.lower().replace("-", "_") for path in seed_set}
        for source, target in snapshot.dependency_edges:
            target_name = str(target).split(".")[-1].lower().replace("-", "_")
            if source in seed_set:
                for file_path in snapshot.files:
                    stem = Path(file_path).stem.lower().replace("-", "_")
                    if stem == target_name:
                        scores[file_path] = scores.get(file_path, 0) + 1
            if target_name in seed_stems and source not in seed_set:
                scores[source] = scores.get(source, 0) + 1
        for edge in snapshot.graph_edges:
            source_file = edge.source.split(":")[0].split("#")[0]
            target_file = edge.target.split(":")[0].split("#")[0]
            if edge.edge_type == "data_flow":
                if source_file in seed_set and target_file not in seed_set:
                    scores[target_file] = scores.get(target_file, 0) + 2
                if target_file in seed_set and source_file not in seed_set:
                    scores[source_file] = scores.get(source_file, 0) + 2
            elif edge.edge_type == "controls" and source_file in seed_set:
                scores[source_file] = scores.get(source_file, 0) + 1

    def _graph_signal_score(self, snapshot: RepoSnapshot, file_path: str, keywords: set[str]) -> int:
        score = 0
        for edge in snapshot.graph_edges:
            source_file = edge.source.split(":")[0].split("#")[0]
            target_file = edge.target.split(":")[0].split("#")[0]
            if file_path not in {source_file, target_file}:
                continue
            if edge.edge_type == "data_flow":
                score += 2
            elif edge.edge_type == "controls":
                score += 1
            elif edge.edge_type in {"imports", "calls"}:
                score += 1
            edge_terms = {term.lower() for term in str(edge.target).replace("#", ":").split(":") if term}
            if keywords & edge_terms:
                score += 1
        return min(score, 6)
