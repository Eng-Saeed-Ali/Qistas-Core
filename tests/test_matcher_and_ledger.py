from __future__ import annotations

import time
from dataclasses import replace

import pytest

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
from qistas.matcher import reconcile, solve_subset_sum_batch
from qistas.models import (
    GENESIS_HASH_U64,
    AccountCode,
    BankTx,
    FeeSchedule,
    Invoice,
    MatchKind,
    ReconciliationMatch,
    parse_iso_day,
)
from qistas.normalizer import normalize_entity


def _inv(
    inv_id: str,
    entity: str,
    amount_cents: int,
    iso_day: str,
    ref_code: str = "",
) -> Invoice:
    day = parse_iso_day(iso_day)
    return Invoice(inv_id, entity, normalize_entity(entity), amount_cents, day, ref_code)


def _tx(
    tx_id: str,
    payer: str,
    net_cents: int,
    iso_day: str,
    memo: str = "",
) -> BankTx:
    return BankTx(tx_id, payer, normalize_entity(payer), net_cents, parse_iso_day(iso_day), memo)


def test_expected_gateway_fee_and_fee_matches() -> None:
    zero = FeeSchedule()
    assert expected_gateway_fee(50_000, zero) == 0
    assert fee_matches(50_000, 50_000, zero) == (True, 0)
    assert fee_matches(50_000, 49_999, zero) == (False, 1)

    sched = FeeSchedule(
        fixed_fee_cents=100,
        rate_permille=25,
        min_fee_cents=300,
        max_fee_cents=1_500,
        tolerance_cents=5,
        max_settlement_lag_days=5,
    )
    assert expected_gateway_fee(1_000, sched) == 300
    assert expected_gateway_fee(10_001, sched) == 351
    assert expected_gateway_fee(100_000, sched) == 1_500

    assert fee_matches(10_001, 10_001, sched) == (True, 0)
    assert fee_matches(10_001, 10_001 - 356, sched) == (True, 5)
    assert fee_matches(10_001, 10_001 - 357, sched) == (False, 6)
    assert fee_matches(10_001, 10_002, sched) == (False, 1)
    assert fee_matches(10_001, 0, sched) == (False, 9_650)

    with pytest.raises(TypeError):
        expected_gateway_fee(True, sched)
    with pytest.raises(ValueError):
        expected_gateway_fee(-1, sched)


def test_all_match_kinds_journal_entries_and_trial_balance() -> None:
    d = parse_iso_day("2026-01-10")
    samples = [
        ReconciliationMatch("TX-1", MatchKind.EXACT_REF, ("INV-1",), 10_000, 10_000, 0, d),
        ReconciliationMatch("TX-2", MatchKind.FEE_ADJUSTED_REF, ("INV-2",), 10_000, 9_700, 300, d),
        ReconciliationMatch("TX-3", MatchKind.ENTITY_EXACT, ("INV-3",), 5_000, 5_000, 0, d),
        ReconciliationMatch(
            "TX-4", MatchKind.ENTITY_FEE_ADJUSTED, ("INV-4",), 5_000, 4_850, 150, d
        ),
        ReconciliationMatch(
            "TX-5", MatchKind.BATCH_SUBSET_SUM, ("INV-5", "INV-6"), 8_000, 7_800, 200, d
        ),
        ReconciliationMatch("TX-6", MatchKind.PARTIAL_PAYMENT, ("INV-7",), 3_000, 3_000, 0, d),
        ReconciliationMatch("TX-7", MatchKind.OVERPAYMENT_CREDIT, ("INV-8",), 4_000, 5_500, 0, d),
        ReconciliationMatch(
            "TX-8", MatchKind.DUPLICATE_PAYMENT_ALERT, ("INV-1",), 10_000, 10_000, 0, d
        ),
        ReconciliationMatch("TX-9", MatchKind.ORPHAN_BANK_DEPOSIT, (), 0, 2_000, 0, d),
    ]
    entries = tuple(build_journal_entry(f"E-{i}", m) for i, m in enumerate(samples, start=1))
    assert len(entries[0].postings) == 2
    assert len(entries[1].postings) == 3
    assert {p.account for p in entries[6].postings} == {
        AccountCode.BANK_CASH,
        AccountCode.AR_RECEIVABLE,
        AccountCode.CUSTOMER_CREDIT_LIABILITY,
    }
    assert {p.account for p in entries[7].postings} == {
        AccountCode.BANK_CASH,
        AccountCode.CUSTOMER_CREDIT_LIABILITY,
    }
    assert {p.account for p in entries[8].postings} == {
        AccountCode.BANK_CASH,
        AccountCode.SUSPENSE_CLEARING,
    }

    tb = compute_trial_balance(entries)
    assert isinstance(tb, TrialBalance)
    assert tb.total_debit_cents == tb.total_credit_cents == 58_500
    assert tuple(acct for acct, _ in tb.debits_by_account) == tuple(AccountCode)
    credit_map = dict(tb.credits_by_account)
    assert credit_map[AccountCode.SUSPENSE_CLEARING] == 2_000
    assert credit_map[AccountCode.CUSTOMER_CREDIT_LIABILITY] == 11_500

    with pytest.raises(ValueError):
        build_journal_entry("E-BAD", ReconciliationMatch(
            "TX-X", MatchKind.OVERPAYMENT_CREDIT, ("INV-1",), 4_000, 4_000, 0, d
        ))
    with pytest.raises(ValueError):
        TrialBalance(tb.debits_by_account, tb.credits_by_account, 10, 20)


def test_audit_chain_build_and_tamper_detection() -> None:
    d = parse_iso_day("2026-01-10")
    m0 = ReconciliationMatch("TX-1", MatchKind.EXACT_REF, ("INV-1",), 10_000, 10_000, 0, d)
    m1 = ReconciliationMatch("TX-2", MatchKind.ORPHAN_BANK_DEPOSIT, (), 0, 2_500, 0, d + 1)

    assert verify_audit_chain(()) is True
    chain = build_audit_chain((m0, m1))
    assert len(chain) == 2
    assert chain[0].prev_hash_u64 == GENESIS_HASH_U64
    assert chain[1].prev_hash_u64 == chain[0].chain_hash_u64
    assert chain[0].chain_hash_u64 == compute_audit_hash(0, m0, GENESIS_HASH_U64)
    assert verify_audit_chain(chain) is True

    assert verify_audit_chain((replace(chain[0], net_cents=9_999), chain[1])) is False
    assert verify_audit_chain((chain[1], chain[0])) is False
    bad_prev = replace(chain[1], prev_hash_u64=GENESIS_HASH_U64)
    assert verify_audit_chain((chain[0], bad_prev)) is False
    assert verify_audit_chain((replace(chain[0], chain_hash_u64=1), chain[1])) is False


def test_pass1_exact_ref_fee_adjusted_and_duplicate_alert() -> None:
    fee = FeeSchedule(rate_permille=20, tolerance_cents=2)
    invs = (
        _inv("INV-1001", "شركة النور", 10_000, "2026-01-10"),
        _inv("INV-1002", "Acme Corp", 20_000, "2026-01-10", ref_code="FAT-2026-05"),
    )
    txs = (
        _tx("TX-1", "Payer A", 10_000, "2026-01-11", "تحويل inv_\u0661\u0660\u0660\u0661"),
        _tx("TX-2", "Payer B", 19_600, "2026-01-12", "Ref FAT2026-05 fee"),
        _tx("TX-3", "Payer A", 10_000, "2026-01-16", "Duplicate INV-1001"),
    )
    res = reconcile(invs, txs, fee)
    assert [m.match_kind for m in res.matches] == [
        MatchKind.EXACT_REF,
        MatchKind.FEE_ADJUSTED_REF,
        MatchKind.DUPLICATE_PAYMENT_ALERT,
    ]
    assert res.matches[1].fee_cents == 400
    assert res.matches[2].invoice_ids == ("INV-1001",)
    assert res.unpaid_invoices == ()
    assert verify_audit_chain(res.audit_chain) is True
    assert res.trial_balance.total_debit_cents == res.trial_balance.total_credit_cents


def test_pass2_entity_match_weekend_lag_and_tie_break() -> None:
    fee = FeeSchedule(max_settlement_lag_days=5)
    invs = (
        _inv("INV-EARLY", "شركة \u0627\ufef7مل ذ.م.م.", 12_000, "2026-01-01"),
        _inv("INV-LATE", "مؤسسة الأمل", 12_000, "2026-01-03"),
        _inv("INV-EN", "Beta Holdings LLC", 9_000, "2026-01-01"),
        _inv("INV-EXPIRED", "Gamma Co", 7_000, "2026-01-01"),
    )
    txs = (
        _tx("TX-1", "شركة الأمل", 12_000, "2026-01-05"),
        _tx("TX-EN", "BETA HOLDINGS INC.", 9_000, "2026-01-06"),
        _tx("TX-2", "Gamma Limited", 7_000, "2026-01-07"),
    )
    res = reconcile(invs, txs, fee)
    assert res.matches[0].match_kind is MatchKind.ENTITY_EXACT
    assert res.matches[0].invoice_ids == ("INV-LATE",)
    assert res.matches[1].match_kind is MatchKind.ENTITY_EXACT
    assert res.matches[1].invoice_ids == ("INV-EN",)
    assert res.matches[2].match_kind is MatchKind.ORPHAN_BANK_DEPOSIT
    assert [u.invoice_id for u in res.unpaid_invoices] == ["INV-EARLY", "INV-EXPIRED"]


def test_pass3_subset_sum_2_3_4_and_fee_modes() -> None:
    invs = (
        _inv("INV-1", "Acme LLC", 1_000, "2026-02-01"),
        _inv("INV-2", "Acme LLC", 2_000, "2026-02-01"),
        _inv("INV-3", "Acme LLC", 4_000, "2026-02-02"),
        _inv("INV-4", "Acme LLC", 8_000, "2026-02-02"),
    )
    tx2 = _tx("TX-2INV", "Acme", 3_000, "2026-02-04")
    sub2, fee2, _, opt2 = solve_subset_sum_batch(invs, tx2, FeeSchedule())
    assert ([i.id for i in sub2], fee2, opt2) == (["INV-1", "INV-2"], 0, True)

    tx3 = _tx("TX-3INV", "Acme", 13_000, "2026-02-04")
    sub3, fee3, _, opt3 = solve_subset_sum_batch(invs, tx3, FeeSchedule())
    assert ([i.id for i in sub3], fee3, opt3) == (["INV-1", "INV-3", "INV-4"], 0, True)

    tx4 = _tx("TX-4INV", "Acme", 15_000, "2026-02-04")
    sub4, fee4, _, opt4 = solve_subset_sum_batch(invs, tx4, FeeSchedule())
    assert ([i.id for i in sub4], fee4, opt4) == (["INV-1", "INV-2", "INV-3", "INV-4"], 0, True)

    per_inv_fee = FeeSchedule(fixed_fee_cents=333, rate_permille=0, tolerance_cents=0)
    tx_per_inv = _tx("TX-PI", "Acme", 15_000 - 4 * 333, "2026-02-04")
    sub_pi, fee_pi, _, _ = solve_subset_sum_batch(invs, tx_per_inv, per_inv_fee)
    assert ([i.id for i in sub_pi], fee_pi) == (["INV-1", "INV-2", "INV-3", "INV-4"], 1_332)
    res_batch = reconcile(invs, (tx_per_inv,), per_inv_fee)
    assert res_batch.matches[0].match_kind is MatchKind.BATCH_SUBSET_SUM
    assert res_batch.matches[0].invoice_ids == ("INV-1", "INV-2", "INV-3", "INV-4")
    assert res_batch.unpaid_invoices == ()

    single_fee = FeeSchedule(fixed_fee_cents=0, rate_permille=20, min_fee_cents=250)
    tx_sb = _tx("TX-SB", "Acme", 12_000 - 250, "2026-02-04")
    sub_sb, fee_sb, _, _ = solve_subset_sum_batch(invs, tx_sb, single_fee)
    assert ([i.id for i in sub_sb], fee_sb) == (["INV-3", "INV-4"], 250)


def test_pass3_35_invoice_stress_pruning_and_truncation() -> None:
    invs = tuple(_inv(f"INV-{i:02d}", "Stress Co", 1_000 + i, "2026-03-01") for i in range(1, 33))
    targets = (
        _inv("INV-T1", "Stress Co", 50_000, "2026-03-01"),
        _inv("INV-T2", "Stress Co", 60_000, "2026-03-01"),
        _inv("INV-T3", "Stress Co", 70_000, "2026-03-01"),
    )
    tx = _tx("TX-STRESS", "Stress Co", 180_000, "2026-03-02")
    t0 = time.perf_counter_ns()
    sub, fee_cents, exp_used, opt = solve_subset_sum_batch(invs + targets, tx, FeeSchedule())
    elapsed_ns = time.perf_counter_ns() - t0
    assert ([i.id for i in sub], fee_cents, opt) == (["INV-T1", "INV-T2", "INV-T3"], 0, True)
    assert exp_used < 50
    assert elapsed_ns < 20_000_000

    res_trunc = reconcile(invs + targets, (tx,), FeeSchedule(), max_expansions=2)
    assert res_trunc.expansions == 2
    assert res_trunc.proven_optimal_batches is False


def test_pass4_sequential_partials_overpayment_and_orphan() -> None:
    invs = (
        _inv("INV-P1", "Delta Corp", 10_000, "2026-04-01"),
        _inv("INV-P2", "Echo Corp", 10_000, "2026-04-01"),
        _inv("INV-AMB1", "Ambig Co", 5_000, "2026-04-01"),
        _inv("INV-AMB2", "Ambig Co", 6_000, "2026-04-01"),
    )
    txs = (
        _tx("TX-1", "Delta", 4_000, "2026-04-02", "Pay INV-P1"),
        _tx("TX-2", "Delta", 3_000, "2026-04-03", "Pay INV-P1"),
        _tx("TX-3", "Echo", 4_000, "2026-04-02", "Pay INV-P2"),
        _tx("TX-4", "Echo", 8_000, "2026-04-03", "Pay INV-P2"),
        _tx("TX-5", "Ambig Co", 2_000, "2026-04-03"),
    )
    res = reconcile(invs, txs, FeeSchedule())
    kinds = {m.tx_id: m.match_kind for m in res.matches}
    assert kinds == {
        "TX-1": MatchKind.PARTIAL_PAYMENT,
        "TX-2": MatchKind.PARTIAL_PAYMENT,
        "TX-3": MatchKind.PARTIAL_PAYMENT,
        "TX-4": MatchKind.OVERPAYMENT_CREDIT,
        "TX-5": MatchKind.ORPHAN_BANK_DEPOSIT,
    }
    by_id = {m.tx_id: m for m in res.matches}
    assert by_id["TX-4"].gross_cents == 6_000
    assert by_id["TX-4"].net_cents == 8_000
    assert [(u.invoice_id, u.residual_cents) for u in res.unpaid_invoices] == [
        ("INV-AMB1", 5_000),
        ("INV-AMB2", 6_000),
        ("INV-P1", 3_000),
    ]
    assert [e.entry_id for e in res.journal_entries] == [
        "ENTRY-0001", "ENTRY-0002", "ENTRY-0003", "ENTRY-0004", "ENTRY-0005"
    ]
    assert res.trial_balance.total_debit_cents == res.trial_balance.total_credit_cents == 21_000
    assert verify_audit_chain(res.audit_chain) is True
