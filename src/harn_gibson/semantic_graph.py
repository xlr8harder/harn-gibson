"""Bounded semantic repo graph for renderer perception."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEMANTIC_REPO_GRAPH_SCHEMA = "harn-gibson.semantic-repo-graph.v1"

_PYTHON_SUFFIX = ".py"
_MAX_IMPORTS_PER_FILE = 32
_MAX_SYMBOLS_PER_FILE = 16
_REPO_EXCLUDED_NAMES = {
    ".coverage",
    ".git",
    ".harn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "test-artifacts",
}
_SENSITIVE_PATH_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "auth.json",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "tokens",
    "token",
}
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")


@dataclass(frozen=True, slots=True)
class SemanticRepoGraphConfig:
    project_root: str | None = None
    max_files: int = 96
    max_edges: int = 192
    max_symbols: int = 160
    max_file_bytes: int = 384_000


@dataclass(frozen=True, slots=True)
class SymbolFact:
    name: str
    kind: str
    line: int


@dataclass(frozen=True, slots=True)
class PythonFileFact:
    path: str
    module: str
    line_count: int
    imports: tuple[str, ...] = ()
    symbols: tuple[SymbolFact, ...] = ()
    syntax_ok: bool = True
    parse_error: str | None = None
    skipped_reason: str | None = None


@dataclass(slots=True)
class _GraphBuildState:
    max_edges: int
    max_symbols: int
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    node_ids: set[str] = field(default_factory=set)
    edge_keys: set[tuple[str, str, str, str]] = field(default_factory=set)
    symbol_count: int = 0
    truncated: bool = False

    def append_node(self, node: Mapping[str, Any]) -> None:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in self.node_ids:
            return
        self.nodes.append(dict(node))
        self.node_ids.add(node_id)

    def append_edge(
        self,
        source: str,
        target: str,
        relationship: str,
        *,
        detail: str = "",
        provenance_source: str = "observed",
        confidence: float = 1.0,
        basis: str = "Parsed from bounded local project metadata.",
    ) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, relationship, detail)
        if key in self.edge_keys:
            return
        if len(self.edges) >= self.max_edges:
            self.truncated = True
            return
        self.edge_keys.add(key)
        edge: dict[str, Any] = {
            "id": f"edge:{len(self.edges) + 1}",
            "source": source,
            "target": target,
            "relationship": relationship,
            "detail": detail,
            "provenance": {
                "source": provenance_source,
                "confidence": confidence,
                "basis": basis,
            },
        }
        self.edges.append(edge)


def semantic_repo_graph_context(config: SemanticRepoGraphConfig | None = None) -> dict[str, Any]:
    """Build a bounded repo semantic graph without exposing file contents."""

    actual_config = config or SemanticRepoGraphConfig()
    root = _project_root(actual_config)
    base_payload: dict[str, Any] = {
        "schema": SEMANTIC_REPO_GRAPH_SCHEMA,
        "rootName": root.name or root.as_posix(),
        "maxFiles": max(0, actual_config.max_files),
        "maxEdges": max(0, actual_config.max_edges),
        "maxSymbols": max(0, actual_config.max_symbols),
    }
    if not root.is_dir():
        return {
            **base_payload,
            "available": False,
            "reason": "project root is not a directory",
            "languages": [],
            "files": [],
            "nodes": [],
            "edges": [],
            "nodeCount": 0,
            "edgeCount": 0,
            "fileCount": 0,
            "symbolCount": 0,
            "truncated": False,
        }

    python_paths, file_truncated = _python_files(root, actual_config)
    facts = tuple(_parse_python_file(root, path, actual_config) for path in python_paths)
    module_map = {fact.module: fact for fact in facts if fact.syntax_ok and not fact.skipped_reason}
    state = _GraphBuildState(
        max_edges=max(0, actual_config.max_edges),
        max_symbols=max(0, actual_config.max_symbols),
        truncated=file_truncated,
    )
    _add_graph_nodes_and_edges(state, root, facts, module_map)
    files = [_file_fact_payload(fact) for fact in facts]
    return {
        **base_payload,
        "available": True,
        "languages": ["python"] if facts else [],
        "files": files,
        "nodes": state.nodes,
        "edges": state.edges,
        "nodeCount": len(state.nodes),
        "edgeCount": len(state.edges),
        "fileCount": len(files),
        "symbolCount": state.symbol_count,
        "importEdgeCount": sum(1 for edge in state.edges if edge.get("relationship") == "imports"),
        "testEdgeCount": sum(1 for edge in state.edges if edge.get("relationship") == "tests"),
        "truncated": state.truncated,
        "provenance": {
            "source": "observed",
            "confidence": 1.0,
            "basis": (
                "Built from bounded local AST metadata. File contents are not included; "
                "relationship-level provenance marks inferred test edges."
            ),
        },
    }


def _project_root(config: SemanticRepoGraphConfig) -> Path:
    if config.project_root:
        return Path(config.project_root).expanduser().resolve()
    return Path.cwd().resolve()


def _python_files(root: Path, config: SemanticRepoGraphConfig) -> tuple[tuple[Path, ...], bool]:
    max_files = max(0, config.max_files)
    found: list[Path] = []
    dirs = [root]
    truncated = False
    while dirs and len(found) < max_files:
        current = dirs.pop(0)
        for child in sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if _skip_path_part(child.name) or child.is_symlink():
                continue
            if child.is_dir():
                dirs.append(child)
                continue
            if child.suffix == _PYTHON_SUFFIX:
                found.append(child)
                if len(found) >= max_files:
                    truncated = _has_more_python_files(dirs, current, child)
                    break
    if dirs and len(found) >= max_files:
        truncated = True
    return tuple(found), truncated


def _has_more_python_files(pending_dirs: Iterable[Path], current_dir: Path, last_child: Path) -> bool:
    for directory in (current_dir, *tuple(pending_dirs)):
        for child in directory.iterdir():
            if child == last_child or _skip_path_part(child.name) or child.is_symlink():
                continue
            if child.is_dir() or child.suffix == _PYTHON_SUFFIX:
                return True
    return False


def _parse_python_file(root: Path, path: Path, config: SemanticRepoGraphConfig) -> PythonFileFact:
    relative_path = path.relative_to(root).as_posix()
    module = _module_name(Path(relative_path))
    if path.stat().st_size > max(0, config.max_file_bytes):
        return PythonFileFact(
            path=relative_path,
            module=module,
            line_count=0,
            syntax_ok=False,
            skipped_reason="file exceeds semantic graph byte limit",
        )
    source = path.read_text(encoding="utf-8", errors="replace")
    line_count = source.count("\n") + (0 if not source or source.endswith("\n") else 1)
    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError as error:
        return PythonFileFact(
            path=relative_path,
            module=module,
            line_count=line_count,
            syntax_ok=False,
            parse_error=_syntax_error_summary(error),
        )
    imports = _imports_from_ast(tree, module, path.name == "__init__.py")
    symbols = _symbols_from_ast(tree)
    return PythonFileFact(
        path=relative_path,
        module=module,
        line_count=line_count,
        imports=imports,
        symbols=symbols,
    )


def _module_name(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else relative_path.stem


def _imports_from_ast(tree: ast.AST, module: str, is_package: bool) -> tuple[str, ...]:
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _append_unique(imports, alias.name)
        elif isinstance(node, ast.ImportFrom):
            for imported in _import_from_targets(node, module, is_package):
                _append_unique(imports, imported)
        if len(imports) >= _MAX_IMPORTS_PER_FILE:
            break
    return tuple(imports[:_MAX_IMPORTS_PER_FILE])


def _import_from_targets(node: ast.ImportFrom, module: str, is_package: bool) -> tuple[str, ...]:
    base = _resolve_import_base(node.module or "", node.level, module, is_package)
    targets: list[str] = []
    if base:
        targets.append(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        _append_unique(targets, f"{base}.{alias.name}" if base else alias.name)
    return tuple(targets)


def _resolve_import_base(import_module: str, level: int, module: str, is_package: bool) -> str:
    if level <= 0:
        return import_module
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    base_count = max(0, len(package_parts) - level + 1)
    parts = package_parts[:base_count]
    if import_module:
        parts.extend(import_module.split("."))
    return ".".join(part for part in parts if part)


def _symbols_from_ast(tree: ast.AST) -> tuple[SymbolFact, ...]:
    symbols: list[SymbolFact] = []
    body = getattr(tree, "body", ())
    for node in body:
        if isinstance(node, ast.ClassDef):
            symbols.append(SymbolFact(node.name, "class", int(node.lineno)))
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(SymbolFact(node.name, "async_function", int(node.lineno)))
        elif isinstance(node, ast.FunctionDef):
            symbols.append(SymbolFact(node.name, "function", int(node.lineno)))
        if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
            break
    return tuple(symbols)


def _add_graph_nodes_and_edges(
    state: _GraphBuildState,
    root: Path,
    facts: tuple[PythonFileFact, ...],
    module_map: Mapping[str, PythonFileFact],
) -> None:
    root_id = "repo:."
    state.append_node({"id": root_id, "kind": "repoRoot", "label": root.name or root.as_posix(), "path": "."})
    for fact in facts:
        file_id = _file_id(fact.path)
        package_id = _package_id(fact.module)
        state.append_node(
            {
                "id": package_id,
                "kind": "package",
                "label": _package_name(fact.module),
                "module": _package_name(fact.module),
            }
        )
        state.append_node(
            {
                "id": file_id,
                "kind": "file",
                "label": Path(fact.path).name,
                "path": fact.path,
                "module": fact.module,
                "language": "python",
                "lineCount": fact.line_count,
                "syntaxOk": fact.syntax_ok,
            }
        )
        state.append_edge(root_id, package_id, "contains", detail=_package_name(fact.module))
        state.append_edge(package_id, file_id, "contains", detail=fact.path)
        _add_symbol_nodes(state, fact, file_id)
        _add_import_edges(state, fact, module_map, file_id)
    _add_test_edges(state, facts, module_map)


def _add_symbol_nodes(state: _GraphBuildState, fact: PythonFileFact, file_id: str) -> None:
    for symbol in fact.symbols:
        if state.symbol_count >= state.max_symbols:
            state.truncated = True
            return
        symbol_id = f"symbol:{fact.path}:{symbol.name}"
        state.append_node(
            {
                "id": symbol_id,
                "kind": symbol.kind,
                "label": symbol.name,
                "path": fact.path,
                "module": fact.module,
                "qualifiedName": f"{fact.module}.{symbol.name}" if fact.module else symbol.name,
                "line": symbol.line,
            }
        )
        state.symbol_count += 1
        state.append_edge(file_id, symbol_id, "defines", detail=symbol.kind)


def _add_import_edges(
    state: _GraphBuildState,
    fact: PythonFileFact,
    module_map: Mapping[str, PythonFileFact],
    file_id: str,
) -> None:
    for imported in fact.imports:
        target = _local_import_target(imported, module_map)
        if target is None:
            continue
        target_file_id = _file_id(target.path)
        state.append_edge(file_id, target_file_id, "imports", detail=imported)


def _add_test_edges(
    state: _GraphBuildState,
    facts: tuple[PythonFileFact, ...],
    module_map: Mapping[str, PythonFileFact],
) -> None:
    for fact in facts:
        if not _is_test_file(fact.path):
            continue
        test_file_id = _file_id(fact.path)
        for imported in fact.imports:
            target = _local_import_target(imported, module_map)
            if target is not None and target.path != fact.path:
                state.append_edge(
                    test_file_id,
                    _file_id(target.path),
                    "tests",
                    detail=imported,
                    provenance_source="inferred",
                    confidence=0.74,
                    basis="Test file imports a local project module.",
                )
        for target in _test_name_targets(fact.path, facts):
            state.append_edge(
                test_file_id,
                _file_id(target.path),
                "tests",
                detail=Path(fact.path).name,
                provenance_source="inferred",
                confidence=0.56,
                basis="Test filename resembles a local source module name.",
            )


def _local_import_target(imported: str, module_map: Mapping[str, PythonFileFact]) -> PythonFileFact | None:
    candidate = imported
    while "." in candidate:
        target = module_map.get(candidate)
        if target is not None:
            return target
        candidate = candidate.rsplit(".", 1)[0]
    return module_map.get(candidate)


def _test_name_targets(path: str, facts: tuple[PythonFileFact, ...]) -> tuple[PythonFileFact, ...]:
    stem = Path(path).stem
    if stem.startswith("test_"):
        candidate = stem.removeprefix("test_")
    elif stem.endswith("_test"):
        candidate = stem.removesuffix("_test")
    else:
        return ()
    return tuple(fact for fact in facts if not _is_test_file(fact.path) and Path(fact.path).stem == candidate)


def _file_fact_payload(fact: PythonFileFact) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": fact.path,
        "module": fact.module,
        "language": "python",
        "lineCount": fact.line_count,
        "syntaxOk": fact.syntax_ok,
        "importCount": len(fact.imports),
        "symbolCount": len(fact.symbols),
    }
    if fact.parse_error:
        payload["parseError"] = fact.parse_error
    if fact.skipped_reason:
        payload["skippedReason"] = fact.skipped_reason
    return payload


def _file_id(path: str) -> str:
    return f"file:{path}"


def _package_id(module: str) -> str:
    return f"package:{_package_name(module)}"


def _package_name(module: str) -> str:
    return module.split(".", 1)[0] if module else "root"


def _is_test_file(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _syntax_error_summary(error: SyntaxError) -> str:
    detail = error.msg or "syntax error"
    return f"{detail} at line {error.lineno or 0}"


def _skip_path_part(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _REPO_EXCLUDED_NAMES
        or lowered in _SENSITIVE_PATH_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(_SENSITIVE_SUFFIXES)
    )


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


__all__ = [
    "SEMANTIC_REPO_GRAPH_SCHEMA",
    "PythonFileFact",
    "SemanticRepoGraphConfig",
    "SymbolFact",
    "semantic_repo_graph_context",
]
