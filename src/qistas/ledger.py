from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from qistas.models import (
    GENESIS_HASH_U64,
    AccountCode,
    AuditLogEntry,
    FeeSchedule,
    JournalEntry,
    LedgerPosting,
    MatchKind,
    ReconciliationMatch,
    ceil_div,
)

_U64_LIMIT: int = 1 << 64
_SETTLED_KINDS: frozenset[MatchKind] = frozenset({
    MatchKind.EXACT_REF,
    MatchKind.FEE_ADJUSTED_REF,
    MatchKind.ENTITY_EXACT,
    MatchKind.ENTITY_FEE_ADJUSTED,
    MatchKind.BATCH_SUBSET_SUM,
})


@dataclass(slots=True, frozen=True)
class TrialBalance:
    debits_by_account: tuple[tuple[AccountCode, int], ...]
    credits_by_account: tuple[tuple[AccountCode, int], ...]
    total_debit_cents: int
    total_credit_cents: int

    def __post_init__(self) -> None:
        if type(self.total_debit_cents) is not int or type(self.total_credit_cents) is not int:
            raise TypeError("totals must be int")
        if self.total_debit_cents < 0 or self.total_debit_cents != self.total_credit_cents:
            raise ValueError("trial balance out of balance")
        expected_accounts = tuple(AccountCode)
        for rows, expected_sum in (
            (self.debits_by_account, self.total_debit_cents),
            (self.credits_by_account, self.total_credit_cents),
        ):
            if type(rows) is not tuple or len(rows) != len(expected_accounts):
                raise ValueError("account rows must match AccountCode order")
            running = 0
            for (acct, amt), exp_acct in zip(rows, expected_accounts, strict=True):
                if acct != exp_acct:
                    raise ValueError("account ordering mismatch")
                if type(amt) is not int or amt < 0:
                    raise ValueError("account balance must be non-negative int")
                running += amt
            if running != expected_sum:
                raise ValueError("row sum mismatch")
        assert self.total_debit_cents == self.total_credit_cents
        assert len(self.debits_by_account) == len(AccountCode)


def expected_gateway_fee(gross_cents: int, fee: FeeSchedule) -> int:
    if type(gross_cents) is not int:
        raise TypeError("gross_cents must be int")
    if not isinstance(fee, FeeSchedule):
        raise TypeError("fee must be FeeSchedule")
    if gross_cents < 0:
        raise ValueError("gross_cents must be non-negative")
    raw_fee = fee.fixed_fee_cents + ceil_div(gross_cents * fee.rate_permille, 1000)
    floored = max(raw_fee, fee.min_fee_cents)
    res = min(floored, fee.max_fee_cents) if fee.max_fee_cents > 0 else floored
    assert type(res) is int
    assert res >= 0
    return res


def fee_matches(gross_cents: int, net_cents: int, fee: FeeSchedule) -> tuple[bool, int]:
    if type(gross_cents) is not int or type(net_cents) is not int:
        raise TypeError("gross_cents and net_cents must be int")
    if not isinstance(fee, FeeSchedule):
        raise TypeError("fee must be FeeSchedule")
    if gross_cents < 0 or net_cents < 0:
        raise ValueError("amounts must be non-negative")
    actual_fee = gross_cents - net_cents
    if actual_fee < 0 or actual_fee > gross_cents:
        res = (False, abs(actual_fee))
    elif actual_fee == 0:
        res = (True, 0)
    else:
        exp_fee = expected_gateway_fee(gross_cents, fee)
        fee_err = abs(actual_fee - exp_fee)
        res = (fee_err <= fee.tolerance_cents, fee_err)
    assert type(res[0]) is bool
    assert type(res[1]) is int and res[1] >= 0
    return res


def build_journal_entry(entry_id: str, match: ReconciliationMatch) -> JournalEntry:
    if not isinstance(match, ReconciliationMatch):
        raise TypeError("match must be ReconciliationMatch")
    ar_ref = match.invoice_ids[0] if match.invoice_ids else match.tx_id
    kind = match.match_kind
    if kind in _SETTLED_KINDS:
        if match.fee_cents == 0:
            postings: tuple[LedgerPosting, ...] = (
                LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
                LedgerPosting(AccountCode.AR_RECEIVABLE, 0, match.gross_cents, ar_ref),
            )
        else:
            postings = (
                LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
                LedgerPosting(AccountCode.GATEWAY_FEE_EXPENSE, match.fee_cents, 0, match.tx_id),
                LedgerPosting(AccountCode.AR_RECEIVABLE, 0, match.gross_cents, ar_ref),
            )
    elif kind is MatchKind.PARTIAL_PAYMENT:
        postings = (
            LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
            LedgerPosting(AccountCode.AR_RECEIVABLE, 0, match.net_cents, ar_ref),
        )
    elif kind is MatchKind.OVERPAYMENT_CREDIT:
        excess = match.net_cents - match.gross_cents
        if excess <= 0 or match.gross_cents <= 0:
            raise ValueError("OVERPAYMENT_CREDIT requires net_cents > gross_cents > 0")
        postings = (
            LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
            LedgerPosting(AccountCode.AR_RECEIVABLE, 0, match.gross_cents, ar_ref),
            LedgerPosting(AccountCode.CUSTOMER_CREDIT_LIABILITY, 0, excess, match.tx_id),
        )
    elif kind is MatchKind.DUPLICATE_PAYMENT_ALERT:
        postings = (
            LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
            LedgerPosting(AccountCode.CUSTOMER_CREDIT_LIABILITY, 0, match.net_cents, match.tx_id),
        )
    else:
        postings = (
            LedgerPosting(AccountCode.BANK_CASH, match.net_cents, 0, match.tx_id),
            LedgerPosting(AccountCode.SUSPENSE_CLEARING, 0, match.net_cents, match.tx_id),
        )
    entry = JournalEntry(entry_id, match.day_ordinal, kind, postings)
    assert isinstance(entry, JournalEntry)
    assert len(entry.postings) >= 2
    return entry


def compute_trial_balance(entries: Sequence[JournalEntry]) -> TrialBalance:
    debits: dict[AccountCode, int] = {acct: 0 for acct in AccountCode}
    credits: dict[AccountCode, int] = {acct: 0 for acct in AccountCode}
    for entry in entries:
        if not isinstance(entry, JournalEntry):
            raise TypeError("entries must contain JournalEntry")
        for posting in entry.postings:
            debits[posting.account] += posting.debit_cents
            credits[posting.account] += posting.credit_cents
    debit_rows = tuple((acct, debits[acct]) for acct in AccountCode)
    credit_rows = tuple((acct, credits[acct]) for acct in AccountCode)
    total_debit = sum(debits[acct] for acct in AccountCode)
    total_credit = sum(credits[acct] for acct in AccountCode)
    tb = TrialBalance(debit_rows, credit_rows, total_debit, total_credit)
    assert tb.total_debit_cents == tb.total_credit_cents
    assert len(tb.debits_by_account) == len(AccountCode)
    return tb


def _hash_audit_fields(
    seq_no: int,
    match_kind: MatchKind,
    primary_id: str,
    secondary_ids: tuple[str, ...],
    gross_cents: int,
    net_cents: int,
    fee_cents: int,
    day_ordinal: int,
    prev_hash_u64: int,
) -> int:
    if type(seq_no) is not int or type(prev_hash_u64) is not int:
        raise TypeError("seq_no and prev_hash_u64 must be int")
    if seq_no < 0 or not 0 <= prev_hash_u64 < _U64_LIMIT:
        raise ValueError("seq_no or prev_hash_u64 out of range")
    sec_joined = ",".join(secondary_ids)
    payload = (
        f"{seq_no}|{match_kind.value}|{primary_id}|{sec_joined}|"
        f"{gross_cents}|{net_cents}|{fee_cents}|{day_ordinal}|{prev_hash_u64}"
    ).encode()
    digest = int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")
    assert type(digest) is int
    assert 0 <= digest < _U64_LIMIT
    return digest


def compute_audit_hash(seq_no: int, match: ReconciliationMatch, prev_hash_u64: int) -> int:
    if not isinstance(match, ReconciliationMatch):
        raise TypeError("match must be ReconciliationMatch")
    digest = _hash_audit_fields(
        seq_no,
        match.match_kind,
        match.tx_id,
        match.invoice_ids,
        match.gross_cents,
        match.net_cents,
        match.fee_cents,
        match.day_ordinal,
        prev_hash_u64,
    )
    assert type(digest) is int
    assert 0 <= digest < _U64_LIMIT
    return digest


def build_audit_chain(matches: Sequence[ReconciliationMatch]) -> tuple[AuditLogEntry, ...]:
    out: list[AuditLogEntry] = []
    prev_hash = GENESIS_HASH_U64
    for seq_no, match in enumerate(matches):
        if not isinstance(match, ReconciliationMatch):
            raise TypeError("matches must contain ReconciliationMatch")
        chain_hash = compute_audit_hash(seq_no, match, prev_hash)
        entry = AuditLogEntry(
            seq_no=seq_no,
            match_kind=match.match_kind,
            primary_id=match.tx_id,
            secondary_ids=match.invoice_ids,
            gross_cents=match.gross_cents,
            net_cents=match.net_cents,
            fee_cents=match.fee_cents,
            day_ordinal=match.day_ordinal,
            prev_hash_u64=prev_hash,
            chain_hash_u64=chain_hash,
        )
        out.append(entry)
        prev_hash = chain_hash
    chain = tuple(out)
    assert len(chain) == len(matches)
    assert not chain or chain[0].prev_hash_u64 == GENESIS_HASH_U64
    return chain


def verify_audit_chain(chain: Sequence[AuditLogEntry]) -> bool:
    expected_prev = GENESIS_HASH_U64
    valid = True
    for idx, entry in enumerate(chain):
        if not isinstance(entry, AuditLogEntry):
            raise TypeError("chain must contain AuditLogEntry")
        if entry.seq_no != idx or entry.prev_hash_u64 != expected_prev:
            valid = False
            break
        expected_hash = _hash_audit_fields(
            entry.seq_no,
            entry.match_kind,
            entry.primary_id,
            entry.secondary_ids,
            entry.gross_cents,
            entry.net_cents,
            entry.fee_cents,
            entry.day_ordinal,
            entry.prev_hash_u64,
        )
        if entry.chain_hash_u64 != expected_hash:
            valid = False
            break
        expected_prev = entry.chain_hash_u64
    assert type(valid) is bool
    assert expected_prev >= 0
    return valid
