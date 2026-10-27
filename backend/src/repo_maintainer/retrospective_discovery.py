"""Small, read-only Python symbol index for bounded incident exploration."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field


class CodeDiscoveryPlan(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=6)
    questions: list[str] = Field(default_factory=list, max_length=4)


@dataclass(frozen=True)
class CodeSymbol:
    path: str
    name: str
    qualified_name: str
    first: int
    last: int
    calls: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualified_name}"


class PythonSymbolIndex:
    """Resolve local/imported symbols first; ambiguous references stay unresolved."""

    def __init__(self, root: Path, files: set[str]) -> None:
        self.symbols: dict[str, CodeSymbol] = {}
        self.bindings: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        self.bases: dict[tuple[str, str], list[str]] = {}
        modules = {path.removesuffix('.py').replace('/', '.'): path for path in files if path.endswith('.py')}
        modules.update({name.removesuffix('.__init__'): path for name, path in list(modules.items()) if name.endswith('.__init__')})
        for path in sorted(files):
            if not path.endswith('.py'):
                continue
            try:
                tree = ast.parse((root / path).read_text(encoding='utf-8'))
            except (OSError, UnicodeError, SyntaxError):
                continue
            package = path.split('/')[:-1]
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    prefix = package[:len(package) - node.level + 1] if node.level else []
                    module = '.'.join([*prefix, *node.module.split('.')]) if node.module else '.'.join(prefix)
                    for alias in node.names:
                        target = modules.get(module)
                        if target:
                            self.bindings[path][alias.asname or alias.name] = (target, alias.name)
                        elif (target := modules.get(f'{module}.{alias.name}')):
                            self.bindings[path][alias.asname or alias.name] = (target, '')
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname and alias.name in modules:
                            self.bindings[path][alias.asname] = (modules[alias.name], '')
                        elif not alias.asname and alias.name in modules:
                            self.bindings[path][alias.name] = (modules[alias.name], '')
            self._definitions(path, tree, '')
        self.outgoing: dict[str, set[str]] = defaultdict(set)
        self.incoming: dict[str, set[str]] = defaultdict(set)
        for symbol in self.symbols.values():
            for call in symbol.calls:
                target = self.resolve(symbol, call)
                if target and target.key != symbol.key:
                    self.outgoing[symbol.key].add(target.key)
                    self.incoming[target.key].add(symbol.key)

    def _definitions(self, path: str, node: ast.AST, parent: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f'{parent}.{child.name}' if parent else child.name
                if isinstance(child, ast.ClassDef):
                    self.bases[path, qualified] = [ast.unparse(base) for base in child.bases]
                if not isinstance(child, ast.ClassDef):
                    # Do not attribute nested functions' calls to their enclosing function.
                    calls: list[str] = []
                    pending = list(child.body)
                    while pending:
                        item = pending.pop()
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            continue
                        if isinstance(item, ast.Call):
                            calls.append(ast.unparse(item.func))
                            # create_task(worker()) and add_done_callback(handler) both matter.
                            calls.extend(ast.unparse(arg) for arg in item.args if isinstance(arg, (ast.Name, ast.Attribute)))
                        pending.extend(ast.iter_child_nodes(item))
                    symbol = CodeSymbol(path, child.name, qualified, child.lineno, child.end_lineno or child.lineno, tuple(calls))
                    self.symbols[symbol.key] = symbol
                self._definitions(path, child, qualified)
            else:
                self._definitions(path, child, parent)

    def resolve(self, source: CodeSymbol, reference: str) -> CodeSymbol | None:
        parts = reference.split('.')
        name = parts[-1]
        if not all(part.isidentifier() for part in parts):
            return None
        if len(parts) == 1 or (len(parts) == 2 and parts[0] in {'self', 'cls'}):
            owner = source.qualified_name.rpartition('.')[0]
            local = self.symbols.get(f'{source.path}:{owner + "." if owner else ""}{name}')
            if local:
                return local
            if parts[0] in {'self', 'cls'} and len(parts) == 2:
                for base in self.bases.get((source.path, owner), []):
                    binding = self.bindings[source.path].get(base)
                    base_path, base_name = binding if binding else (source.path, base)
                    inherited = self.symbols.get(f'{base_path}:{base_name}.{name}')
                    if inherited:
                        return inherited
            if len(parts) == 1 and (local := self.symbols.get(f'{source.path}:{name}')):
                return local
        for alias, (path, imported) in self.bindings[source.path].items():
            if reference == alias or reference.startswith(alias + '.'):
                qualified = '.'.join(filter(None, [imported, reference[len(alias):].lstrip('.')]))
                target = self.symbols.get(f'{path}:{qualified}')
                if target:
                    return target
        # No global same-name fallback: an unrelated class's method is not a call target.
        return None

    def at(self, path: str, lines: set[int]) -> list[CodeSymbol]:
        found: dict[str, CodeSymbol] = {}
        for line in sorted(lines):
            enclosing = [s for s in self.symbols.values() if s.path == path and s.first <= line <= s.last]
            if enclosing:
                symbol = min(enclosing, key=lambda s: s.last - s.first)
                found[symbol.key] = symbol
        return list(found.values())

    def expand(self, seeds: list[CodeSymbol], *, depth: int = 2, limit: int = 24) -> list[CodeSymbol]:
        seeds = seeds[:limit]
        seen = {s.key for s in seeds}
        frontier = sorted(seen)
        result = list(seeds)
        for _ in range(depth):
            following: list[str] = []
            for key in frontier:
                for neighbor in sorted(self.outgoing[key] | self.incoming[key]):
                    if neighbor not in seen and len(result) < limit:
                        seen.add(neighbor)
                        following.append(neighbor)
                        result.append(self.symbols[neighbor])
            frontier = following
        return result

    def lookup(self, reference: str) -> list[CodeSymbol]:
        # Planner suggestions must identify a unique indexed symbol, never a new path.
        matches = [s for s in self.symbols.values() if reference in {s.key, s.qualified_name, s.name}]
        return matches if len(matches) == 1 else []


def service_map_context(root: Path, files: set[str], seed_paths: set[str]) -> tuple[list[str], dict]:
    """Explicit in-repository service hints only; never fetch another repository."""
    path = '.insight/context.json'
    if path not in files:
        return [], {'status': 'not_configured'}
    try:
        text = (root / path).read_text(encoding='utf-8')
        if len(text) > 40_000:
            return [], {'status': 'too_large'}
        services = json.loads(text)['services']
        if not isinstance(services, list) or len(services) > 20:
            raise ValueError('services must be a list of at most 20 entries')
        def paths(service, key):
            values = service.get(key, [])
            if not isinstance(values, list):
                return []
            return [v for v in values[:20] if isinstance(v, str) and v and not PurePosixPath(v).is_absolute()
                    and '..' not in PurePosixPath(v).parts and '\\' not in v and ':' not in v]
        catalog = {s['id']: s for s in services if isinstance(s, dict) and isinstance(s.get('id'), str)}
        matched = [key for key, s in catalog.items() if any(p == prefix.rstrip('/') or p.startswith(prefix.rstrip('/') + '/')
                   for p in seed_paths for prefix in paths(s, 'paths'))]
        dependencies = sorted({key for service in matched
                               for key in (catalog[service].get('dependencies', [])
                                           if isinstance(catalog[service].get('dependencies', []), list) else [])
                               if isinstance(key, str) and key in catalog})
        docs = sorted({p for key in [*matched, *dependencies] for p in paths(catalog[key], 'docs') if p in files})[:4]
        return ([path, *docs] if matched else []), {
            'status': 'mapped' if matched else 'no_match', 'services': matched,
            'dependency_candidates': dependencies, 'impact': 'unverified',
        }
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return [], {'status': 'invalid'}
