"""Evidence-producing repository context for a retrospective.

Graph edges are retrieval hints. Every emitted evidence item contains source text
from a pinned commit so the review generator can cite code rather than a guessed
relationship.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import stat
import tempfile
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, ContextManager, Iterator, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from .error_semantics import ErrorSemanticCompressor
from .models import IncidentReport
from .repo_indexer import RepositoryIndexer
from .retrospective_models import Evidence, EvidenceSource, IncidentRecord
from .retrospective_discovery import CodeDiscoveryPlan, PythonSymbolIndex, service_map_context


class RepositoryContextError(RuntimeError):
    """Repository context could not be read; other evidence remains usable."""


class RepositorySource(Protocol):
    def open(self, incident: IncidentRecord) -> ContextManager[Path]: ...


class GitHubArchiveSource:
    """Read one immutable GitHub revision without executing repository code."""

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        *,
        timeout: float = 30.0,
        max_archive_bytes: int = 30_000_000,
        max_extracted_bytes: int = 120_000_000,
        max_files: int = 8_000,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_archive_bytes = max(1_000_000, max_archive_bytes)
        self.max_extracted_bytes = max(1_000_000, max_extracted_bytes)
        self.max_files = max(1, max_files)

    @contextmanager
    def open(self, incident: IncidentRecord) -> Iterator[Path]:
        repo = incident.repo_name
        revision = str(incident.metadata.get("head_sha") or incident.commit_sha)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[a-fA-F0-9]{7,40}", revision):
            raise RepositoryContextError("A GitHub repository and immutable commit SHA are required")
        request = Request(
            f"{self.api_url}/repos/{quote(repo, safe='/')}/zipball/{revision}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                archive = response.read(self.max_archive_bytes + 1)
            if len(archive) > self.max_archive_bytes:
                raise RepositoryContextError("Repository archive exceeds the configured size limit")
            with tempfile.TemporaryDirectory(prefix="retrospective-code-") as directory:
                root = Path(directory)
                self._extract(archive, root)
                yield root
        except RepositoryContextError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise RepositoryContextError(f"Repository archive unavailable: {type(exc).__name__}") from exc

    def _extract(self, archive: bytes, root: Path) -> None:
        count = 0
        total_bytes = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for item in zipped.infolist():
                parts = PurePosixPath(item.filename).parts
                if item.is_dir() or len(parts) < 2 or item.filename.startswith("/"):
                    continue
                if any(part in RepositoryIndexer.EXCLUDED_DIRS or part in {".", ".."} or ":" in part or "\\" in part for part in parts[1:]):
                    continue
                if stat.S_ISLNK(item.external_attr >> 16):
                    continue
                relative = Path(*parts[1:])
                if relative.suffix.lower() not in RepositoryIndexer.SUPPORTED_SUFFIXES:
                    continue
                if item.file_size > 1_000_000:
                    continue
                count += 1
                total_bytes += item.file_size
                if count > self.max_files or total_bytes > self.max_extracted_bytes:
                    raise RepositoryContextError("Repository archive exceeds the analysis limit")
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(item))


class RepositoryCodeContext:
    """Expand diff and failure clues to nearby source and test evidence."""

    def __init__(
        self,
        source: RepositorySource,
        *,
        indexer: RepositoryIndexer | None = None,
        max_files: int = 10,
        max_expanded_files: int = 40,
        max_regions_per_file: int = 2,
        max_snippet_chars: int = 2_400,
        planner: Callable[[IncidentRecord, list[Evidence]], CodeDiscoveryPlan] | None = None,
    ) -> None:
        self.source = source
        self.indexer = indexer or RepositoryIndexer()
        self.max_files = max(1, max_files)
        self.max_expanded_files = max(self.max_files, max_expanded_files)
        self.max_regions_per_file = max(1, max_regions_per_file)
        self.max_snippet_chars = max(300, max_snippet_chars)
        self.planner = planner

    def collect(self, incident: IncidentRecord, evidence: list[Evidence]) -> list[Evidence]:
        with self.source.open(incident) as root:
            snapshot = self.indexer.index(root)
            files = set(snapshot.files)
            changed_lines: dict[str, set[int]] = defaultdict(set)
            for item in evidence:
                if item.source_type != EvidenceSource.GITHUB_DIFF:
                    continue
                path = item.location.replace("\\", "/").split(" @@", 1)[0]
                if path in files:
                    changed_lines[path].update(_changed_new_lines(item.content))

            report = IncidentReport(
                id=incident.id,
                title=incident.title,
                description=incident.description,
                logs=incident.logs + "\n" + "\n".join(item.content[:3_000] for item in evidence if item.source_type == EvidenceSource.CI_LOG),
                changed_files=list(changed_lines),
                commit_sha=incident.commit_sha,
            )
            compressed = ErrorSemanticCompressor().compress(report)
            ranked_files = self.indexer.relevant_files(snapshot, report, compressed, limit=16)
            scores: dict[str, int] = defaultdict(int)
            reasons: dict[str, set[str]] = defaultdict(set)
            focus_names: dict[str, set[str]] = defaultdict(set)
            stack_lines: dict[str, set[int]] = defaultdict(set)

            def add(path: str, score: int, reason: str, name: str = "") -> None:
                if path not in files:
                    return
                scores[path] += score
                reasons[path].add(reason)
                if name:
                    focus_names[path].add(name)

            for path in changed_lines:
                add(path, 100, "changed_file")
            for index, path in enumerate(ranked_files):
                add(path, max(5, 20 - index), "failure_or_repository_match")
            for reference, line in _stack_locations(report.logs):
                matches = [
                    path for path in files
                    if path == reference or path.endswith("/" + reference)
                ]
                if not matches:
                    basename = Path(reference).name
                    matches = [path for path in files if Path(path).name == basename]
                if len(matches) == 1:
                    add(matches[0], 75, "error_stack_frame")
                    stack_lines[matches[0]].add(line)

            changed_names: set[str] = set()
            for path, lines in changed_lines.items():
                source_text = _read_source(root / path)
                for start, end, name in _python_regions(source_text):
                    if any(start <= line <= end for line in lines):
                        changed_names.add(name)
                        focus_names[path].add(name)
            if not changed_names:
                changed_names = {symbol.name for symbol in snapshot.symbols if symbol.file_path in changed_lines}

            symbols_by_name: dict[str, set[str]] = defaultdict(set)
            for symbol in snapshot.symbols:
                symbols_by_name[symbol.name].add(symbol.file_path)
            for source_symbol, target_name in snapshot.call_edges:
                source_path = source_symbol.split(":", 1)[0]
                if target_name in changed_names and source_path not in changed_lines:
                    add(source_path, 65, "calls_changed_symbol", target_name)
                if source_path in changed_lines:
                    for target_path in symbols_by_name.get(target_name, set()) - set(changed_lines):
                        add(target_path, 40, "called_by_changed_file", target_name)

            changed_stems = {Path(path).stem for path in changed_lines}
            for source_path, imported in snapshot.dependency_edges:
                imported_stem = str(imported).split(".")[-1]
                if source_path not in changed_lines and imported_stem in changed_stems:
                    add(source_path, 32, "imports_changed_module")
                if source_path in changed_lines:
                    for path in files:
                        if Path(path).stem == imported_stem and path not in changed_lines:
                            add(path, 25, "imported_by_changed_file")

            # One extra file-graph hop can reveal an entry point or downstream
            # dependency that does not mention the changed symbol directly.
            # Only resolve edges with a unique target file to limit name clashes.
            graph: dict[str, set[str]] = defaultdict(set)
            graph_focus: dict[str, set[str]] = defaultdict(set)
            stems: dict[str, set[str]] = defaultdict(set)
            for path in files:
                stems[Path(path).stem].add(path)

            def connect(source_path: str, targets: set[str], source_name: str = "", target_name: str = "") -> None:
                if source_path not in files or len(targets) != 1:
                    return
                target_path = next(iter(targets))
                if target_path == source_path:
                    return
                graph[source_path].add(target_path)
                graph[target_path].add(source_path)
                if source_name:
                    graph_focus[source_path].add(source_name)
                if target_name:
                    graph_focus[source_path].add(target_name)
                    graph_focus[target_path].add(target_name)

            for source_symbol, target_name in snapshot.call_edges:
                source_path, _, source_name = source_symbol.partition(":")
                connect(source_path, symbols_by_name.get(target_name, set()), source_name, target_name)
            for source_path, imported in snapshot.dependency_edges:
                imported_stem = str(imported).split(".")[-1]
                connect(source_path, stems.get(imported_stem, set()), target_name=imported_stem)
            first_hop = {neighbor for path in changed_lines for neighbor in graph[path]}
            second_hop = {neighbor for path in first_hop for neighbor in graph[path]}
            for path in second_hop - first_hop - set(changed_lines):
                add(path, 18, "graph_two_hop")
                focus_names[path].update(graph_focus[path])

            for path in files:
                if path in changed_lines or not _is_test(path) or not changed_names:
                    continue
                text = _read_source(root / path)
                matched = next((name for name in changed_names if re.search(rf"\b{re.escape(name)}\b", text)), "")
                if matched:
                    add(path, 36, "test_mentions_changed_symbol", matched)

            for path in scores:
                if path not in changed_lines and not focus_names[path]:
                    focus_names[path].update(changed_names)
                    focus_names[path].update(changed_stems)
                    if not focus_names[path]:
                        focus_names[path].update(compressed.keywords[:5])

            symbols = PythonSymbolIndex(root, files)
            seeds = [symbol for path in sorted(set(changed_lines) | set(stack_lines))
                     for symbol in symbols.at(path, changed_lines[path] | stack_lines[path])]
            chain = symbols.expand(seeds)
            for symbol in chain:
                add(symbol.path, 80, "symbol_behavior_chain", symbol.name)
            document_paths, service_map = service_map_context(root, files, set(changed_lines) | set(stack_lines))
            for path in document_paths:
                add(path, 55, "service_map_document")
                stack_lines[path].add(1)

            report_summary = {
                "strategy": "symbol-first-v1", "static_hops": 2,
                "planner": "not_needed" if self.planner else "disabled", "service_map": service_map,
                "questions": [], "unresolved_calls": sorted({call for seed in seeds for call in seed.calls
                    if "." in call and not call.startswith('self.logger.') and symbols.resolve(seed, call) is None})[:12],
                'followup_symbols': [],
            }

            revision = str(incident.metadata.get("head_sha") or incident.commit_sha)
            output: list[Evidence] = []
            ordered = sorted(scores, key=lambda item: (-scores[item], item))
            changed_candidates = [path for path in ordered if path in changed_lines]
            related_candidates = [path for path in ordered if path not in changed_lines]
            file_limit = min(self.max_expanded_files, max(self.max_files, 2 * len(changed_candidates)))
            related_reserve = 1 if related_candidates else 0
            changed_quota = min(len(changed_candidates), file_limit - related_reserve)
            chosen = changed_candidates[:changed_quota] + related_candidates[: file_limit - changed_quota]
            if len(chosen) < file_limit:
                chosen.extend(changed_candidates[changed_quota : changed_quota + file_limit - len(chosen)])
            if self.planner is not None and report_summary['unresolved_calls']:
                chosen = chosen[:max(len(changed_candidates), file_limit - 2, 1)]
            for path in chosen:
                text = _read_source(root / path)
                if not text.strip():
                    continue
                anchors = changed_lines.get(path, set()) | stack_lines.get(path, set())
                regions: list[tuple[int, int, str, int]] = []
                symbol_anchors = [symbol.first for symbol in chain if symbol.path == path]
                for anchor in [*sorted(anchors), *symbol_anchors]:
                    if any(first <= anchor <= last for first, last, _, _ in regions):
                        continue
                    region = _select_region(text, {anchor}, focus_names[path], self.max_snippet_chars)
                    if region is not None and not any(region[:2] == existing[:2] for existing in regions):
                        regions.append((*region, anchor))
                    if len(regions) >= self.max_regions_per_file:
                        break
                if not regions:
                    region = _select_region(text, set(), focus_names[path], self.max_snippet_chars)
                    if region is not None:
                        regions.append((*region, region[0]))
                for first, last, excerpt, anchor in regions:
                    digest = hashlib.sha1(f"{incident.id}:{revision}:{path}:{first}:{last}".encode()).hexdigest()[:20]
                    relation = sorted(reasons[path])
                    output.append(
                        Evidence(
                            id=f"ev_code_{digest}",
                            incident_id=incident.id,
                            source_type=EvidenceSource.REPOSITORY_CODE,
                            content=excerpt,
                            location=f"{path}#L{first}-L{last}",
                            source_url=(f"https://github.com/{incident.repo_name}/blob/{revision}/{quote(path)}#L{first}-L{last}" if incident.repo_name and revision else ""),
                            repo_id=incident.repo_id,
                            commit_sha=revision,
                            metadata={"file_path": path, "line_start": first, "line_end": last, "anchor_line": anchor, "relation": relation, "retrieval_score": scores[path], "analysis": "static_candidate",
                                      "region_complete": any(a == first and b == last for a, b, _ in _python_regions(text))},
                            selection_reason=", ".join(relation),
                        )
                    )

            # One small planning call only when static discovery leaves an unresolved
            # method reference. The planner can name symbols, not execute commands.
            if self.planner is not None and seeds and report_summary['unresolved_calls']:
                try:
                    plan = self.planner(incident, output)
                    if not isinstance(plan, CodeDiscoveryPlan):
                        raise ValueError('Invalid discovery plan')
                    report_summary['planner'] = 'completed'
                    report_summary['questions'] = [q[:200] for q in plan.questions]
                    by_path: dict[str, int] = defaultdict(int)
                    for item in output:
                        by_path[item.metadata['file_path']] += 1
                    for reference in plan.symbols:
                        for symbol in symbols.lookup(reference):
                            if len(by_path) >= file_limit and symbol.path not in by_path:
                                continue
                            if by_path[symbol.path] >= self.max_regions_per_file:
                                continue
                            if any(item.metadata['file_path'] == symbol.path
                                   and item.metadata['line_start'] <= symbol.first <= item.metadata['line_end'] for item in output):
                                continue
                            region = _select_region(_read_source(root / symbol.path), {symbol.first}, {symbol.name}, self.max_snippet_chars)
                            if region is None:
                                continue
                            first, last, content = region
                            digest = hashlib.sha1(f'{incident.id}:{revision}:{symbol.path}:{first}:{last}'.encode()).hexdigest()[:20]
                            output.append(Evidence(
                                id=f'ev_code_{digest}', incident_id=incident.id, source_type=EvidenceSource.REPOSITORY_CODE,
                                content=content, location=f'{symbol.path}#L{first}-L{last}', repo_id=incident.repo_id,
                                commit_sha=revision,
                                source_url=f'https://github.com/{incident.repo_name}/blob/{revision}/{quote(symbol.path)}#L{first}-L{last}',
                                metadata={'file_path': symbol.path, 'line_start': first, 'line_end': last, 'anchor_line': symbol.first,
                                          'relation': ['bounded_symbol_followup'], 'retrieval_score': 85, 'analysis': 'static_candidate',
                                          'region_complete': first == symbol.first and last == symbol.last},
                                selection_reason='bounded_symbol_followup',
                            ))
                            by_path[symbol.path] += 1
                            report_summary['followup_symbols'].append(symbol.key)
                except Exception as exc:
                    # Optional planning must never discard deterministic evidence.
                    report_summary['planner'] = f'unavailable:{type(exc).__name__}'
            report_summary['files'] = len({item.metadata['file_path'] for item in output})
            report_summary['regions'] = len(output)
            report_summary['incomplete_regions'] = sum(not item.metadata['region_complete'] for item in output)
            for item in output:
                item.metadata['context_discovery'] = report_summary
            return output


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        return ""


def _changed_new_lines(patch: str) -> set[int]:
    lines: set[int] = set()
    current: int | None = None
    for line in patch.splitlines():
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            current = int(match.group(1))
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            lines.add(current)
            current += 1
        elif current is not None and line.startswith(" "):
            current += 1
    return lines


def _stack_locations(logs: str) -> list[tuple[str, int]]:
    matches = [
        (path, int(line))
        for path, line in re.findall(r'File "([^"\n]+)", line (\d+)', logs)
    ]
    matches.extend(
        (path, int(line))
        for path, line in re.findall(r"([A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx)):(\d+)(?::\d+)?", logs)
    )
    return [(path.replace("\\", "/").lstrip("./"), line) for path, line in matches]


def _python_regions(text: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _select_region(text: str, changed: set[int], focus_names: set[str], max_chars: int) -> tuple[int, int, str] | None:
    lines = text.splitlines()
    if not lines:
        return None
    regions = _python_regions(text)
    anchors = sorted(line for line in changed if 1 <= line <= len(lines))
    if not anchors and focus_names:
        matching = [region for region in regions if region[2] in focus_names]
        if matching:
            anchors = [min(matching, key=lambda item: item[1] - item[0])[0]]
    if not anchors and focus_names:
        anchors = [
            index for index, line in enumerate(lines, 1)
            if any(re.search(rf"\b{re.escape(name)}\s*\(", line) for name in focus_names)
            and not line.lstrip().startswith(("from ", "import "))
        ][:1]
    if not anchors and focus_names:
        anchors = [index for index, line in enumerate(lines, 1) if any(re.search(rf"\b{re.escape(name)}\b", line) for name in focus_names)][:1]
    if not anchors:
        return None
    anchor = anchors[0]
    enclosing = [region for region in regions if region[0] <= anchor <= region[1]]
    if enclosing:
        first, last, _ = min(enclosing, key=lambda item: item[1] - item[0])
    else:
        first, last = max(1, anchor - 8), min(len(lines), anchor + 12)
    if last - first > 70:
        first, last = max(first, anchor - 20), min(last, anchor + 35)
    while first < anchor and sum(len(line) + 1 for line in lines[first - 1 : anchor]) > max_chars:
        first += 1
    excerpt_lines: list[str] = []
    used = 0
    for line in lines[first - 1 : last]:
        if used + len(line) + 1 > max_chars:
            break
        excerpt_lines.append(line)
        used += len(line) + 1
    if not excerpt_lines:
        return None
    return first, first + len(excerpt_lines) - 1, "\n".join(excerpt_lines)


def _is_test(path: str) -> bool:
    name = Path(path).name.lower()
    return name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js")) or "/tests/" in f"/{path.lower()}"
