from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass, field

try:
    import networkx as nx
except Exception:  # noqa: BLE001
    nx = None

from .config import GraphConfig
from .models import CodeEdge, CodeGraphMetadata, SymbolDefinition


@dataclass(slots=True)
class AnalyzerResult:
    dependency_edges: list[tuple[str, str]] = field(default_factory=list)
    call_edges: list[tuple[str, str]] = field(default_factory=list)
    symbols: list[SymbolDefinition] = field(default_factory=list)
    graph_edges: list[CodeEdge] = field(default_factory=list)
    tree_sitter_language: str | None = None


class PythonAstAnalyzer:
    def __init__(self, config: GraphConfig) -> None:
        self.config = config

    def analyze(self, relative_path: str, text: str) -> AnalyzerResult:
        result = AnalyzerResult()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return result

        parent_map = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.dependency_edges.append((relative_path, alias.name))
                    result.graph_edges.append(CodeEdge(relative_path, alias.name, "imports"))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                result.dependency_edges.append((relative_path, module))
                result.graph_edges.append(CodeEdge(relative_path, module, "imports"))
            elif isinstance(node, ast.ClassDef):
                symbol_id = f"{relative_path}:{node.name}"
                result.symbols.append(
                    SymbolDefinition(
                        symbol_id=symbol_id,
                        name=node.name,
                        kind="class",
                        file_path=relative_path,
                        line=getattr(node, "lineno", 0),
                        language="python",
                        signature=f"class {node.name}",
                    )
                )
                result.graph_edges.append(CodeEdge(relative_path, symbol_id, "defines"))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_id = f"{relative_path}:{node.name}"
                signature = f"def {node.name}({', '.join(arg.arg for arg in node.args.args)})"
                result.symbols.append(
                    SymbolDefinition(
                        symbol_id=symbol_id,
                        name=node.name,
                        kind="function",
                        file_path=relative_path,
                        line=getattr(node, "lineno", 0),
                        language="python",
                        signature=signature,
                    )
                )
                result.graph_edges.append(CodeEdge(relative_path, symbol_id, "defines"))
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        target = self._render_call_target(child.func)
                        if target:
                            result.call_edges.append((symbol_id, target))
                            result.graph_edges.append(CodeEdge(symbol_id, target, "calls"))
            elif self.config.data_flow_enabled and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                scope = self._scope_name(parent_map, node, relative_path)
                targets = self._assignment_targets(node)
                value_names = self._value_names(node)
                for target_name in targets:
                    result.graph_edges.append(CodeEdge(scope, f"{relative_path}:{target_name}", "writes"))
                    for source_name in value_names:
                        result.graph_edges.append(CodeEdge(f"{relative_path}:{source_name}", f"{relative_path}:{target_name}", "data_flow"))
            elif self.config.control_flow_enabled and isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                scope = self._scope_name(parent_map, node, relative_path)
                marker = f"{relative_path}#control:{getattr(node, 'lineno', 0)}"
                result.graph_edges.append(CodeEdge(scope, marker, "controls"))
        return result

    def _render_call_target(self, func: ast.AST) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    def _scope_name(self, parent_map: dict[ast.AST, ast.AST], node: ast.AST, relative_path: str) -> str:
        current = parent_map.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return f"{relative_path}:{current.name}"
            current = parent_map.get(current)
        return relative_path

    def _assignment_targets(self, node: ast.AST) -> list[str]:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        names: list[str] = []
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                names.extend(item.id for item in target.elts if isinstance(item, ast.Name))
        return names

    def _value_names(self, node: ast.AST) -> list[str]:
        value = getattr(node, "value", None)
        if value is None:
            return []
        return [child.id for child in ast.walk(value) if isinstance(child, ast.Name)]


class RegexLanguageAnalyzer:
    FUNCTION_PATTERNS = [
        re.compile(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\("),
        re.compile(r"export\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ]
    IMPORT_PATTERNS = [
        re.compile(r'import\s+["\']([^"\']+)["\']'),
        re.compile(r'from\s+["\']([^"\']+)["\']'),
        re.compile(r'require\(\s*["\']([^"\']+)["\']\s*\)'),
    ]
    CALL_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(")
    ASSIGN_PATTERN = re.compile(r"(?:const|let|var)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)")
    CONTROL_PATTERN = re.compile(r"\b(if|else|for|while|switch|try|catch)\b")

    def __init__(self, config: GraphConfig) -> None:
        self.config = config

    def analyze(self, relative_path: str, text: str, language: str) -> AnalyzerResult:
        result = AnalyzerResult()
        for pattern in self.IMPORT_PATTERNS:
            for match in pattern.findall(text):
                result.dependency_edges.append((relative_path, match))
                result.graph_edges.append(CodeEdge(relative_path, match, "imports"))
        for pattern in self.FUNCTION_PATTERNS:
            for match in pattern.findall(text):
                symbol_id = f"{relative_path}:{match}"
                result.symbols.append(
                    SymbolDefinition(
                        symbol_id=symbol_id,
                        name=match,
                        kind="function",
                        file_path=relative_path,
                        line=0,
                        language=language,
                        signature=f"{language}::{match}",
                    )
                )
                result.graph_edges.append(CodeEdge(relative_path, symbol_id, "defines"))
        for target in self.CALL_PATTERN.findall(text):
            result.call_edges.append((relative_path, target))
            result.graph_edges.append(CodeEdge(relative_path, target, "calls"))
        if self.config.data_flow_enabled:
            for target_name, source_name in self.ASSIGN_PATTERN.findall(text):
                result.graph_edges.append(CodeEdge(f"{relative_path}:{source_name}", f"{relative_path}:{target_name}", "data_flow"))
        if self.config.control_flow_enabled:
            for index, _ in enumerate(self.CONTROL_PATTERN.findall(text), start=1):
                result.graph_edges.append(CodeEdge(relative_path, f"{relative_path}#control:{index}", "controls"))
        return result


class TreeSitterLanguageAnalyzer:
    def __init__(self, config: GraphConfig) -> None:
        self.config = config
        self._available = False
        self._languages: dict[str, object] = {}
        self._available_languages: set[str] = set()
        self._get_parser = None
        if not config.enable_treesitter:
            return
        try:
            import tree_sitter_languages  # type: ignore

            self._get_parser = tree_sitter_languages.get_parser
            self._available = True
        except Exception:
            self._available = False
        if not self._available and config.treesitter_auto_detect:
            self._load_language_specific_parsers()

    def available(self) -> bool:
        return self._available

    def available_languages(self) -> list[str]:
        return sorted(self._available_languages)

    def analyze(self, relative_path: str, text: str, language: str) -> AnalyzerResult | None:
        parser = self._resolve_parser(language)
        if parser is None:
            return None
        tree = parser.parse(text.encode("utf-8"))
        root = tree.root_node
        result = AnalyzerResult(tree_sitter_language=language)
        self._walk_tree(relative_path, language, root, text.encode("utf-8"), result)
        return result

    def _walk_tree(self, relative_path: str, language: str, node, source: bytes, result: AnalyzerResult) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            node_type = getattr(current, "type", "")
            if node_type in {"function_definition", "function_declaration", "method_definition", "lexical_declaration"}:
                name = self._extract_identifier(current, source)
                if name:
                    symbol_id = f"{relative_path}:{name}"
                    result.symbols.append(
                        SymbolDefinition(
                            symbol_id=symbol_id,
                            name=name,
                            kind="function",
                            file_path=relative_path,
                            line=getattr(current, "start_point", (0, 0))[0] + 1,
                            language=language,
                            signature=f"{language}::{name}",
                        )
                    )
                    result.graph_edges.append(CodeEdge(relative_path, symbol_id, "defines"))
            elif node_type in {"import_statement", "import_from_statement"}:
                text_value = source[current.start_byte : current.end_byte].decode("utf-8", errors="ignore")
                for match in re.findall(r'["\']([^"\']+)["\']', text_value):
                    result.dependency_edges.append((relative_path, match))
                    result.graph_edges.append(CodeEdge(relative_path, match, "imports"))
            elif "call" in node_type:
                name = self._extract_identifier(current, source)
                if name:
                    result.call_edges.append((relative_path, name))
                    result.graph_edges.append(CodeEdge(relative_path, name, "calls"))
            stack.extend(reversed(getattr(current, "children", [])))

    def _extract_identifier(self, node, source: bytes) -> str:
        for child in getattr(node, "children", []):
            child_type = getattr(child, "type", "")
            if child_type in {"identifier", "property_identifier", "type_identifier"}:
                return source[child.start_byte : child.end_byte].decode("utf-8", errors="ignore")
        text = source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)", text)
        return match.group(1) if match else ""

    def _resolve_parser(self, language: str):
        normalized = {"tsx": "typescript", "jsx": "javascript"}.get(language, language)
        if self._get_parser is not None:
            try:
                parser = self._get_parser(normalized)
                self._available_languages.add(normalized)
                self._available = True
                return parser
            except Exception:
                pass
        parser_factory = self._languages.get(normalized)
        if parser_factory is None:
            return None
        try:
            parser = parser_factory()
            self._available_languages.add(normalized)
            self._available = True
            return parser
        except Exception:
            return None

    def _load_language_specific_parsers(self) -> None:
        candidates = {
            "python": "tree_sitter_python",
            "javascript": "tree_sitter_javascript",
            "typescript": "tree_sitter_typescript",
        }
        try:
            from tree_sitter import Language, Parser  # type: ignore
        except Exception:
            return
        for language, module_name in candidates.items():
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            factory = getattr(module, "language", None) or getattr(module, "LANGUAGE", None)
            if factory is None:
                continue

            def build_parser(factory=factory, Parser=Parser, Language=Language):
                parser = Parser()
                grammar = factory() if callable(factory) else factory
                parser.language = grammar if isinstance(grammar, Language) else Language(grammar)
                return parser

            self._languages[language] = build_parser
            self._available = True


class LanguageAnalyzerSuite:
    LANGUAGE_MAP = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }

    def __init__(self, config: GraphConfig) -> None:
        self.config = config
        self.python_analyzer = PythonAstAnalyzer(config)
        self.regex_analyzer = RegexLanguageAnalyzer(config)
        self.tree_sitter_analyzer = TreeSitterLanguageAnalyzer(config)
        self._tree_sitter_hits: set[str] = set()

    def reset_session(self) -> None:
        self._tree_sitter_hits = set()

    def analyze(self, relative_path: str, suffix: str, text: str) -> AnalyzerResult:
        language = self.LANGUAGE_MAP.get(suffix.lower(), suffix.lower().lstrip(".") or "text")
        tree_result = self.tree_sitter_analyzer.analyze(relative_path, text, language)
        if tree_result is not None:
            if tree_result.tree_sitter_language:
                self._tree_sitter_hits.add(tree_result.tree_sitter_language)
            return tree_result
        if suffix.lower() == ".py":
            return self.python_analyzer.analyze(relative_path, text)
        return self.regex_analyzer.analyze(relative_path, text, language)

    def build_graph_metadata(self, files: list[str], graph_edges: list[CodeEdge]) -> CodeGraphMetadata:
        nodes = set(files)
        limited_edges = graph_edges[: self.config.max_symbol_edges]
        for edge in limited_edges:
            nodes.add(edge.source)
            nodes.add(edge.target)

        if nx is not None:
            graph = nx.DiGraph()
            graph.add_nodes_from(nodes)
            for edge in limited_edges:
                graph.add_edge(edge.source, edge.target, edge_type=edge.edge_type, weight=edge.weight)
            component_count = nx.number_strongly_connected_components(graph) if graph.number_of_nodes() > 0 else 0
            node_count = graph.number_of_nodes()
            edge_count = graph.number_of_edges()
        else:
            node_count = len(nodes)
            edge_count = len(limited_edges)
            component_count = self._scc_count(nodes, limited_edges)
        return CodeGraphMetadata(
            graph_nodes=node_count,
            graph_edges=edge_count,
            strongly_connected_components=component_count,
            tree_sitter_enabled=self.tree_sitter_analyzer.available(),
            languages_with_tree_sitter=sorted(self._tree_sitter_hits or set(self.tree_sitter_analyzer.available_languages())),
        )

    def _scc_count(self, nodes: set[str], edges: list[CodeEdge]) -> int:
        adjacency: dict[str, set[str]] = {node: set() for node in nodes}
        reverse_adjacency: dict[str, set[str]] = {node: set() for node in nodes}
        for edge in edges:
            adjacency.setdefault(edge.source, set()).add(edge.target)
            reverse_adjacency.setdefault(edge.target, set()).add(edge.source)
            adjacency.setdefault(edge.target, set())
            reverse_adjacency.setdefault(edge.source, set())

        visited: set[str] = set()
        order: list[str] = []

        def dfs(start: str) -> None:
            stack: list[tuple[str, bool]] = [(start, False)]
            while stack:
                node, expanded = stack.pop()
                if expanded:
                    order.append(node)
                    continue
                if node in visited:
                    continue
                visited.add(node)
                stack.append((node, True))
                for neighbor in adjacency.get(node, set()):
                    if neighbor not in visited:
                        stack.append((neighbor, False))

        for node in list(nodes):
            if node not in visited:
                dfs(node)

        seen: set[str] = set()
        components = 0
        for node in reversed(order):
            if node in seen:
                continue
            components += 1
            stack = [node]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(reverse_adjacency.get(current, set()) - seen)
        return components
