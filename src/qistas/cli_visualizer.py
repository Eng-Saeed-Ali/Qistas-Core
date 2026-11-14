from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from qistas.exporters import export_xlsx_atomic
from qistas.intake_gate import ingest_json_bundle
from qistas.ledger import verify_audit_chain
from qistas.matcher import ReconciliationResult, reconcile
from qistas.models import (
    BankTx,
    FeeSchedule,
    Invoice,
    parse_iso_day,
)
from qistas.normalizer import normalize_entity


def format_money(cents: int, currency: str = "EGP") -> str:
    if type(cents) is not int:
        raise TypeError("cents must be int")
    if type(currency) is not str:
        raise TypeError("currency must be str")
    clean_cur = currency.strip()
    if not clean_cur or "\x00" in clean_cur:
        raise ValueError("currency must be non-empty string")
    abs_c = abs(cents)
    whole = abs_c // 100
    frac = abs_c % 100
    sign = "-" if cents < 0 else ""
    out = f"{sign}{whole:,}.{frac:02d} {clean_cur}"
    assert type(out) is str
    assert out.endswith(clean_cur)
    return out


def _build_demo_invoices() -> tuple[Invoice, ...]:
    specs = (
        ("INV-1001", "Delta Logistics LLC", 100_000, "2026-03-01", "INV-1001"),
        ("INV-1002", "Alpha Supplies Ltd", 200_000, "2026-03-03", "INV-1002"),
        ("INV-1003", "شركة الأهرام للتوريدات", 150_000, "2026-03-05", "INV-1003"),
        ("INV-1004", "مؤسسة النور للمقاولات", 40_000, "2026-03-06", "INV-1004"),
        ("INV-1005", "مؤسسة النور للمقاولات", 60_000, "2026-03-07", "INV-1005"),
        ("INV-1006", "مؤسسة النور للمقاولات", 80_000, "2026-03-08", "INV-1006"),
        ("INV-1007", "Giza Trading Co", 120_000, "2026-03-11", "INV-1007"),
        ("INV-1008", "Sinai Imports Inc", 50_000, "2026-03-13", "INV-1008"),
    )
    invs = tuple(
        Invoice(
            id=inv_id,
            raw_entity=entity,
            norm_entity=normalize_entity(entity),
            amount_cents=amt,
            day_ordinal=parse_iso_day(iso_dt),
            ref_code=ref_code,
        )
        for inv_id, entity, amt, iso_dt, ref_code in specs
    )
    assert len(invs) == 8
    assert all(inv.amount_cents > 0 for inv in invs)
    return invs


def _build_demo_bank_txs() -> tuple[BankTx, ...]:
    specs = (
        (
            "TX-2001",
            "Delta Logistics",
            100_000,
            "2026-03-02",
            "تحويل فاتورة inv_\u0661\u0660\u0660\u0661",
        ),
        ("TX-2002", "Alpha Supplies", 194_700, "2026-03-04", "Payment INV-1002 gateway"),
        (
            "TX-2003",
            "\u200fشركة \u0627\ufef7\u0647\u0631\u0627\u0645 للتوريدات",
            150_000,
            "2026-03-08",
            "سداد مستحقات",
        ),
        ("TX-2004", "مؤسسة النور للمقاولات", 180_000, "2026-03-09", "دفعة مجمعة"),
        ("TX-2005", "Delta Logistics LLC", 100_000, "2026-03-10", "Duplicate transfer INV-1001"),
        ("TX-2006", "Giza Trading Co", 75_000, "2026-03-12", "Part payment INV-1007"),
        ("TX-2007", "Sinai Imports Inc", 65_000, "2026-03-14", "Wire INV-1008"),
        ("TX-2008", "---", 25_000, "2026-03-15", "ATM CASH DEPOSIT"),
    )
    txs = tuple(
        BankTx(
            id=tx_id,
            raw_payer=payer,
            norm_entity=normalize_entity(payer),
            net_cents=net_c,
            day_ordinal=parse_iso_day(iso_dt),
            memo=memo,
        )
        for tx_id, payer, net_c, iso_dt, memo in specs
    )
    assert len(txs) == 8
    assert all(tx.net_cents > 0 for tx in txs)
    return txs


def build_demo_scenario() -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]:
    fee = FeeSchedule(
        fixed_fee_cents=300,
        rate_permille=25,
        min_fee_cents=500,
        max_fee_cents=50_000,
        tolerance_cents=50,
        max_settlement_lag_days=5,
    )
    invoices = _build_demo_invoices()
    bank_txs = _build_demo_bank_txs()
    assert len(invoices) == 8 and len(bank_txs) == 8
    assert fee.max_settlement_lag_days == 5
    return invoices, bank_txs, fee


def _render_matches_section(result: ReconciliationResult, currency: str) -> list[str]:
    hdr = (
        f"{'TX ID':<10} | {'MATCH KIND':<24} | {'INVOICES':<26} | "
        f"{'GROSS':>15} | {'FEE':>13} | {'NET':>15}"
    )
    lines = ["[MATCHES]", hdr, "-" * 116]
    for m in result.matches:
        inv_text = ",".join(m.invoice_ids) if m.invoice_ids else "-"
        lines.append(
            f"{m.tx_id:<10} | {m.match_kind.value:<24} | {inv_text:<26} | "
            f"{format_money(m.gross_cents, currency):>15} | "
            f"{format_money(m.fee_cents, currency):>13} | "
            f"{format_money(m.net_cents, currency):>15}"
        )
    assert len(lines) == 3 + len(result.matches)
    assert lines[0] == "[MATCHES]"
    return lines


def _render_trial_balance_section(result: ReconciliationResult, currency: str) -> list[str]:
    tb = result.trial_balance
    lines = [
        "[DOUBLE-ENTRY TRIAL BALANCE]",
        f"{'ACCOUNT CODE':<28} | {'DEBIT':>18} | {'CREDIT':>18}",
        "-" * 70,
    ]
    for (d_acct, d_amt), (_, c_amt) in zip(
        tb.debits_by_account, tb.credits_by_account, strict=True
    ):
        lines.append(
            f"{d_acct.value:<28} | {format_money(d_amt, currency):>18} | "
            f"{format_money(c_amt, currency):>18}"
        )
    balanced = "BALANCED" if tb.total_debit_cents == tb.total_credit_cents else "UNBALANCED"
    lines.append("-" * 70)
    lines.append(
        f"{'TOTAL (' + balanced + ')':<28} | "
        f"{format_money(tb.total_debit_cents, currency):>18} | "
        f"{format_money(tb.total_credit_cents, currency):>18}"
    )
    assert tb.total_debit_cents == tb.total_credit_cents
    assert len(lines) == 5 + len(tb.debits_by_account)
    return lines


def _render_unpaid_and_audit_section(result: ReconciliationResult, currency: str) -> list[str]:
    lines = [
        "[UNPAID INVOICE RESIDUALS]",
        f"{'INVOICE ID':<12} | {'NORMALIZED ENTITY':<30} | {'RESIDUAL':>18}",
        "-" * 66,
    ]
    if result.unpaid_invoices:
        for u in result.unpaid_invoices:
            lines.append(
                f"{u.invoice_id:<12} | {u.norm_entity:<30} | "
                f"{format_money(u.residual_cents, currency):>18}"
            )
    else:
        lines.append("(none)")
    chain_ok = verify_audit_chain(result.audit_chain)
    tail_hash = result.audit_chain[-1].chain_hash_u64 if result.audit_chain else 0
    chain_status = "PASS" if chain_ok else "FAIL"
    lines.extend([
        "",
        "[AUDIT CHAIN & SOLVER SUMMARY]",
        f"BLAKE2b-64 Audit Chain : {chain_status} ({len(result.audit_chain)} entries)",
        f"Chain Tail Hash (u64)  : 0x{tail_hash:016x}",
        (
            f"Subset-Sum Expansions  : {result.expansions} "
            f"(proven_optimal={result.proven_optimal_batches})"
        ),
    ])
    assert type(chain_ok) is bool
    assert len(lines) >= 7
    return lines


def render_reconciliation_report(result: ReconciliationResult, currency: str = "EGP") -> str:
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be ReconciliationResult")
    format_money(0, currency)
    sections = [
        "=" * 116,
        "QISTAS-CORE DETERMINISTIC RECONCILIATION REPORT",
        "=" * 116,
        *_render_matches_section(result, currency),
        "",
        *_render_trial_balance_section(result, currency),
        "",
        *_render_unpaid_and_audit_section(result, currency),
        "=" * 116,
    ]
    report = "\n".join(sections)
    assert type(report) is str
    assert "QISTAS-CORE" in report
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qistas",
        description="Qistas-Core zero-float bilingual financial reconciliation CLI.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--demo",
        action="store_true",
        help="Run built-in bilingual Arabic/English demo scenario (default).",
    )
    source.add_argument(
        "--bundle",
        type=str,
        default=None,
        help="Path to a qistas.bundle/v1 JSON file.",
    )
    parser.add_argument(
        "--export-xlsx",
        type=str,
        default=None,
        help="Write atomic multi-sheet XLSX workbook to the specified path.",
    )
    parser.add_argument(
        "--currency",
        type=str,
        default="EGP",
        help="Display currency suffix for report formatting (default: EGP).",
    )
    assert isinstance(parser, argparse.ArgumentParser)
    assert parser.prog == "qistas"
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.bundle is not None:
        raw_bytes = Path(args.bundle).read_bytes()
        invoices, bank_txs, fee = ingest_json_bundle(raw_bytes)
    else:
        invoices, bank_txs, fee = build_demo_scenario()
    result = reconcile(invoices, bank_txs, fee)
    assert verify_audit_chain(result.audit_chain)
    assert result.trial_balance.total_debit_cents == result.trial_balance.total_credit_cents
    if args.export_xlsx is not None:
        export_xlsx_atomic(result, Path(args.export_xlsx))
    print(render_reconciliation_report(result, currency=args.currency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
