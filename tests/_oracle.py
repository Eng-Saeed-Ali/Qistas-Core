from __future__ import annotations

import itertools
import random
from collections.abc import Sequence

from qistas.ledger import (
    compute_trial_balance,
    expected_gateway_fee,
    fee_matches,
    verify_audit_chain,
)
from qistas.matcher import ReconciliationResult
from qistas.models import (
    MAX_BATCH_SIZE,
    BankTx,
    FeeSchedule,
    Invoice,
    MatchKind,
    parse_iso_day,
)
from qistas.normalizer import normalize_entity

_EASTERN_TRANS = str.maketrans(
    "0123456789",
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
)

_GROUP_A_PAIRS: tuple[tuple[str, str], ...] = (
    ("Delta Logistics LLC", "Delta Logistics"),
    ("شركة الدلتا لوجستيكس", "\u200fشركة الدلتا لوجستيكس"),
)
_GROUP_B_PAIRS: tuple[tuple[str, str], ...] = (
    ("شركة الأهرام للتوريدات", "\u200fشركة \u0627\ufef7\u0647\u0631\u0627\u0645 للتوريدات"),
    ("شركة الأهرام للتوريدات Ltd", "الاهرام للتوريدات"),
)
_GROUP_C_PAIRS: tuple[tuple[str, str], ...] = (
    ("مؤسسة السلام للمقاولات", "\u200fمؤسسة \u0627\u0644\u0633\ufefb\u0645 للمقاولات"),
    ("Nile Contractors Co", "Nile Contractors LLC"),
)
_GROUP_D_PAIRS: tuple[tuple[str, str], ...] = (
    ("Giza Trading Co", "Giza Trading LLC"),
    ("Sinai Imports Inc", "Sinai Imports Co"),
)


def brute_force_subset_sum(
    candidates: Sequence[Invoice],
    tx: BankTx,
    fee: FeeSchedule,
) -> tuple[tuple[Invoice, ...], int]:
    eligible = [
        inv for inv in candidates
        if 0 <= tx.day_ordinal - inv.day_ordinal <= fee.max_settlement_lag_days
    ]
    best_subset: tuple[Invoice, ...] = ()
    best_rank: tuple[int, int, tuple[str, ...]] | None = None
    best_gross = 0
    upper_k = min(MAX_BATCH_SIZE, len(eligible))
    for k in range(2, upper_k + 1):
        if k not in (2, 3, 4):
            continue
        for subset in itertools.combinations(eligible, k):
            gross = sum(inv.amount_cents for inv in subset)
            actual_fee = gross - tx.net_cents
            if actual_fee < 0 or actual_fee > gross:
                continue
            sb_ok, sb_err = fee_matches(gross, tx.net_cents, fee)
            exp_sum = sum(expected_gateway_fee(inv.amount_cents, fee) for inv in subset)
            pi_err = 0 if actual_fee == 0 else abs(actual_fee - exp_sum)
            pi_ok = actual_fee == 0 or pi_err <= fee.tolerance_cents
            errs = [err for ok, err in ((sb_ok, sb_err), (pi_ok, pi_err)) if ok]
            if not errs:
                continue
            fee_err = min(errs)
            rank = (fee_err, len(subset), tuple(sorted(inv.id for inv in subset)))
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_subset = subset
                best_gross = gross
    if not best_subset:
        return (), 0
    ordered = tuple(sorted(best_subset, key=lambda inv: (inv.day_ordinal, inv.id)))
    return ordered, best_gross - tx.net_cents


def verify_reconciliation_result(
    invoices: Sequence[Invoice],
    bank_txs: Sequence[BankTx],
    fee: FeeSchedule,
    result: ReconciliationResult,
) -> None:
    expected_tx_ids = [
        tx.id for tx in sorted(bank_txs, key=lambda item: (item.day_ordinal, item.id))
    ]
    assert [m.tx_id for m in result.matches] == expected_tx_ids
    assert len(result.journal_entries) == len(result.matches)
    assert len(result.audit_chain) == len(result.matches)
    assert verify_audit_chain(result.audit_chain)

    for entry in result.journal_entries:
        debit_sum = sum(p.debit_cents for p in entry.postings)
        credit_sum = sum(p.credit_cents for p in entry.postings)
        assert debit_sum == credit_sum and debit_sum > 0

    recomputed_tb = compute_trial_balance(result.journal_entries)
    assert recomputed_tb == result.trial_balance
    assert result.trial_balance.total_debit_cents == result.trial_balance.total_credit_cents

    inv_by_id = {inv.id: inv for inv in invoices}
    credited_by_inv: dict[str, int] = {inv.id: 0 for inv in invoices}
    for m in result.matches:
        assert m.fee_cents >= 0
        if m.match_kind in (MatchKind.DUPLICATE_PAYMENT_ALERT, MatchKind.ORPHAN_BANK_DEPOSIT):
            assert m.fee_cents == 0
            continue
        if len(m.invoice_ids) == 1:
            assert m.gross_cents > 0
            credited_by_inv[m.invoice_ids[0]] += m.gross_cents
        else:
            assert len(m.invoice_ids) >= 2
            batch_sum = sum(inv_by_id[iid].amount_cents for iid in m.invoice_ids)
            assert batch_sum == m.gross_cents
            for iid in m.invoice_ids:
                credited_by_inv[iid] += inv_by_id[iid].amount_cents

    residual_by_inv = {rec.invoice_id: rec.residual_cents for rec in result.unpaid_invoices}
    assert len(residual_by_inv) == len(result.unpaid_invoices)
    for inv in invoices:
        assert credited_by_inv[inv.id] + residual_by_inv.get(inv.id, 0) == inv.amount_cents


def _add_group_a_b_records(
    rng: random.Random,
    idx: int,
    base_day: int,
    fee: FeeSchedule,
    invoices: list[Invoice],
    txs: list[BankTx],
) -> None:
    inv_ent_a, tx_ent_a = _GROUP_A_PAIRS[rng.randrange(len(_GROUP_A_PAIRS))]
    amt_a = rng.randint(40_000, 120_000)
    ref_num = 1000 + idx
    ref_code = f"INV-{ref_num}"
    invoices.append(
        Invoice(f"A-{idx}-1", inv_ent_a, normalize_entity(inv_ent_a), amt_a, base_day, ref_code)
    )
    net_a = amt_a - expected_gateway_fee(amt_a, fee) if rng.randint(0, 1) else amt_a
    digits = str(ref_num).translate(_EASTERN_TRANS) if rng.randint(0, 1) else str(ref_num)
    txs.append(
        BankTx(
            f"TXA-{idx}-1",
            tx_ent_a,
            normalize_entity(tx_ent_a),
            net_a,
            base_day + rng.randint(0, 2),
            f"سداد inv_{digits}",
        )
    )
    if rng.randint(1, 10) <= 3:
        txs.append(
            BankTx(
                f"TXA-{idx}-2",
                tx_ent_a,
                normalize_entity(tx_ent_a),
                amt_a,
                base_day + 3,
                f"DUP INV-{ref_num}",
            )
        )

    inv_ent_b, tx_ent_b = _GROUP_B_PAIRS[rng.randrange(len(_GROUP_B_PAIRS))]
    amt_b = rng.randint(50_000, 150_000)
    invoices.append(Invoice(f"B-{idx}-1", inv_ent_b, normalize_entity(inv_ent_b), amt_b, base_day))
    if rng.randint(1, 10) <= 8:
        fee_b = expected_gateway_fee(amt_b, fee)
        delta_b = rng.randint(-(fee.tolerance_cents // 2), fee.tolerance_cents // 2)
        actual_fee_b = max(1, fee_b + delta_b)
    else:
        actual_fee_b = 0
    txs.append(
        BankTx(
            f"TXB-{idx}-1",
            tx_ent_b,
            normalize_entity(tx_ent_b),
            amt_b - actual_fee_b,
            base_day + rng.randint(0, fee.max_settlement_lag_days),
            "wire transfer",
        )
    )


def _add_group_c_d_e_records(
    rng: random.Random,
    idx: int,
    base_day: int,
    fee: FeeSchedule,
    invoices: list[Invoice],
    txs: list[BankTx],
) -> None:
    inv_ent_c, tx_ent_c = _GROUP_C_PAIRS[rng.randrange(len(_GROUP_C_PAIRS))]
    base_amts = [20_000, 35_000, 52_000, 74_000]
    n_batch = rng.randint(3, 4)
    batch_invs: list[Invoice] = []
    for j in range(n_batch):
        amt = base_amts[j] + rng.randint(1, 9) * 100
        inv = Invoice(f"C-{idx}-{j}", inv_ent_c, normalize_entity(inv_ent_c), amt, base_day + j)
        batch_invs.append(inv)
        invoices.append(inv)
    chosen = sorted(rng.sample(range(n_batch), rng.randint(2, n_batch)))
    batch_gross = sum(batch_invs[pos].amount_cents for pos in chosen)
    fee_mode = rng.randrange(3)
    half_tol = fee.tolerance_cents // 2
    if fee_mode == 0:
        net_c = batch_gross
    elif fee_mode == 1:
        single_fee = expected_gateway_fee(batch_gross, fee)
        delta_c = rng.randint(-half_tol, half_tol)
        net_c = batch_gross - max(0, single_fee + delta_c)
    else:
        per_inv_fee = sum(expected_gateway_fee(batch_invs[pos].amount_cents, fee) for pos in chosen)
        delta_c = rng.randint(-half_tol, half_tol)
        net_c = batch_gross - max(0, per_inv_fee + delta_c)
    txs.append(
        BankTx(
            f"TXC-{idx}-1",
            tx_ent_c,
            normalize_entity(tx_ent_c),
            net_c,
            base_day + fee.max_settlement_lag_days,
            "batch payout",
        )
    )

    inv_ent_d, tx_ent_d = _GROUP_D_PAIRS[rng.randrange(len(_GROUP_D_PAIRS))]
    amt_d = rng.randint(70_000, 130_000)
    ref_d = f"REF-D{idx}"
    invoices.append(
        Invoice(f"D-{idx}-1", inv_ent_d, normalize_entity(inv_ent_d), amt_d, base_day + 1, ref_d)
    )
    is_partial = rng.randint(1, 10) <= 6
    net_d = amt_d // 2 if is_partial else amt_d + rng.randint(3_000, 12_000)
    txs.append(
        BankTx(
            f"TXD-{idx}-1",
            tx_ent_d,
            normalize_entity(tx_ent_d),
            net_d,
            base_day + 2,
            f"settling {ref_d}",
        )
    )

    if rng.randint(0, 1) == 1:
        raw_orphan = ("---", "UNKNOWN ATM", "***")[rng.randrange(3)]
        txs.append(
            BankTx(
                f"TXE-{idx}-1",
                raw_orphan,
                normalize_entity(raw_orphan),
                rng.randint(5_000, 25_000),
                base_day + 2,
                "ATM CASH",
            )
        )


def generate_random_instance(
    rng: random.Random, idx: int
) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]:
    base_day = parse_iso_day("2026-01-01") + (idx % 200)
    min_fee = rng.randint(200, 450)
    fee = FeeSchedule(
        fixed_fee_cents=rng.randint(100, 350),
        rate_permille=rng.randint(15, 35),
        min_fee_cents=min_fee,
        max_fee_cents=rng.randint(15_000, 50_000),
        tolerance_cents=rng.randint(10, 50),
        max_settlement_lag_days=rng.randint(4, 7),
    )
    invoices: list[Invoice] = []
    txs: list[BankTx] = []
    _add_group_a_b_records(rng, idx, base_day, fee, invoices, txs)
    _add_group_c_d_e_records(rng, idx, base_day, fee, invoices, txs)
    return tuple(invoices), tuple(txs), fee
