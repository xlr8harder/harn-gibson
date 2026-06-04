from __future__ import annotations

from pathlib import Path

from harn_gibson import SEMANTIC_REPO_GRAPH_SCHEMA
from harn_gibson.semantic_graph import SemanticRepoGraphConfig, semantic_repo_graph_context


def test_semantic_repo_graph_extracts_imports_symbols_and_test_edges(tmp_path: Path) -> None:
    repo = _semantic_fixture(tmp_path)

    graph = semantic_repo_graph_context(
        SemanticRepoGraphConfig(project_root=str(repo), max_files=32, max_edges=96, max_symbols=16)
    )

    assert graph["schema"] == SEMANTIC_REPO_GRAPH_SCHEMA
    assert graph["available"] is True
    assert graph["rootName"] == "repo"
    assert graph["languages"] == ["python"]
    assert graph["fileCount"] == 10
    assert graph["truncated"] is False
    assert {item["path"] for item in graph["files"]} == {
        "src/demo/__init__.py",
        "src/demo/core.py",
        "src/demo/helpers.py",
        "src/demo/broken.py",
        "lonely.py",
        "root_rel.py",
        "tests/behavior.py",
        "tests/test_self.py",
        "tests/core_test.py",
        "tests/test_core.py",
    }
    assert {item["path"] for item in graph["files"] if not item["syntaxOk"]} == {"src/demo/broken.py"}
    assert "secrets/private.py" not in {item["path"] for item in graph["files"]}
    assert ".venv/tool.py" not in {item["path"] for item in graph["files"]}
    assert ".env.local.py" not in {item["path"] for item in graph["files"]}
    assert "private.key/secret.py" not in {item["path"] for item in graph["files"]}
    assert "src/demo/link.py" not in {item["path"] for item in graph["files"]}

    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["repo:."]["kind"] == "repoRoot"
    assert nodes["package:demo"]["kind"] == "package"
    assert nodes["file:src/demo/core.py"]["module"] == "demo.core"
    assert nodes["symbol:src/demo/core.py:Engine"]["kind"] == "class"
    assert nodes["symbol:src/demo/core.py:crawl"]["kind"] == "async_function"
    assert nodes["symbol:src/demo/core.py:run"]["qualifiedName"] == "demo.core.run"
    assert nodes["file:root_rel.py"]["module"] == "root_rel"

    edges = {
        (edge["source"], edge["target"], edge["relationship"], edge.get("detail", "")): edge
        for edge in graph["edges"]
    }
    assert ("file:src/demo/core.py", "file:src/demo/helpers.py", "imports", "demo.helpers") in edges
    assert ("file:src/demo/core.py", "file:src/demo/helpers.py", "imports", "demo.helpers.Helper") in edges
    assert ("file:root_rel.py", "file:lonely.py", "imports", "lonely") in edges
    assert (
        "file:tests/test_core.py",
        "file:src/demo/core.py",
        "tests",
        "demo.core",
    ) in edges
    assert (
        "file:tests/core_test.py",
        "file:src/demo/core.py",
        "tests",
        "core_test.py",
    ) in edges
    assert edges[
        ("file:tests/core_test.py", "file:src/demo/core.py", "tests", "core_test.py")
    ]["provenance"] == {
        "source": "inferred",
        "confidence": 0.56,
        "basis": "Test filename resembles a local source module name.",
    }
    assert graph["importEdgeCount"] >= 2
    assert graph["testEdgeCount"] >= 2
    assert graph["provenance"]["source"] == "observed"


def test_semantic_repo_graph_reports_missing_root() -> None:
    graph = semantic_repo_graph_context(SemanticRepoGraphConfig(project_root="/tmp/harn-gibson-missing-root"))

    assert graph == {
        "schema": SEMANTIC_REPO_GRAPH_SCHEMA,
        "rootName": "harn-gibson-missing-root",
        "maxFiles": 96,
        "maxEdges": 192,
        "maxSymbols": 160,
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


def test_semantic_repo_graph_bounds_files_edges_symbols_and_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def one():\n    return 1\n\ndef two():\n    return 2\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("from .a import one\n", encoding="utf-8")
    (repo / "src" / "c.py").write_text("from .b import one\n", encoding="utf-8")
    (repo / "src" / "big.py").write_text("print('too large')\n", encoding="utf-8")
    (repo / "src" / "many.py").write_text(
        "\n".join(f"def func_{index}():\n    return {index}" for index in range(18)) + "\n",
        encoding="utf-8",
    )
    (repo / "src" / "imports_many.py").write_text(
        "\n".join(f"import external_{index}" for index in range(40)) + "\n",
        encoding="utf-8",
    )
    (repo / "src" / "private.key").mkdir()
    (repo / "src" / "private.key" / "hidden.py").write_text("print('skip')\n", encoding="utf-8")
    (repo / "later").mkdir()
    (repo / "later" / "z.py").write_text("print('later')\n", encoding="utf-8")
    (repo / "src" / "link.py").symlink_to("a.py")

    exact_limit = semantic_repo_graph_context(SemanticRepoGraphConfig(project_root=str(repo), max_files=1))
    single_file_repo = tmp_path / "single"
    single_file_repo.mkdir()
    (single_file_repo / "only.py").write_text("print('one')\n", encoding="utf-8")
    (single_file_repo / "notes.txt").write_text("not python\n", encoding="utf-8")
    exact_single = semantic_repo_graph_context(
        SemanticRepoGraphConfig(project_root=str(single_file_repo), max_files=1)
    )
    truncated_files = semantic_repo_graph_context(SemanticRepoGraphConfig(project_root=str(repo), max_files=2))
    bounded_edges = semantic_repo_graph_context(SemanticRepoGraphConfig(project_root=str(repo), max_edges=0))
    bounded_symbols = semantic_repo_graph_context(SemanticRepoGraphConfig(project_root=str(repo), max_symbols=1))
    bounded_bytes = semantic_repo_graph_context(
        SemanticRepoGraphConfig(project_root=str(repo), max_files=16, max_file_bytes=4)
    )

    assert exact_limit["fileCount"] == 1
    assert exact_limit["truncated"] is True
    assert exact_single["fileCount"] == 1
    assert exact_single["truncated"] is False
    assert truncated_files["fileCount"] == 2
    assert truncated_files["truncated"] is True
    assert bounded_edges["edgeCount"] == 0
    assert bounded_edges["truncated"] is True
    assert bounded_symbols["symbolCount"] == 1
    assert bounded_symbols["truncated"] is True
    assert {item["path"] for item in bounded_bytes["files"] if item.get("skippedReason")} == {
        "later/z.py",
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/big.py",
        "src/imports_many.py",
        "src/many.py",
    }


def test_semantic_repo_graph_handles_empty_repo_and_cwd(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# empty\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    graph = semantic_repo_graph_context()

    assert graph["available"] is True
    assert graph["languages"] == []
    assert graph["nodes"] == [{"id": "repo:.", "kind": "repoRoot", "label": "repo", "path": "."}]
    assert graph["edges"] == []
    assert graph["truncated"] is False


def _semantic_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "demo").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "secrets").mkdir()
    (repo / "private.key").mkdir()
    (repo / ".venv").mkdir()
    (repo / "src" / "demo" / "__init__.py").write_text("from .core import run\n", encoding="utf-8")
    (repo / "src" / "demo" / "core.py").write_text(
        "\n".join(
            [
                "import os",
                "from .helpers import helper",
                "from demo.helpers import Helper",
                "from demo.helpers import *",
                "from demo import core",
                "",
                "class Engine:",
                "    pass",
                "",
                "async def crawl():",
                "    return Helper",
                "",
                "def run():",
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "lonely.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "root_rel.py").write_text("from . import lonely\n", encoding="utf-8")
    (repo / "src" / "demo" / "helpers.py").write_text(
        "def helper():\n    return 'ok'\n\nclass Helper:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "src" / "demo" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from demo.core import run\n\ndef test_run():\n    assert run()\n",
        encoding="utf-8",
    )
    (repo / "tests" / "core_test.py").write_text("def test_name_only():\n    assert True\n", encoding="utf-8")
    (repo / "tests" / "behavior.py").write_text("def test_behavior():\n    assert True\n", encoding="utf-8")
    (repo / "tests" / "test_self.py").write_text(
        "from tests import test_self\n\ndef test_self_import():\n    assert test_self\n",
        encoding="utf-8",
    )
    (repo / "secrets" / "private.py").write_text("TOKEN = 'redacted'\n", encoding="utf-8")
    (repo / "private.key" / "secret.py").write_text("print('ignored')\n", encoding="utf-8")
    (repo / ".env.local.py").write_text("print('ignored')\n", encoding="utf-8")
    (repo / ".venv" / "tool.py").write_text("print('ignored')\n", encoding="utf-8")
    (repo / "src" / "demo" / "link.py").symlink_to("core.py")
    return repo
