"""Static tripwires (rule 9): plugins import only the contract and a few pure standard modules,
and call nothing that reaches outside the process; framework modules import no storage or
network client and name no label table or marker. A tripwire, not a sandbox: Python code can
always escape a static check; review and the trace view's filtering are the real boundary."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "traceblame_detectors"
TEST_PLUGINS = Path(__file__).resolve().parent / "plugins"

PLUGIN_IMPORTS = frozenset(
    {
        "__future__",
        "traceblame_detectors.api",
        "typing",
        "dataclasses",
        "math",
        "statistics",
        "collections",
        "collections.abc",
        "re",
        "enum",
        "functools",
    }
)
BANNED_NAMES = frozenset(
    {
        "__import__",
        "importlib",
        "open",
        "eval",
        "exec",
        "compile",
        "__builtins__",
        "builtins",
        "globals",
        "vars",
        "breakpoint",
        "input",
        "memoryview",
        "getattr",
        "setattr",
        "delattr",
    }
)
BANNED_ATTRS = frozenset(
    {
        "__globals__",
        "__subclasses__",
        "__builtins__",
        "__code__",
        "__dict__",
        "__class__",
        "__base__",
        "__bases__",
        "__mro__",
    }
)

FRAMEWORK = (
    "api.py",
    "canonical.py",
    "config.py",
    "context.py",
    "engine.py",
    "model.py",
    "view.py",
)
FRAMEWORK_BANNED_IMPORTS = (
    "traceblame_storage",
    "clickhouse_connect",
    "psycopg",
    "nats",
    "socket",
    "os",
    "subprocess",
    "urllib",
    "http",
    "requests",
)
LABELS = ("eval_traces", "injections", "aegis.eval.", "aegis.fault.", "eval.run.id", "injection.id")


def _imports(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append("." * node.level + (node.module or ""))
    return out


def plugin_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    problems = [f"import {m}" for m in _imports(tree) if m not in PLUGIN_IMPORTS]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            problems.append(f"attribute {node.attr}")
    return problems


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def framework_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    problems = [
        f"import {m}" for m in _imports(tree) if m.split(".")[0] in FRAMEWORK_BANNED_IMPORTS
    ]
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            problems.extend(f"literal {label!r}" for label in LABELS if label in node.value)
    return problems


def _plugin_files() -> list[Path]:
    shipped = [p for p in (SRC / "plugins").rglob("*.py") if "tests" not in p.parts]
    return shipped + sorted(TEST_PLUGINS.rglob("*.py"))


def test_plugins_pass_the_guard() -> None:
    files = _plugin_files()
    assert len(files) >= 4  # plugins/__init__.py and the reference detectors
    for path in files:
        if path == SRC / "plugins" / "__init__.py":
            continue  # the registry imports plugin modules, not a plugin itself
        assert plugin_problems(path.read_text(encoding="utf-8")) == [], path


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("import os\n", "import os"),
        ("from traceblame_storage import client\n", "import traceblame_storage"),
        ("import clickhouse_connect\n", "import clickhouse_connect"),
        (
            "from traceblame_detectors.sql_statements import statement\n",
            "import traceblame_detectors.sql_statements",
        ),
        ("from . import sibling\n", "import ."),
        ("x = __import__('os')\n", "name __import__"),
        ("x = open('/etc/passwd').read()\n", "name open"),
        ("eval('1')\n", "name eval"),
        ("exec('x=1')\n", "name exec"),
        ("compile('1', 'f', 'eval')\n", "name compile"),
        ("x = getattr(__builtins__, 'open')\n", "name __builtins__"),
        ("x = getattr(object, 'x')\n", "name getattr"),
        ("x = ().__class__.__base__\n", "attribute __class__"),
        ("x = ().__class__.__base__.__subclasses__()\n", "attribute __subclasses__"),
        ("x = (lambda: 0).__globals__\n", "attribute __globals__"),
    ],
)
def test_the_plugin_guard_fails_on_sabotage(source: str, problem: str) -> None:
    assert problem in plugin_problems(source)


def test_framework_modules_pass_the_guard() -> None:
    files = [SRC / name for name in FRAMEWORK] + sorted((SRC / "sources").glob("*.py"))
    assert len(files) >= len(FRAMEWORK)
    for path in files:
        problems = framework_problems(path.read_text(encoding="utf-8"))
        if path.parent.name == "sources":  # sources read ClickHouse by design; no labels though
            problems = [p for p in problems if p.startswith("literal")]
        assert problems == [], path


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("import socket\n", "import socket"),
        (
            "from traceblame_storage.client import ClickHouseClient\n",
            "import traceblame_storage.client",
        ),
        ("SQL = 'SELECT golden_doc_ids FROM eval_traces'\n", "literal 'eval_traces'"),
        ("KEY = 'aegis.fault.injection.id'\n", "literal 'aegis.fault.'"),
    ],
)
def test_the_framework_guard_fails_on_sabotage(source: str, problem: str) -> None:
    assert problem in framework_problems(source)


def test_a_docstring_may_name_what_is_off_limits() -> None:
    assert framework_problems('"""never reads eval_traces or injections"""\n') == []
