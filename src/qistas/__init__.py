from __future__ import annotations

from qistas.exporters import (
    export_xlsx_atomic,
    neutralize_formula_cell,
    query_duckdb_summary,
)
from qistas.intake_gate import (
    MAX_INT_DIGITS,
    MAX_JSON_DEPTH,
    MAX_PAYLOAD_BYTES,
    MAX_RECORDS,
    IntakeError,
    RecordRejection,
    ingest_json_bundle,
    ingest_json_bundle_report,
    parse_decimal_to_cents,
)
from qistas.ledger import (
    TrialBalance,
    build_audit_chain,
    build_journal_entry,
    compute_audit_hash,
    compute_trial_balance,
    expected_gateway_fee,
    fee_matches,
    verify_audit_chain,
)
from qistas.matcher import ReconciliationResult, reconcile, solve_subset_sum_batch
from qistas.models import (
    GENESIS_HASH_U64,
    MAX_AMOUNT_CENTS,
    MAX_BATCH_SIZE,
    MAX_EXPANSIONS,
    MAX_LAG_DAYS_LIMIT,
    MAX_RATE_PERMILLE,
    AccountCode,
    AuditLogEntry,
    BankTx,
    FeeSchedule,
    Invoice,
    JournalEntry,
    LedgerPosting,
    MatchKind,
    ReconciliationMatch,
    UnpaidInvoiceRecord,
    ceil_div,
    format_iso_day,
    parse_iso_day,
)
from qistas.normalizer import extract_ref_codes, normalize_entity, normalize_text

__all__ = [
    "GENESIS_HASH_U64",
    "MAX_AMOUNT_CENTS",
    "MAX_BATCH_SIZE",
    "MAX_EXPANSIONS",
    "MAX_INT_DIGITS",
    "MAX_JSON_DEPTH",
    "MAX_LAG_DAYS_LIMIT",
    "MAX_PAYLOAD_BYTES",
    "MAX_RATE_PERMILLE",
    "MAX_RECORDS",
    "AccountCode",
    "AuditLogEntry",
    "BankTx",
    "FeeSchedule",
    "IntakeError",
    "Invoice",
    "JournalEntry",
    "LedgerPosting",
    "MatchKind",
    "ReconciliationMatch",
    "ReconciliationResult",
    "RecordRejection",
    "TrialBalance",
    "UnpaidInvoiceRecord",
    "build_audit_chain",
    "build_demo_scenario",
    "build_journal_entry",
    "ceil_div",
    "compute_audit_hash",
    "compute_trial_balance",
    "expected_gateway_fee",
    "export_xlsx_atomic",
    "extract_ref_codes",
    "fee_matches",
    "format_iso_day",
    "format_money",
    "ingest_json_bundle",
    "ingest_json_bundle_report",
    "main",
    "neutralize_formula_cell",
    "normalize_entity",
    "normalize_text",
    "parse_decimal_to_cents",
    "parse_iso_day",
    "query_duckdb_summary",
    "reconcile",
    "render_reconciliation_report",
    "solve_subset_sum_batch",
    "verify_audit_chain",
]

_LAZY_CLI_EXPORTS: frozenset[str] = frozenset({
    "build_demo_scenario",
    "format_money",
    "main",
    "render_reconciliation_report",
})


def __getattr__(name: str) -> object:
    assert type(name) is str
    assert len(_LAZY_CLI_EXPORTS) == 4
    if name in _LAZY_CLI_EXPORTS:
        import qistas.cli_visualizer as _cli

        return getattr(_cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
