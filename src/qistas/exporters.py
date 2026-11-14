from __future__ import annotations

import os
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import duckdb
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from qistas.matcher import ReconciliationResult
from qistas.models import AccountCode, format_iso_day

_FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "|", "%", "\t", "\r", "\n")
_HDR_FILL = PatternFill(fill_type="solid", fgColor="0F172A")
_HDR_FONT = Font(name="Consolas", size=11, bold=True, color="FFFFFF")
_HDR_ALIGN = Alignment(horizontal="center", vertical="center")
_TOT_FILL = PatternFill(fill_type="solid", fgColor="D1FAE5")
_TOT_FONT = Font(name="Consolas", size=11, bold=True, color="065F46")
_BODY_FONT = Font(name="Consolas", size=10, bold=False, color="0F172A")
_THIN_SIDE = Side(style="thin", color="CBD5E1")
_CELL_BORDER = Border(
    left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE
)


def neutralize_formula_cell(val: str) -> str:
    if type(val) is not str:
        raise TypeError("cell value must be str")
    assert type(val) is str
    norm_s = unicodedata.normalize("NFKC", val).lstrip()
    if val.startswith(_FORMULA_TRIGGERS) or norm_s.startswith(_FORMULA_TRIGGERS):
        out = "'" + val
    else:
        out = val
    assert type(out) is str
    return out


def _style_worksheet(ws: Worksheet, has_total_row: bool = False) -> None:
    max_row = ws.max_row
    max_col = ws.max_column
    assert max_row >= 1 and max_col >= 1
    for col_idx in range(1, max_col + 1):
        max_len = 10
        for row_idx in range(1, max_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = _CELL_BORDER
            val = cell.value
            if type(val) is int or cell.data_type == "f":
                cell.number_format = "#,##0"
            if row_idx == 1:
                cell.fill = _HDR_FILL
                cell.font = _HDR_FONT
                cell.alignment = _HDR_ALIGN
            elif has_total_row and row_idx == max_row:
                cell.fill = _TOT_FILL
                cell.font = _TOT_FONT
            else:
                cell.font = _BODY_FONT
            text_len = len(str(val)) if val is not None else 0
            if text_len > max_len:
                max_len = text_len
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = max_len + 5
    assert ws.max_column == max_col


def _write_sheet(
    wb: openpyxl.Workbook,
    title: str,
    rows: Sequence[Sequence[str | int]],
) -> Worksheet:
    assert len(rows) >= 1
    ws = wb.create_sheet(title=title)
    for row_idx, row_vals in enumerate(rows, start=1):
        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if type(val) is str:
                cell.value = neutralize_formula_cell(val)
                cell.data_type = "s"
            elif type(val) is int:
                cell.value = val
            else:
                raise TypeError("cell value must be str or int")
    assert ws.title == title
    return ws


def _append_matches_total_row(ws: Worksheet, last_row: int) -> None:
    assert last_row >= 1
    end_row = max(2, last_row)
    total_row = last_row + 1
    c_lbl = ws.cell(row=total_row, column=1)
    c_lbl.value = "TOTAL"
    c_lbl.data_type = "s"
    for col_idx in (2, 3, 7):
        c_empty = ws.cell(row=total_row, column=col_idx)
        c_empty.value = "—"
        c_empty.data_type = "s"
    for col_idx, col_let in ((4, "D"), (5, "E"), (6, "F")):
        c_sum = ws.cell(row=total_row, column=col_idx)
        c_sum.value = f"=SUM({col_let}2:{col_let}{end_row})"
        c_sum.data_type = "f"
    assert ws.max_row == total_row


def _populate_matches_journal_unpaid(wb: openpyxl.Workbook, result: ReconciliationResult) -> None:
    assert isinstance(result, ReconciliationResult)
    match_rows: list[tuple[str | int, ...]] = [
        ("tx_id", "match_kind", "invoice_ids", "gross_cents", "net_cents", "fee_cents", "date")
    ]
    for m in result.matches:
        inv_text = ",".join(m.invoice_ids) if m.invoice_ids else "N/A"
        match_rows.append((
            m.tx_id, m.match_kind.value, inv_text,
            m.gross_cents, m.net_cents, m.fee_cents, format_iso_day(m.day_ordinal),
        ))
    je_rows: list[tuple[str | int, ...]] = [
        (
            "entry_id", "date", "match_kind", "account",
            "debit_cents", "credit_cents", "ref_id", "memo",
        )
    ]
    for je in result.journal_entries:
        iso_dt = format_iso_day(je.day_ordinal)
        memo_text = je.memo if je.memo else "N/A"
        for p in je.postings:
            je_rows.append((
                je.entry_id, iso_dt, je.match_kind.value,
                p.account.value, p.debit_cents, p.credit_cents, p.ref_id, memo_text,
            ))
    unpaid_rows: list[tuple[str | int, ...]] = [
        ("invoice_id", "norm_entity", "residual_cents", "date")
    ]
    for u in result.unpaid_invoices:
        unpaid_rows.append(
            (u.invoice_id, u.norm_entity, u.residual_cents, format_iso_day(u.day_ordinal))
        )
    ws_matches = _write_sheet(wb, "Matches", match_rows)
    _append_matches_total_row(ws_matches, len(match_rows))
    _style_worksheet(ws_matches, has_total_row=True)
    ws_je = _write_sheet(wb, "JournalEntries", je_rows)
    _style_worksheet(ws_je, has_total_row=False)
    _populate_trial_balance(wb, result)
    ws_unpaid = _write_sheet(wb, "UnpaidInvoices", unpaid_rows)
    _style_worksheet(ws_unpaid, has_total_row=False)
    assert len(wb.sheetnames) >= 4


def _populate_trial_balance(wb: openpyxl.Workbook, result: ReconciliationResult) -> None:
    tb = result.trial_balance
    assert len(tb.debits_by_account) == len(AccountCode)
    rows: list[tuple[str | int, ...]] = [("account", "debit_cents", "credit_cents")]
    for (d_acct, d_amt), (c_acct, c_amt) in zip(
        tb.debits_by_account, tb.credits_by_account, strict=True
    ):
        assert d_acct == c_acct
        rows.append((d_acct.value, d_amt, c_amt))
    ws = _write_sheet(wb, "TrialBalance", rows)
    last_acct_row = len(rows)
    total_row = last_acct_row + 1
    c_lbl = ws.cell(row=total_row, column=1)
    c_lbl.value = "TOTAL"
    c_lbl.data_type = "s"
    c_dr = ws.cell(row=total_row, column=2)
    c_dr.value = f"=SUM(B2:B{last_acct_row})"
    c_dr.data_type = "f"
    c_cr = ws.cell(row=total_row, column=3)
    c_cr.value = f"=SUM(C2:C{last_acct_row})"
    c_cr.data_type = "f"
    _style_worksheet(ws, has_total_row=True)


def export_xlsx_atomic(result: ReconciliationResult, target_path: str | os.PathLike[str]) -> None:
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be ReconciliationResult")
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".tmp_{target.name}.{os.getpid()}")
    assert target != tmp_path
    wb = openpyxl.Workbook()
    try:
        try:
            _populate_matches_journal_unpaid(wb, result)
            if "Sheet" in wb.sheetnames:
                del wb["Sheet"]
            wb.save(tmp_path)
        finally:
            wb.close()
        fd = os.open(tmp_path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    assert target.exists()


def _load_duckdb_tables(con: duckdb.DuckDBPyConnection, result: ReconciliationResult) -> None:
    con.execute(
        "CREATE TABLE reconciliation_matches ("
        "tx_id VARCHAR, match_kind VARCHAR, gross_cents BIGINT, "
        "net_cents BIGINT, fee_cents BIGINT, day_ordinal BIGINT)"
    )
    con.execute(
        "CREATE TABLE journal_postings ("
        "entry_id VARCHAR, account VARCHAR, debit_cents BIGINT, "
        "credit_cents BIGINT, ref_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE unpaid_invoices ("
        "invoice_id VARCHAR, residual_cents BIGINT, day_ordinal BIGINT, norm_entity VARCHAR)"
    )
    match_rows = [
        (m.tx_id, m.match_kind.value, m.gross_cents, m.net_cents, m.fee_cents, m.day_ordinal)
        for m in result.matches
    ]
    posting_rows = [
        (je.entry_id, p.account.value, p.debit_cents, p.credit_cents, p.ref_id)
        for je in result.journal_entries
        for p in je.postings
    ]
    unpaid_rows = [
        (u.invoice_id, u.residual_cents, u.day_ordinal, u.norm_entity)
        for u in result.unpaid_invoices
    ]
    if match_rows:
        con.executemany("INSERT INTO reconciliation_matches VALUES (?, ?, ?, ?, ?, ?)", match_rows)
    if posting_rows:
        con.executemany("INSERT INTO journal_postings VALUES (?, ?, ?, ?, ?)", posting_rows)
    if unpaid_rows:
        con.executemany("INSERT INTO unpaid_invoices VALUES (?, ?, ?, ?)", unpaid_rows)
    con.execute(
        "CREATE VIEW v_trial_balance AS "
        "SELECT account, CAST(COALESCE(SUM(debit_cents), 0) AS BIGINT) AS debit_cents, "
        "CAST(COALESCE(SUM(credit_cents), 0) AS BIGINT) AS credit_cents "
        "FROM journal_postings GROUP BY account"
    )
    assert len(match_rows) == len(result.matches)
    assert len(unpaid_rows) == len(result.unpaid_invoices)


def query_duckdb_summary(result: ReconciliationResult) -> dict[str, int]:
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be ReconciliationResult")
    con = duckdb.connect(":memory:")
    try:
        _load_duckdb_tables(con, result)
        m_row = con.execute(
            "SELECT COUNT(*), CAST(COALESCE(SUM(gross_cents), 0) AS BIGINT), "
            "CAST(COALESCE(SUM(net_cents), 0) AS BIGINT), "
            "CAST(COALESCE(SUM(fee_cents), 0) AS BIGINT) FROM reconciliation_matches"
        ).fetchone()
        u_row = con.execute(
            "SELECT COUNT(*), CAST(COALESCE(SUM(residual_cents), 0) AS BIGINT) "
            "FROM unpaid_invoices"
        ).fetchone()
        t_row = con.execute(
            "SELECT CAST(COALESCE(SUM(debit_cents), 0) AS BIGINT), "
            "CAST(COALESCE(SUM(credit_cents), 0) AS BIGINT) FROM v_trial_balance"
        ).fetchone()
    finally:
        con.close()
    assert m_row is not None and u_row is not None and t_row is not None
    summary: dict[str, int] = {
        "matched_tx_count": int(m_row[0]),
        "total_gross_cents": int(m_row[1]),
        "total_net_cents": int(m_row[2]),
        "total_fee_cents": int(m_row[3]),
        "unpaid_invoice_count": int(u_row[0]),
        "unpaid_residual_cents": int(u_row[1]),
        "total_debit_cents": int(t_row[0]),
        "total_credit_cents": int(t_row[1]),
    }
    assert summary["total_debit_cents"] == summary["total_credit_cents"]
    return summary
