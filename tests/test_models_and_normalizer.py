from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from qistas.models import (
    GENESIS_HASH_U64,
    MAX_AMOUNT_CENTS,
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


def _sample_postings() -> tuple[LedgerPosting, LedgerPosting]:
    return (
        LedgerPosting(AccountCode.BANK_CASH, 10_000, 0, "TX-1"),
        LedgerPosting(AccountCode.AR_RECEIVABLE, 0, 10_000, "INV-1"),
    )


def _sample_dataclasses() -> tuple[object, ...]:
    p1, p2 = _sample_postings()
    return (
        FeeSchedule(),
        Invoice("INV-1", "شركة النور", "النور", 10_000, 739_617, "INV-1"),
        BankTx("TX-1", "Al Noor LLC", "al noor", 10_000, 739_618, "INV-1"),
        p1,
        JournalEntry("ENTRY-1", 739_618, MatchKind.EXACT_REF, (p1, p2), "settled"),
        ReconciliationMatch("TX-1", MatchKind.EXACT_REF, ("INV-1",), 10_000, 10_000, 0, 739_618),
        UnpaidInvoiceRecord("INV-2", 5_000, 739_617, "acme"),
        AuditLogEntry(
            0, MatchKind.EXACT_REF, "TX-1", ("INV-1",), 10_000, 10_000, 0, 739_618,
            GENESIS_HASH_U64, 12345,
        ),
    )


def test_ceil_div_exact_and_boundaries() -> None:
    assert ceil_div(0, 1) == 0
    assert ceil_div(0, 1000) == 0
    assert ceil_div(1, 1000) == 1
    assert ceil_div(1000, 1000) == 1
    assert ceil_div(1001, 1000) == 2
    assert ceil_div(2500, 1000) == 3

    for bad_n, bad_d in [(True, 2), (2, True), (False, 1), (-1, 10), (10, 0), (10, -2)]:
        with pytest.raises((TypeError, ValueError)):
            ceil_div(bad_n, bad_d)


@pytest.mark.parametrize(
    "n", [0, 1, 2, 7, 999, 1000, 1001, 5_000_003, 9_999_999]
)
@pytest.mark.parametrize("d", [1, 2, 3, 7, 1000, 9999])
def test_ceil_div_property(n: int, d: int) -> None:
    res = ceil_div(n, d)
    assert type(res) is int
    if n == 0:
        assert res == 0
    else:
        assert (res - 1) * d < n <= res * d


def test_iso_day_helpers_and_rejections() -> None:
    day = parse_iso_day("2026-01-15")
    assert format_iso_day(day) == "2026-01-15"
    assert format_iso_day(parse_iso_day("2024-02-29")) == "2024-02-29"

    for bad_s in ["2026-1-15", "2026-02-29", "2026-01-15\n", "not-a-date", "20260115"]:
        with pytest.raises(ValueError):
            parse_iso_day(bad_s)
    with pytest.raises((TypeError, ValueError)):
        parse_iso_day(True)  # type: ignore[arg-type]

    for bad_ord in [True, False, 0, -1, 3_652_060]:
        with pytest.raises((TypeError, ValueError)):
            format_iso_day(bad_ord)


@pytest.mark.parametrize(
    "day", [1, 2, 365, 366, 719163, 738000, 739616, 739617, 1_000_000, 3_652_058, 3_652_059]
)
def test_iso_day_roundtrip_property(day: int) -> None:
    assert parse_iso_day(format_iso_day(day)) == day


def test_all_dataclasses_frozen_and_slotted() -> None:
    objs = _sample_dataclasses()
    assert len(objs) == 8
    attr_targets = [
        ("rate_permille", 10),
        ("amount_cents", 99),
        ("net_cents", 99),
        ("debit_cents", 1),
        ("memo", "x"),
        ("fee_cents", 1),
        ("residual_cents", 1),
        ("seq_no", 1),
    ]
    for obj, (attr, val) in zip(objs, attr_targets, strict=True):
        assert not hasattr(obj, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, val)


def test_f1_boundary_hygiene_rejects_bool_nul_and_bad_ids() -> None:
    with pytest.raises(TypeError):
        FeeSchedule(rate_permille=True)
    with pytest.raises(TypeError):
        Invoice("INV-1", "Acme", "acme", True, 739_617)
    with pytest.raises(TypeError):
        Invoice("INV-1", "Acme", "acme", 100, False)
    with pytest.raises(TypeError):
        BankTx("TX-1", "Acme", "acme", True, 739_617)
    with pytest.raises(TypeError):
        LedgerPosting(AccountCode.BANK_CASH, True, 0, "TX-1")
    with pytest.raises(TypeError):
        AuditLogEntry(
            False, MatchKind.EXACT_REF, "TX-1", ("INV-1",), 100, 100, 0, 739_617, 1, 2
        )

    with pytest.raises(ValueError):
        Invoice("INV-1", "Acme\x00", "acme", 100, 739_617)
    with pytest.raises(ValueError):
        Invoice("INV-1", "   ", "acme", 100, 739_617)
    with pytest.raises(ValueError):
        BankTx("TX-1", "Acme", "acme", 100, 739_617, "memo\x00")
    with pytest.raises(ValueError):
        JournalEntry("ENTRY-1", 739_617, MatchKind.EXACT_REF, _sample_postings(), "bad\x00")
    with pytest.raises(ValueError):
        UnpaidInvoiceRecord("INV-1", 100, 739_617, "acme\x00")

    for bad_id in ["", "inv-1", "INV 1", "INV-1\n", "A" * 33, "INV!1"]:
        with pytest.raises(ValueError):
            Invoice(bad_id, "Acme", "acme", 100, 739_617)
        if bad_id != "":
            with pytest.raises(ValueError):
                Invoice("INV-1", "Acme", "acme", 100, 739_617, ref_code=bad_id)
        with pytest.raises(ValueError):
            BankTx(bad_id, "Acme", "acme", 100, 739_617)
        with pytest.raises(ValueError):
            ReconciliationMatch("TX-1", MatchKind.EXACT_REF, (bad_id,), 100, 100, 0, 739_617)
    assert Invoice("INV-1", "Acme", "acme", 100, 739_617, ref_code="").ref_code == ""


def test_f1_domain_value_invariants() -> None:
    assert FeeSchedule(min_fee_cents=200, max_fee_cents=0).min_fee_cents == 200
    with pytest.raises(ValueError):
        FeeSchedule(rate_permille=MAX_RATE_PERMILLE + 1)
    with pytest.raises(ValueError):
        FeeSchedule(max_settlement_lag_days=MAX_LAG_DAYS_LIMIT + 1)
    with pytest.raises(ValueError):
        FeeSchedule(min_fee_cents=200, max_fee_cents=100)
    with pytest.raises(ValueError):
        Invoice("INV-1", "Acme", "acme", 0, 739_617)
    with pytest.raises(ValueError):
        Invoice("INV-1", "Acme", "acme", MAX_AMOUNT_CENTS + 1, 739_617)
    with pytest.raises(ValueError):
        BankTx("TX-1", "Acme", "acme", 0, 739_617)

    with pytest.raises(ValueError):
        LedgerPosting(AccountCode.BANK_CASH, 0, 0, "TX-1")
    with pytest.raises(ValueError):
        LedgerPosting(AccountCode.BANK_CASH, 100, 100, "TX-1")
    with pytest.raises(ValueError):
        LedgerPosting(AccountCode.BANK_CASH, -10, 0, "TX-1")

    p1 = LedgerPosting(AccountCode.BANK_CASH, 100, 0, "TX-1")
    p2 = LedgerPosting(AccountCode.AR_RECEIVABLE, 0, 99, "INV-1")
    with pytest.raises(ValueError):
        JournalEntry("ENTRY-1", 739_617, MatchKind.EXACT_REF, (p1, p2))
    with pytest.raises(ValueError):
        JournalEntry("ENTRY-1", 739_617, MatchKind.EXACT_REF, (p1,))
    with pytest.raises((TypeError, ValueError)):
        JournalEntry("ENTRY-1", 739_617, MatchKind.EXACT_REF, [p1, p2])  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        AuditLogEntry(
            0, MatchKind.EXACT_REF, "TX-1", ("INV-1",), 100, 100, 0, 739_617, 0, 1 << 64
        )
    with pytest.raises(ValueError):
        AuditLogEntry(
            -1, MatchKind.EXACT_REF, "TX-1", ("INV-1",), 100, 100, 0, 739_617, 0, 1
        )


def test_f2_arabic_ligatures_bidi_harakat_tatweel_and_digits() -> None:
    assert normalize_entity("شركة \u0627\ufef7مل") == normalize_entity("شركة الأمل") == "الامل"
    assert normalize_entity("شركة \u0627\ufefbمل") == "الامل"
    assert normalize_text("س\ufefbم") == "سلام"
    assert normalize_text("\u200fشَرِكَةُ\u200e ال\u202aنـــوُر\u202c") == "شركه النور"
    assert normalize_text("\ufeffأإآٱ ىئیک ة ؤ") == "\u0627\u0627\u0627\u0627 يييك \u0647 و"
    assert normalize_text("فاتورة ١٢٣ و ۴۵۶ ۰۹") == "فاتوره 123 و 456 09"
    assert normalize_entity("شركة الأمل للتجارة ذ.م.م.") == "الامل للتجاره"
    assert normalize_entity("Acme Holdings LLC") == "acme holdings"
    assert normalize_entity("ش.م.م النور") == "النور"

    with pytest.raises(ValueError):
        normalize_text("bad\x00text")
    with pytest.raises(ValueError):
        extract_ref_codes("bad\x00memo")


def test_ec_a_stopword_only_entities_and_ref_extraction() -> None:
    assert normalize_entity("شركة ذ.م.م") != ""
    assert normalize_entity("LLC Co") != ""
    assert normalize_entity("شركة") == "شركه"
    assert normalize_entity("") == ""

    memo_ref = "تحويل فاتورة رقم inv_\u0661\u0660\u0660\u0661 و FAT2026-05 و inv-1001"
    assert extract_ref_codes(memo_ref) == ("INV-1001", "FAT-2026-05")
    assert extract_ref_codes("Pay \u200fQST_99 and REF-2026_A1; bill_42") == (
        "QST-99",
        "REF-2026-A1",
        "BILL-42",
    )


@pytest.mark.parametrize(
    "s",
    [
        "",
        "   ",
        "Acme Corp. LLC",
        "شركة \u0627\ufef7مل للتجارة ذ.م.م",
        "مؤسسة النّور — ١٢٣٤٥",
        "کابل پی یوزر ۰۹۸۷",
        "\u200fINV-2026-0042\u200e",
        "شركة ذ.م.م",
        "LLC Co",
        "___---...",
    ],
)
def test_normalizer_idempotence_property(s: str) -> None:
    t1 = normalize_text(s)
    assert normalize_text(t1) == t1
    e1 = normalize_entity(s)
    assert normalize_entity(e1) == e1
