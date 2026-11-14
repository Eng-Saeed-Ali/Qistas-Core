from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

import qistas
from qistas.cli_visualizer import (
    build_demo_scenario,
    format_money,
    main,
    render_reconciliation_report,
)
from qistas.exporters import (
    export_xlsx_atomic,
    neutralize_formula_cell,
    query_duckdb_summary,
)
from qistas.intake_gate import (
    MAX_PAYLOAD_BYTES,
    MAX_RECORDS,
    IntakeError,
    RecordRejection,
    ingest_json_bundle,
    ingest_json_bundle_report,
    parse_decimal_to_cents,
)
from qistas.ledger import (
    build_audit_chain,
    build_journal_entry,
    compute_trial_balance,
    verify_audit_chain,
)
from qistas.matcher import ReconciliationResult, reconcile
from qistas.models import (
    BankTx,
    FeeSchedule,
    Invoice,
    MatchKind,
    ReconciliationMatch,
    UnpaidInvoiceRecord,
    parse_iso_day,
)


def _valid_bundle_payload(
    *,
    invoices: list[object] | None = None,
    bank_txs: list[object] | None = None,
    fee_schedule: object | None = None,
) -> str:
    body: dict[str, object] = {
        "schema_version": "qistas.bundle/v1",
        "invoices": invoices if invoices is not None else [],
        "bank_txs": bank_txs if bank_txs is not None else [],
    }
    if fee_schedule is not None:
        body["fee_schedule"] = fee_schedule
    return json.dumps(body)


def test_parse_decimal_to_cents_valid_and_invalid() -> None:
    assert parse_decimal_to_cents("0.01") == 1
    assert parse_decimal_to_cents("0.5") == 50
    assert parse_decimal_to_cents("1") == 100
    assert parse_decimal_to_cents("1250.50") == 125_050
    assert parse_decimal_to_cents("999999999.99") == 999_999_999_99

    for bad in ("0", "0.00", "01.00", "-5.00", "10.123", "1e5", "10.", ".50", ""):
        with pytest.raises(IntakeError):
            parse_decimal_to_cents(bad)
    for bad_obj in (100, True, None):
        with pytest.raises(IntakeError):
            parse_decimal_to_cents(bad_obj)  # type: ignore[arg-type]
    with pytest.raises(IntakeError):
        parse_decimal_to_cents("1000000000.00")


def test_stage1_byte_cap_encoding_and_nul_defense() -> None:
    oversized_ascii = " " * (MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(oversized_ascii)

    oversized_utf8_str = "\u20ac" * 2_000_000  # 6,000,000 UTF-8 bytes > 4 MiB
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(oversized_utf8_str)

    with pytest.raises(IntakeError):
        ingest_json_bundle_report('{"schema_version": "\ud800"}')
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(b"\xff\xfe")
    with pytest.raises(IntakeError):
        ingest_json_bundle_report('{"schema_version": "qistas.bundle/v1\x00"}')
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(123)  # type: ignore[arg-type]


def test_stage2_depth_scanner_and_string_escapes() -> None:
    deep_9 = '{"schema_version": "qistas.bundle/v1", "invoices": [[[[[[[[]]]]]]]], "bank_txs": []}'
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(deep_9)

    for bad_brackets in (
        '{"schema_version": "qistas.bundle/v1", "invoices": [}',
        '{"schema_version": "qistas.bundle/v1", "invoices": []',
        '{"schema_version": "unclosed}',
    ):
        with pytest.raises(IntakeError):
            ingest_json_bundle_report(bad_brackets)

    tricky_strings = _valid_bundle_payload(
        invoices=[
            {
                "id": "INV-1",
                "entity": 'Acme \\"[[[[[[[[[[{{{{{{}}}}',
                "amount": "100.00",
                "date": "2026-01-10",
            }
        ],
        bank_txs=[
            {
                "id": "TX-1",
                "payer": "Acme Corp",
                "net_amount": "100.00",
                "date": "2026-01-11",
                "memo": 'note \\\\"{[[[[[[[[[}}}}',
            }
        ],
    )
    invs, txs, _, rejections = ingest_json_bundle_report(tricky_strings)
    assert len(invs) == 1
    assert len(txs) == 1
    assert rejections == ()


def test_stage3_zero_float_rejects_constants_huge_ints_and_dup_keys() -> None:
    for const_tok in ("NaN", "Infinity", "-Infinity"):
        payload = (
            '{"schema_version": "qistas.bundle/v1", '
            f'"fee_schedule": {{"rate_permille": {const_tok}}}, "invoices": [], "bank_txs": []}}'
        )
        with pytest.raises(IntakeError):
            ingest_json_bundle_report(payload)

    huge_int = "1" + ("0" * 15)  # 16 digits > MAX_INT_DIGITS (15)
    payload_huge = (
        '{"schema_version": "qistas.bundle/v1", '
        f'"fee_schedule": {{"fixed_fee_cents": {huge_int}}}, "invoices": [], "bank_txs": []}}'
    )
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(payload_huge)

    dup_key_payload = (
        '{"schema_version": "qistas.bundle/v1", '
        '"invoices": [], "invoices": [], "bank_txs": []}'
    )
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(dup_key_payload)

    float_fee_payload = (
        '{"schema_version": "qistas.bundle/v1", '
        '"fee_schedule": {"rate_permille": 25.5}, "invoices": [], "bank_txs": []}'
    )
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(float_fee_payload)

    bool_fee_payload = _valid_bundle_payload(fee_schedule={"rate_permille": True})
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(bool_fee_payload)

    json_decimal_payload = (
        '{"schema_version": "qistas.bundle/v1", '
        '"invoices": [{"id": "INV-1", "entity": "Acme LLC", '
        '"amount": 1250.50, "date": "2026-01-10"}], '
        '"bank_txs": [{"id": "TX-1", "payer": "Acme", "net_amount": 1250, "date": "2026-01-11"}]}'
    )
    invs, txs, _ = ingest_json_bundle(json_decimal_payload)
    assert invs[0].amount_cents == 125_050
    assert txs[0].net_cents == 125_000


def test_stage4_envelope_and_per_record_rejection_isolation() -> None:
    with pytest.raises(IntakeError):
        ingest_json_bundle_report('{"schema_version": "other/v1", "invoices": [], "bank_txs": []}')
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(
            '{"schema_version": "qistas.bundle/v1", "extra": 1, "invoices": [], "bank_txs": []}'
        )
    too_many = _valid_bundle_payload(invoices=[{"id": f"INV-{i}"} for i in range(MAX_RECORDS + 1)])
    with pytest.raises(IntakeError):
        ingest_json_bundle_report(too_many)

    mixed = _valid_bundle_payload(
        invoices=[
            {
                "id": "INV-1",
                "entity": "شركة النور للتجارة",
                "amount": "500.00",
                "date": "2026-02-01",
                "ref_code": "INV-2026-01",
            },
            {"id": "INV-BAD-AMT", "entity": "Acme", "amount": "10.999", "date": "2026-02-01"},
            {"id": "INV-1", "entity": "Dup Company", "amount": "100.00", "date": "2026-02-01"},
            ["not-a-dict"],
            {"id": "INV-2", "entity": "Beta Corp", "amount": "250.25", "date": "2026-02-02"},
        ],
        bank_txs=[
            {"id": "TX-1", "payer": "Al Noor", "net_amount": "500.00", "date": "2026-02-02"},
            {"id": "TX-BAD", "payer": "Beta", "net_amount": "250.00", "date": "bad-date"},
        ],
        fee_schedule={"fixed_fee_cents": 100, "rate_permille": 20},
    )
    invs, txs, fee, rejections = ingest_json_bundle_report(mixed.encode())
    assert [inv.id for inv in invs] == ["INV-1", "INV-2"]
    assert invs[0].ref_code == "INV-2026-01"
    assert [tx.id for tx in txs] == ["TX-1"]
    assert fee.fixed_fee_cents == 100 and fee.rate_permille == 20
    assert len(rejections) == 4
    assert all(isinstance(r, RecordRejection) for r in rejections)
    assert rejections[1].code == "DUPLICATE_ID"
    assert rejections[2].code == "INVALID_RECORD_TYPE"
    assert rejections[3].record_type == "bank_tx"

    with pytest.raises(IntakeError):
        ingest_json_bundle(mixed)


def test_cwe1236_formula_neutralization() -> None:
    payloads = (
        "=cmd|' /C calc'!A0",
        "   +SUM(A1:A2)",
        "\uff1dSUM(A1:A2)",
        "\t@malicious",
        "-100",
        "|cmd",
        "%magic",
        "\r=1",
        "\n=2",
    )
    for evil in payloads:
        sanitized = neutralize_formula_cell(evil)
        assert sanitized == "'" + evil
    assert neutralize_formula_cell("ACME CORP") == "ACME CORP"
    assert neutralize_formula_cell("Arabic Text 123") == "Arabic Text 123"
    with pytest.raises(TypeError):
        neutralize_formula_cell(123)  # type: ignore[arg-type]


def test_export_xlsx_atomic_and_duckdb_summary(tmp_path: Path) -> None:
    inv1 = Invoice("INV-1", "Acme", "acme", 10_000, parse_iso_day("2026-03-01"), "INV-001")
    inv2 = Invoice("INV-2", "=EVIL", "=EVIL", 5_000, parse_iso_day("2026-03-02"))
    tx1 = BankTx("TX-1", "Acme", "acme", 9_700, parse_iso_day("2026-03-02"), "REF INV-001")
    tx2 = BankTx("TX-2", "Unknown", "unknown", 2_500, parse_iso_day("2026-03-03"), "+cmd|calc")
    fee = FeeSchedule(fixed_fee_cents=100, rate_permille=20)
    res = reconcile([inv1, inv2], [tx1, tx2], fee)

    evil_memo_entry = build_journal_entry(
        "ENTRY-9999",
        ReconciliationMatch("TX-1", MatchKind.EXACT_REF, ("INV-1",), 10_000, 10_000, 0, 739_677),
    )
    assert evil_memo_entry.entry_id == "ENTRY-9999"

    # Also build a result explicitly carrying a malicious UnpaidInvoiceRecord entity + memo
    custom_entries = (
        res.journal_entries[0],
        res.journal_entries[1].__class__(
            entry_id=res.journal_entries[1].entry_id,
            day_ordinal=res.journal_entries[1].day_ordinal,
            match_kind=res.journal_entries[1].match_kind,
            postings=res.journal_entries[1].postings,
            memo="\uff1dHYPERLINK(\"http://bad.test\")",
        ),
    )
    custom_unpaid = (
        UnpaidInvoiceRecord("INV-2", 5_000, parse_iso_day("2026-03-02"), "=cmd|' /C calc'!A0"),
    )
    custom_res = ReconciliationResult(
        matches=res.matches,
        journal_entries=custom_entries,
        unpaid_invoices=custom_unpaid,
        audit_chain=build_audit_chain(res.matches),
        trial_balance=compute_trial_balance(custom_entries),
        expansions=res.expansions,
        proven_optimal_batches=res.proven_optimal_batches,
    )

    out_file = tmp_path.joinpath("qistas_report.xlsx")
    export_xlsx_atomic(custom_res, out_file)
    assert out_file.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp_")]
    assert leftovers == []

    wb = openpyxl.load_workbook(out_file, data_only=False)
    try:
        assert wb.sheetnames == ["Matches", "JournalEntries", "TrialBalance", "UnpaidInvoices"]
        ws_m = wb["Matches"]
        assert ws_m["G2"].value == "2026-03-02"
        assert ws_m["A4"].value == "TOTAL"
        assert ws_m["A4"].data_type == "s"
        assert ws_m["D4"].value == "=SUM(D2:D3)"
        assert ws_m["D4"].data_type == "f"
        assert ws_m["E4"].value == "=SUM(E2:E3)"
        assert ws_m["E4"].data_type == "f"
        assert ws_m["F4"].value == "=SUM(F2:F3)"
        assert ws_m["F4"].data_type == "f"
        assert ws_m["D2"].number_format == "#,##0"
        assert ws_m["D4"].number_format == "#,##0"
        assert str(ws_m["A1"].fill.fgColor.rgb).endswith("0F172A")
        assert str(ws_m["A4"].fill.fgColor.rgb).endswith("D1FAE5")

        ws_tb = wb["TrialBalance"]
        assert ws_tb["A7"].value == "TOTAL"
        assert ws_tb["A7"].data_type == "s"
        assert ws_tb["B7"].value == "=SUM(B2:B6)"
        assert ws_tb["B7"].data_type == "f"
        assert ws_tb["C7"].value == "=SUM(C2:C6)"
        assert ws_tb["C7"].data_type == "f"

        ws_unpaid = wb["UnpaidInvoices"]
        assert ws_unpaid["B2"].value == "'=cmd|' /C calc'!A0"
        assert ws_unpaid["B2"].data_type == "s"
        assert ws_unpaid["D2"].value == "2026-03-02"

        ws_je = wb["JournalEntries"]
        assert ws_je["H5"].value == "'\uff1dHYPERLINK(\"http://bad.test\")"
        assert ws_je["H5"].data_type == "s"
        assert ws_je["H6"].value == "'\uff1dHYPERLINK(\"http://bad.test\")"
        assert ws_je["H6"].data_type == "s"
    finally:
        wb.close()

    summary = query_duckdb_summary(custom_res)
    expected = {
        "matched_tx_count": 2,
        "total_gross_cents": 10_000,
        "total_net_cents": 12_200,
        "total_fee_cents": 300,
        "unpaid_invoice_count": 1,
        "unpaid_residual_cents": 5_000,
        "total_debit_cents": 12_500,
        "total_credit_cents": 12_500,
    }
    assert summary == expected
    assert all(type(v) is int for v in summary.values())


def test_anonymous_payer_ingested_and_classified_orphan() -> None:
    payload = _valid_bundle_payload(
        invoices=[
            {"id": "INV-1", "entity": "Acme LLC", "amount": "100.00", "date": "2026-03-01"}
        ],
        bank_txs=[
            {
                "id": "TX-ANON",
                "payer": "---",
                "net_amount": "55.00",
                "date": "2026-03-02",
                "memo": "ATM DEPOSIT",
            }
        ],
    )
    invs, txs, fee = ingest_json_bundle(payload)
    assert txs[0].norm_entity == ""
    res = reconcile(invs, txs, fee)
    assert len(res.matches) == 1
    assert res.matches[0].match_kind is MatchKind.ORPHAN_BANK_DEPOSIT


def test_format_money_and_demo_scenario_covers_all_passes() -> None:
    assert format_money(0) == "0.00 EGP"
    assert format_money(125_050, "USD") == "1,250.50 USD"
    assert format_money(-5, "EGP") == "-0.05 EGP"
    with pytest.raises(TypeError):
        format_money(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_money(100, "   ")

    invoices, bank_txs, fee = build_demo_scenario()
    res = reconcile(invoices, bank_txs, fee)
    assert verify_audit_chain(res.audit_chain)
    assert res.trial_balance.total_debit_cents == res.trial_balance.total_credit_cents

    by_tx = {m.tx_id: m for m in res.matches}
    assert by_tx["TX-2001"].match_kind is MatchKind.EXACT_REF
    assert by_tx["TX-2002"].match_kind is MatchKind.FEE_ADJUSTED_REF
    assert by_tx["TX-2003"].match_kind is MatchKind.ENTITY_EXACT
    assert by_tx["TX-2004"].match_kind is MatchKind.BATCH_SUBSET_SUM
    assert by_tx["TX-2004"].invoice_ids == ("INV-1004", "INV-1005", "INV-1006")
    assert by_tx["TX-2005"].match_kind is MatchKind.DUPLICATE_PAYMENT_ALERT
    assert by_tx["TX-2006"].match_kind is MatchKind.PARTIAL_PAYMENT
    assert by_tx["TX-2007"].match_kind is MatchKind.OVERPAYMENT_CREDIT
    assert by_tx["TX-2008"].match_kind is MatchKind.ORPHAN_BANK_DEPOSIT
    assert [(u.invoice_id, u.residual_cents) for u in res.unpaid_invoices] == [("INV-1007", 45_000)]

    report = render_reconciliation_report(res, currency="EGP")
    assert "[MATCHES]" in report
    assert "BALANCED" in report
    assert "PASS" in report


def test_cli_main_demo_bundle_xlsx_and_pep562(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert qistas.format_money is format_money
    assert qistas.build_demo_scenario is build_demo_scenario
    assert qistas.render_reconciliation_report is render_reconciliation_report
    assert qistas.main is main

    out_xlsx = tmp_path.joinpath("demo_report.xlsx")
    rc_demo = main(["--demo", "--export-xlsx", str(out_xlsx)])
    assert rc_demo == 0
    assert out_xlsx.exists()
    out_text = capsys.readouterr().out
    assert "QISTAS-CORE DETERMINISTIC RECONCILIATION REPORT" in out_text

    bundle_path = tmp_path.joinpath("bundle.json")
    bundle_path.write_text(
        _valid_bundle_payload(
            invoices=[
                {"id": "INV-9", "entity": "Oasis LLC", "amount": "250.00", "date": "2026-04-01"}
            ],
            bank_txs=[
                {"id": "TX-9", "payer": "Oasis LLC", "net_amount": "250.00", "date": "2026-04-02"}
            ],
        ),
        encoding="utf-8",
    )
    rc_bundle = main(["--bundle", str(bundle_path), "--currency", "USD"])
    assert rc_bundle == 0
    bundle_out = capsys.readouterr().out
    assert "250.00 USD" in bundle_out


def test_cli_subprocess_determinism_and_no_runpy_warning() -> None:
    outputs: list[str] = []
    for seed in ("1", "999"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "qistas.cli_visualizer", "--demo"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        assert proc.returncode == 0
        assert proc.stderr == ""
        assert "RuntimeWarning" not in proc.stderr
        assert "QISTAS-CORE DETERMINISTIC RECONCILIATION REPORT" in proc.stdout
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1]
