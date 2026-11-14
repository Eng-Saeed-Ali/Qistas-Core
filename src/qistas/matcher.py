from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from qistas.ledger import (
    TrialBalance,
    build_audit_chain,
    build_journal_entry,
    compute_trial_balance,
    expected_gateway_fee,
    fee_matches,
)
from qistas.models import (
    MAX_AMOUNT_CENTS,
    MAX_BATCH_SIZE,
    MAX_EXPANSIONS,
    AuditLogEntry,
    BankTx,
    FeeSchedule,
    Invoice,
    JournalEntry,
    MatchKind,
    ReconciliationMatch,
    UnpaidInvoiceRecord,
    ceil_div,
)
from qistas.normalizer import extract_ref_codes


@dataclass(slots=True, frozen=True)
class ReconciliationResult:
    matches: tuple[ReconciliationMatch, ...]
    journal_entries: tuple[JournalEntry, ...]
    unpaid_invoices: tuple[UnpaidInvoiceRecord, ...]
    audit_chain: tuple[AuditLogEntry, ...]
    trial_balance: TrialBalance
    expansions: int
    proven_optimal_batches: bool = True

    def __post_init__(self) -> None:
        if type(self.matches) is not tuple or type(self.journal_entries) is not tuple:
            raise TypeError("matches and journal_entries must be tuple")
        if type(self.unpaid_invoices) is not tuple or type(self.audit_chain) is not tuple:
            raise TypeError("unpaid_invoices and audit_chain must be tuple")
        if not isinstance(self.trial_balance, TrialBalance):
            raise TypeError("trial_balance must be TrialBalance")
        if type(self.expansions) is not int or self.expansions < 0:
            raise ValueError("expansions must be non-negative int")
        if type(self.proven_optimal_batches) is not bool:
            raise TypeError("proven_optimal_batches must be bool")
        if len(self.matches) != len(self.journal_entries) or len(self.matches) != len(
            self.audit_chain
        ):
            raise ValueError("result collection lengths must match")
        assert len(self.matches) == len(self.journal_entries)
        assert self.expansions >= 0


def _compute_gross_upper(tx: BankTx, fee: FeeSchedule) -> int:
    if fee.rate_permille == 0 and fee.fixed_fee_cents == 0 and fee.min_fee_cents == 0:
        gross_upper = tx.net_cents + fee.tolerance_cents
    elif fee.rate_permille < 1000:
        per_inv_fixed = max(fee.fixed_fee_cents, fee.min_fee_cents)
        max_fixed = MAX_BATCH_SIZE * per_inv_fixed + fee.tolerance_cents + MAX_BATCH_SIZE
        gross_upper = ceil_div(1000 * (tx.net_cents + max_fixed), 1000 - fee.rate_permille)
    else:
        gross_upper = MAX_BATCH_SIZE * MAX_AMOUNT_CENTS
    assert type(gross_upper) is int
    assert gross_upper >= tx.net_cents
    return gross_upper


def _eval_batch_subset(
    subset: tuple[Invoice, ...],
    gross_cents: int,
    tx: BankTx,
    fee: FeeSchedule,
) -> int | None:
    actual_fee = gross_cents - tx.net_cents
    sb_ok, sb_err = fee_matches(gross_cents, tx.net_cents, fee)
    best_err: int | None = sb_err if sb_ok else None
    if 0 <= actual_fee <= gross_cents:
        if actual_fee == 0:
            pi_err = 0
        else:
            exp_sum = sum(expected_gateway_fee(inv.amount_cents, fee) for inv in subset)
            pi_err = abs(actual_fee - exp_sum)
        if (actual_fee == 0 or pi_err <= fee.tolerance_cents) and (
            best_err is None or pi_err < best_err
        ):
            best_err = pi_err
    assert len(subset) >= 2
    assert best_err is None or best_err >= 0
    return best_err


def solve_subset_sum_batch(
    candidates: Sequence[Invoice],
    tx: BankTx,
    fee: FeeSchedule,
    max_expansions: int = MAX_EXPANSIONS,
) -> tuple[tuple[Invoice, ...], int, int, bool]:
    if not isinstance(tx, BankTx) or not isinstance(fee, FeeSchedule):
        raise TypeError("tx must be BankTx and fee must be FeeSchedule")
    if type(max_expansions) is not int or max_expansions < 0:
        raise ValueError("max_expansions must be non-negative int")
    tx_day, max_lag = tx.day_ordinal, fee.max_settlement_lag_days
    cands = tuple(sorted(
        (inv for inv in candidates if 0 <= tx_day - inv.day_ordinal <= max_lag),
        key=lambda i: (-i.amount_cents, i.day_ordinal, i.id),
    ))
    m = len(cands)
    suffix = [0] * (m + 1)
    for idx in range(m - 1, -1, -1):
        suffix[idx] = suffix[idx + 1] + cands[idx].amount_cents
    gross_upper = _compute_gross_upper(tx, fee)
    stack: list[tuple[int, int, tuple[Invoice, ...]]] = [(0, 0, ())] if m >= 2 else []
    best_subset: tuple[Invoice, ...] = ()
    best_rank: tuple[int, int, tuple[str, ...]] | None = None
    best_gross, expansions = 0, 0
    for _ in range(max_expansions):
        if not stack:
            break
        idx, run_gross, picked = stack.pop()
        expansions += 1
        rem_slots = MAX_BATCH_SIZE - len(picked)
        if idx >= m or len(picked) + (m - idx) < 2 or rem_slots <= 0:
            continue
        max_add = suffix[idx] - suffix[min(m, idx + rem_slots)]
        if run_gross + suffix[idx] < tx.net_cents or run_gross + max_add < tx.net_cents:
            continue
        if idx + 1 < m and len(picked) + (m - idx - 1) >= 2:
            stack.append((idx + 1, run_gross, picked))
        cand = cands[idx]
        new_gross = run_gross + cand.amount_cents
        if new_gross <= gross_upper:
            new_picked = (*picked, cand)
            if len(new_picked) >= 2 and new_gross >= tx.net_cents:
                err = _eval_batch_subset(new_picked, new_gross, tx, fee)
                if err is not None:
                    rank = (err, len(new_picked), tuple(sorted(i.id for i in new_picked)))
                    if best_rank is None or rank < best_rank:
                        best_rank, best_subset, best_gross = rank, new_picked, new_gross
            if len(new_picked) < MAX_BATCH_SIZE and idx + 1 < m:
                stack.append((idx + 1, new_gross, new_picked))
    ordered = tuple(sorted(best_subset, key=lambda i: (i.day_ordinal, i.id)))
    actual_fee = best_gross - tx.net_cents if ordered else 0
    assert expansions <= max_expansions
    assert not ordered or 2 <= len(ordered) <= MAX_BATCH_SIZE
    return (ordered, actual_fee, expansions, len(stack) == 0)


def _best_single_candidate(
    candidates: Sequence[Invoice],
    tx: BankTx,
    fee: FeeSchedule,
    *,
    enforce_window: bool,
) -> tuple[Invoice, int] | None:
    best_inv: Invoice | None = None
    best_rank: tuple[int, int, int, str] | None = None
    best_fee = 0
    for inv in candidates:
        lag = tx.day_ordinal - inv.day_ordinal
        if enforce_window and not (0 <= lag <= fee.max_settlement_lag_days):
            continue
        ok, err = fee_matches(inv.amount_cents, tx.net_cents, fee)
        if not ok:
            continue
        rank = (err, abs(lag), inv.day_ordinal, inv.id)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_inv = inv
            best_fee = inv.amount_cents - tx.net_cents
    assert best_inv is None or best_fee >= 0
    assert type(enforce_window) is bool
    return (best_inv, best_fee) if best_inv is not None else None


def _run_pass1_exact_ref(
    sorted_invoices: tuple[Invoice, ...],
    sorted_txs: tuple[BankTx, ...],
    fee: FeeSchedule,
    settled_inv_ids: set[str],
    matches_by_tx: dict[str, ReconciliationMatch],
) -> None:
    for tx in sorted_txs:
        refs = extract_ref_codes(tx.memo)
        if not refs:
            continue
        ref_set = set(refs)
        open_cands = [
            inv for inv in sorted_invoices
            if inv.id not in settled_inv_ids and (inv.id in ref_set or inv.ref_code in ref_set)
        ]
        matched = _best_single_candidate(open_cands, tx, fee, enforce_window=True)
        if matched is not None:
            inv, fee_cents = matched
            kind = MatchKind.EXACT_REF if fee_cents == 0 else MatchKind.FEE_ADJUSTED_REF
            matches_by_tx[tx.id] = ReconciliationMatch(
                tx.id, kind, (inv.id,), inv.amount_cents, tx.net_cents, fee_cents, tx.day_ordinal
            )
            settled_inv_ids.add(inv.id)
            continue
        settled_cands = [
            inv for inv in sorted_invoices
            if inv.id in settled_inv_ids and (inv.id in ref_set or inv.ref_code in ref_set)
        ]
        dup = _best_single_candidate(settled_cands, tx, fee, enforce_window=False)
        if dup is not None:
            inv_s, _ = dup
            matches_by_tx[tx.id] = ReconciliationMatch(
                tx.id, MatchKind.DUPLICATE_PAYMENT_ALERT, (inv_s.id,),
                tx.net_cents, tx.net_cents, 0, tx.day_ordinal,
            )
    assert len(matches_by_tx) <= len(sorted_txs)
    assert len(settled_inv_ids) <= len(sorted_invoices)


def _run_pass2_entity_match(
    sorted_invoices: tuple[Invoice, ...],
    sorted_txs: tuple[BankTx, ...],
    fee: FeeSchedule,
    settled_inv_ids: set[str],
    matches_by_tx: dict[str, ReconciliationMatch],
) -> None:
    for tx in sorted_txs:
        if tx.id in matches_by_tx or not tx.norm_entity:
            continue
        entity_cands = [
            inv for inv in sorted_invoices
            if inv.id not in settled_inv_ids and inv.norm_entity == tx.norm_entity
        ]
        matched = _best_single_candidate(entity_cands, tx, fee, enforce_window=True)
        if matched is not None:
            inv, fee_cents = matched
            kind = MatchKind.ENTITY_EXACT if fee_cents == 0 else MatchKind.ENTITY_FEE_ADJUSTED
            matches_by_tx[tx.id] = ReconciliationMatch(
                tx.id, kind, (inv.id,), inv.amount_cents, tx.net_cents, fee_cents, tx.day_ordinal
            )
            settled_inv_ids.add(inv.id)
    assert len(matches_by_tx) <= len(sorted_txs)
    assert len(settled_inv_ids) <= len(sorted_invoices)


def _run_pass3_batch_subset(
    sorted_invoices: tuple[Invoice, ...],
    sorted_txs: tuple[BankTx, ...],
    fee: FeeSchedule,
    max_expansions: int,
    settled_inv_ids: set[str],
    matches_by_tx: dict[str, ReconciliationMatch],
) -> tuple[int, bool]:
    total_expansions = 0
    proven_optimal = True
    for tx in sorted_txs:
        if tx.id in matches_by_tx or not tx.norm_entity:
            continue
        open_cands = [
            inv for inv in sorted_invoices
            if inv.id not in settled_inv_ids and inv.norm_entity == tx.norm_entity
        ]
        subset, fee_cents, exp_used, optimal = solve_subset_sum_batch(
            open_cands, tx, fee, max_expansions
        )
        total_expansions += exp_used
        if not optimal:
            proven_optimal = False
        if subset:
            gross_cents = sum(inv.amount_cents for inv in subset)
            inv_ids = tuple(inv.id for inv in subset)
            matches_by_tx[tx.id] = ReconciliationMatch(
                tx.id, MatchKind.BATCH_SUBSET_SUM, inv_ids,
                gross_cents, tx.net_cents, fee_cents, tx.day_ordinal,
            )
            settled_inv_ids.update(inv_ids)
    assert total_expansions >= 0
    assert type(proven_optimal) is bool
    return total_expansions, proven_optimal


def _resolve_pass4_target(
    tx: BankTx,
    sorted_invoices: tuple[Invoice, ...],
    residual_by_id: dict[str, int],
    fee: FeeSchedule,
) -> tuple[Invoice | None, bool]:
    refs = extract_ref_codes(tx.memo)
    target: Invoice | None = None
    via_ref = False
    if refs:
        ref_set = set(refs)
        for inv in sorted_invoices:
            if (
                residual_by_id.get(inv.id, 0) > 0
                and inv.day_ordinal <= tx.day_ordinal
                and (inv.id in ref_set or inv.ref_code in ref_set)
            ):
                target = inv
                via_ref = True
                break
    if target is None and tx.norm_entity:
        cands = [
            inv for inv in sorted_invoices
            if residual_by_id.get(inv.id, 0) > 0
            and inv.norm_entity == tx.norm_entity
            and 0 <= tx.day_ordinal - inv.day_ordinal <= fee.max_settlement_lag_days
        ]
        if len(cands) == 1:
            target = cands[0]
    assert type(via_ref) is bool
    assert target is None or residual_by_id[target.id] > 0
    return target, via_ref


def _run_pass4_residuals(
    sorted_invoices: tuple[Invoice, ...],
    sorted_txs: tuple[BankTx, ...],
    fee: FeeSchedule,
    settled_inv_ids: set[str],
    matches_by_tx: dict[str, ReconciliationMatch],
) -> tuple[UnpaidInvoiceRecord, ...]:
    residual_by_id = {
        inv.id: inv.amount_cents for inv in sorted_invoices if inv.id not in settled_inv_ids
    }
    for tx in sorted_txs:
        if tx.id in matches_by_tx:
            continue
        target, via_ref = _resolve_pass4_target(tx, sorted_invoices, residual_by_id, fee)
        if target is None:
            matches_by_tx[tx.id] = ReconciliationMatch(
                tx.id, MatchKind.ORPHAN_BANK_DEPOSIT, (), 0, tx.net_cents, 0, tx.day_ordinal
            )
            continue
        residual = residual_by_id[target.id]
        if tx.net_cents > residual:
            kind, gross = MatchKind.OVERPAYMENT_CREDIT, residual
            residual_by_id[target.id] = 0
        elif tx.net_cents == residual:
            kind = MatchKind.EXACT_REF if via_ref else MatchKind.ENTITY_EXACT
            gross = residual
            residual_by_id[target.id] = 0
        else:
            kind, gross = MatchKind.PARTIAL_PAYMENT, tx.net_cents
            residual_by_id[target.id] = residual - tx.net_cents
        matches_by_tx[tx.id] = ReconciliationMatch(
            tx.id, kind, (target.id,), gross, tx.net_cents, 0, tx.day_ordinal
        )
    unpaid = tuple(
        UnpaidInvoiceRecord(inv.id, residual_by_id[inv.id], inv.day_ordinal, inv.norm_entity)
        for inv in sorted_invoices if residual_by_id.get(inv.id, 0) > 0
    )
    assert len(matches_by_tx) == len(sorted_txs)
    assert all(r.residual_cents > 0 for r in unpaid)
    return unpaid


_DEFAULT_FEE: FeeSchedule = FeeSchedule()


def reconcile(
    invoices: Sequence[Invoice],
    bank_txs: Sequence[BankTx],
    fee: FeeSchedule = _DEFAULT_FEE,
    max_expansions: int = MAX_EXPANSIONS,
) -> ReconciliationResult:
    if not isinstance(fee, FeeSchedule):
        raise TypeError("fee must be FeeSchedule")
    if type(max_expansions) is not int or max_expansions < 0:
        raise ValueError("max_expansions must be non-negative int")
    sorted_invoices = tuple(sorted(invoices, key=lambda i: (i.day_ordinal, i.id)))
    sorted_txs = tuple(sorted(bank_txs, key=lambda t: (t.day_ordinal, t.id)))
    if len({i.id for i in sorted_invoices}) != len(sorted_invoices):
        raise ValueError("duplicate invoice id")
    if len({t.id for t in sorted_txs}) != len(sorted_txs):
        raise ValueError("duplicate bank transaction id")
    settled_inv_ids: set[str] = set()
    matches_by_tx: dict[str, ReconciliationMatch] = {}
    _run_pass1_exact_ref(sorted_invoices, sorted_txs, fee, settled_inv_ids, matches_by_tx)
    _run_pass2_entity_match(sorted_invoices, sorted_txs, fee, settled_inv_ids, matches_by_tx)
    expansions, proven_optimal = _run_pass3_batch_subset(
        sorted_invoices, sorted_txs, fee, max_expansions, settled_inv_ids, matches_by_tx
    )
    unpaid = _run_pass4_residuals(sorted_invoices, sorted_txs, fee, settled_inv_ids, matches_by_tx)
    ordered_matches = tuple(matches_by_tx[tx.id] for tx in sorted_txs)
    entries = tuple(
        build_journal_entry(f"ENTRY-{idx:04d}", m)
        for idx, m in enumerate(ordered_matches, start=1)
    )
    audit_chain = build_audit_chain(ordered_matches)
    trial_balance = compute_trial_balance(entries)
    assert len(ordered_matches) == len(sorted_txs)
    assert trial_balance.total_debit_cents == trial_balance.total_credit_cents
    return ReconciliationResult(
        matches=ordered_matches,
        journal_entries=entries,
        unpaid_invoices=unpaid,
        audit_chain=audit_chain,
        trial_balance=trial_balance,
        expansions=expansions,
        proven_optimal_batches=proven_optimal,
    )
