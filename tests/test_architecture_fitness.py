from __future__ import annotations

import ast
import random
import re
import xml.dom.minidom
from pathlib import Path

from _oracle import brute_force_subset_sum, generate_random_instance, verify_reconciliation_result
from qistas.matcher import reconcile, solve_subset_sum_batch
from qistas.models import MatchKind

_ROOT = Path(__file__).resolve().parent.parent
_SRC_PKG = _ROOT.joinpath("src", "qistas")
_TESTS_DIR = _ROOT.joinpath("tests")

_ALLOWLISTS: dict[str, frozenset[str]] = {
    "models.py": frozenset({"__future__", "datetime", "dataclasses", "enum", "re"}),
    "normalizer.py": frozenset({"__future__", "re", "unicodedata", "qistas.models"}),
    "ledger.py": frozenset({
        "__future__", "collections.abc", "dataclasses", "hashlib", "qistas.models",
    }),
    "matcher.py": frozenset({
        "__future__", "collections.abc", "dataclasses",
        "qistas.models", "qistas.normalizer", "qistas.ledger",
    }),
    "intake_gate.py": frozenset({
        "__future__", "collections.abc", "dataclasses", "json", "re",
        "qistas.models", "qistas.normalizer",
    }),
    "exporters.py": frozenset({
        "__future__", "collections.abc", "os", "pathlib", "unicodedata",
        "duckdb", "openpyxl", "openpyxl.styles", "openpyxl.utils",
        "openpyxl.worksheet.worksheet", "qistas.models", "qistas.ledger", "qistas.matcher",
    }),
    "cli_visualizer.py": frozenset({
        "__future__", "argparse", "collections.abc", "pathlib",
        "qistas.models", "qistas.normalizer", "qistas.ledger",
        "qistas.matcher", "qistas.intake_gate", "qistas.exporters",
    }),
    "__init__.py": frozenset({
        "__future__", "qistas.models", "qistas.normalizer", "qistas.ledger",
        "qistas.matcher", "qistas.intake_gate", "qistas.exporters", "qistas.cli_visualizer",
    }),
}

_BANNED_SRC_IMPORTS = frozenset({"math", "decimal", "fractions", "statistics"})
_BANNED_SRC_CALLS = frozenset({"eval", "exec", "compile", "open", "__import__", "input"})
_BANNED_NUM_CALLS = frozenset({"float", "complex", "Decimal", "Fraction", "round"})
_EXPECTED_DATACLASSES = frozenset({
    "FeeSchedule",
    "Invoice",
    "BankTx",
    "LedgerPosting",
    "JournalEntry",
    "ReconciliationMatch",
    "UnpaidInvoiceRecord",
    "AuditLogEntry",
    "TrialBalance",
    "ReconciliationResult",
    "RecordRejection",
})


def _all_py_files() -> list[Path]:
    src_files = sorted(_SRC_PKG.glob("*.py"))
    test_files = sorted(_TESTS_DIR.glob("*.py"))
    return [*src_files, *test_files]


def _extract_module_imports(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            mods.add(node.module)
    return mods


def test_ff1_zero_float_and_division_ast() -> None:
    files = _all_py_files()
    assert len(files) >= 10
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Div), f"ast.Div found in {path}:{node.lineno}"
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, (float, complex)), (
                    f"float/complex literal found in {path}:{node.lineno}"
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _BANNED_NUM_CALLS, (
                    f"banned call {node.func.id} in {path}:{node.lineno}"
                )
        if path.parent == _SRC_PKG:
            mods = _extract_module_imports(tree)
            bad = mods & _BANNED_SRC_IMPORTS
            assert not bad, f"banned numeric import(s) {bad} in {path}"


def test_ff2_import_allowlist_and_banned_calls_ast() -> None:
    src_files = sorted(_SRC_PKG.glob("*.py"))
    assert {p.name for p in src_files} == set(_ALLOWLISTS.keys())
    for path in src_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _extract_module_imports(tree)
        extra = imports - _ALLOWLISTS[path.name]
        assert not extra, f"disallowed imports {extra} in {path.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _BANNED_SRC_CALLS, (
                    f"banned call {node.func.id} in {path.name}:{node.lineno}"
                )


def _collect_call_graph(tree: ast.Module) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for top in tree.body:
        if isinstance(top, ast.FunctionDef):
            key = top.name
            callees: set[str] = set()
            for node in ast.walk(top):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    callees.add(node.func.id)
            edges[key] = callees
        elif isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, ast.FunctionDef):
                    key = f"{top.name}.{item.name}"
                    callees = set()
                    for node in ast.walk(item):
                        if isinstance(node, ast.Call):
                            if isinstance(node.func, ast.Name):
                                callees.add(node.func.id)
                            elif (
                                isinstance(node.func, ast.Attribute)
                                and isinstance(node.func.value, ast.Name)
                                and node.func.value.id == "self"
                            ):
                                callees.add(f"{top.name}.{node.func.attr}")
                    edges[key] = callees
    return edges


def test_ff3_zero_recursion_ast() -> None:
    for path in sorted(_SRC_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        graph = _collect_call_graph(tree)
        state: dict[str, int] = dict.fromkeys(graph, 0)
        for start in sorted(graph):
            if state[start] != 0:
                continue
            stack: list[tuple[str, bool]] = [(start, False)]
            while stack:
                node, exiting = stack.pop()
                if exiting:
                    state[node] = 2
                    continue
                if state[node] == 2:
                    continue
                state[node] = 1
                stack.append((node, True))
                for nxt in sorted(graph.get(node, set())):
                    if nxt not in graph:
                        continue
                    assert state[nxt] != 1, f"recursion cycle {node} -> {nxt} in {path.name}"
                    if state[nxt] == 0:
                        stack.append((nxt, False))


def test_ff4_function_length_ceiling_ast() -> None:
    for path in sorted(_SRC_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno is not None
                loc = node.end_lineno - node.lineno + 1
                assert loc <= 60, f"{path.name}:{node.name} length {loc} > 60"


def test_ff5_assertion_density_ast() -> None:
    for path in sorted(_SRC_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                asserts = sum(1 for sub in ast.walk(node) if isinstance(sub, ast.Assert))
                assert asserts >= 2, f"{path.name}:{node.name} has {asserts} assertions (< 2)"


def test_ff6_immutable_slot_dataclasses_ast() -> None:
    found: set[str] = set()
    for path in sorted(_SRC_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for dec in node.decorator_list:
                is_dc = (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "dataclass"
                ) or (isinstance(dec, ast.Name) and dec.id == "dataclass")
                if not is_dc:
                    continue
                found.add(node.name)
                assert isinstance(dec, ast.Call), f"{node.name} dataclass missing slots/frozen args"
                kw_map = {kw.arg: kw.value for kw in dec.keywords if kw.arg is not None}
                assert (
                    isinstance(kw_map.get("slots"), ast.Constant)
                    and kw_map["slots"].value is True
                ), f"{node.name} must specify slots=True"
                assert (
                    isinstance(kw_map.get("frozen"), ast.Constant)
                    and kw_map["frozen"].value is True
                ), f"{node.name} must specify frozen=True"
    assert found == _EXPECTED_DATACLASSES


def _run_oracle_corpus(seed: int, count: int) -> dict[MatchKind, int]:
    rng = random.Random(seed)
    kind_counts: dict[MatchKind, int] = dict.fromkeys(MatchKind, 0)
    for idx in range(count):
        invoices, bank_txs, fee = generate_random_instance(rng, idx)
        cands = [inv for inv in invoices if inv.id.startswith(f"C-{idx}-")]
        tx_c = next(tx for tx in bank_txs if tx.id == f"TXC-{idx}-1")
        sub_actual, fee_actual, _, optimal = solve_subset_sum_batch(cands, tx_c, fee)
        sub_oracle, fee_oracle = brute_force_subset_sum(cands, tx_c, fee)
        assert optimal
        assert tuple(i.id for i in sub_actual) == tuple(i.id for i in sub_oracle)
        assert fee_actual == fee_oracle

        result = reconcile(invoices, bank_txs, fee)
        verify_reconciliation_result(invoices, bank_txs, fee, result)
        for m in result.matches:
            kind_counts[m.match_kind] += 1
    return kind_counts


def test_ff7_seeded_oracle_fuzzer_50_instances() -> None:
    counts = _run_oracle_corpus(seed=42, count=50)
    assert sum(counts.values()) >= 200
    assert counts[MatchKind.BATCH_SUBSET_SUM] >= 30


def test_ff7b_deep_oracle_fuzzer_1000_instances_with_coverage_floors() -> None:
    counts = _run_oracle_corpus(seed=2026, count=1_000)
    assert counts[MatchKind.BATCH_SUBSET_SUM] >= 200
    assert (counts[MatchKind.FEE_ADJUSTED_REF] + counts[MatchKind.ENTITY_FEE_ADJUSTED]) >= 200
    assert counts[MatchKind.PARTIAL_PAYMENT] >= 100
    assert counts[MatchKind.DUPLICATE_PAYMENT_ALERT] >= 50
    assert counts[MatchKind.ORPHAN_BANK_DEPOSIT] >= 100


def test_ff8_svg_and_docs_integrity() -> None:
    assets_dir = _ROOT.joinpath("assets")
    for svg_name in ("architecture_pipeline.svg", "reconciliation_ledger_flow.svg"):
        svg_path = assets_dir.joinpath(svg_name)
        assert svg_path.exists(), f"missing SVG: {svg_name}"
        raw_svg = svg_path.read_text(encoding="utf-8")
        doc = xml.dom.minidom.parseString(raw_svg)
        root = doc.documentElement
        assert root is not None and root.tagName == "svg"
        assert root.hasAttribute("viewBox") and bool(root.getAttribute("viewBox"))
        assert root.hasAttribute("width") and bool(root.getAttribute("width"))
        for marker in doc.getElementsByTagName("marker"):
            mid = marker.getAttribute("id")
            assert mid and f"url(#{mid})" in raw_svg, f"unused marker {mid} in {svg_name}"
        for g_node in doc.getElementsByTagName("g"):
            for t_node in g_node.getElementsByTagName("text"):
                txt = "".join(
                    c.data for c in t_node.childNodes if c.nodeType == c.TEXT_NODE
                ).strip()
                assert len(txt) <= 46, f"overflowing text ({len(txt)} chars) in {svg_name}: {txt!r}"

    for doc_name in ("README.md", "ADR.md", "BLUEPRINT.md", "SPEC.md"):
        doc_path = _ROOT.joinpath(doc_name)
        assert doc_path.exists(), f"missing doc: {doc_name}"
        text = doc_path.read_text(encoding="utf-8")
        assert len(text.splitlines()) >= 50
        assert "$" not in text, f"LaTeX '$' delimiter found in {doc_name}"

    readme_text = _ROOT.joinpath("README.md").read_text(encoding="utf-8")
    img_srcs = re.findall(r'<img\s+[^>]*src="([^"]+)"', readme_text)
    assert len(img_srcs) >= 3
    for rel_src in img_srcs:
        assert _ROOT.joinpath(rel_src).exists(), f"missing README image asset: {rel_src}"

    loc_rows = re.findall(
        r"\[`src/qistas/([a-z_]+\.py)`\]\([^)]+\)\s*\|\s*`([0-9,]+)`\s*\|",
        readme_text,
    )
    assert len(loc_rows) == 8
    for mod_name, loc_str in loc_rows:
        actual_loc = len(_SRC_PKG.joinpath(mod_name).read_text(encoding="utf-8").splitlines())
        assert int(loc_str.replace(",", "")) == actual_loc, (
            f"README LOC drift for {mod_name}: {loc_str} != {actual_loc}"
        )

    pyproject_text = _ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
    assert 'readme = "README.md"' in pyproject_text
    assert 'license = { text = "MIT" }' in pyproject_text

    lic_path = _ROOT.joinpath("LICENSE")
    assert lic_path.exists()
    assert "MIT License" in lic_path.read_text(encoding="utf-8")
