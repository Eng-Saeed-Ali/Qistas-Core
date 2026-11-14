from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import date
from enum import Enum, StrEnum

MAX_AMOUNT_CENTS, MAX_RATE_PERMILLE, MAX_LAG_DAYS_LIMIT = 999_999_999_99, 1_000, 365
MAX_BATCH_SIZE, MAX_EXPANSIONS, GENESIS_HASH_U64 = 4, 10_000, 0x5149535441533236
_DAY_MAX, _U64_MAX = 3_652_059, (1 << 64) - 1
_ID_RE = re.compile(r"^[A-Z0-9_-]{1,32}\Z")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")

class AccountCode(StrEnum):
    AR_RECEIVABLE = "AR_RECEIVABLE"
    BANK_CASH = "BANK_CASH"
    GATEWAY_FEE_EXPENSE = "GATEWAY_FEE_EXPENSE"
    SUSPENSE_CLEARING = "SUSPENSE_CLEARING"
    CUSTOMER_CREDIT_LIABILITY = "CUSTOMER_CREDIT_LIABILITY"

class MatchKind(StrEnum):
    EXACT_REF = "EXACT_REF"
    FEE_ADJUSTED_REF = "FEE_ADJUSTED_REF"
    ENTITY_EXACT = "ENTITY_EXACT"
    ENTITY_FEE_ADJUSTED = "ENTITY_FEE_ADJUSTED"
    BATCH_SUBSET_SUM = "BATCH_SUBSET_SUM"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    OVERPAYMENT_CREDIT = "OVERPAYMENT_CREDIT"
    DUPLICATE_PAYMENT_ALERT = "DUPLICATE_PAYMENT_ALERT"
    ORPHAN_BANK_DEPOSIT = "ORPHAN_BANK_DEPOSIT"

_A, _R, _L = MAX_AMOUNT_CENTS, MAX_RATE_PERMILLE, MAX_LAG_DAYS_LIMIT
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "fixed_fee_cents": (0, _A), "rate_permille": (0, _R), "min_fee_cents": (0, _A),
    "max_fee_cents": (0, _A), "tolerance_cents": (0, _A), "max_settlement_lag_days": (0, _L),
    "amount_cents": (1, _A), "residual_cents": (1, _A), "day_ordinal": (1, _DAY_MAX),
    "debit_cents": (0, -1), "credit_cents": (0, -1), "gross_cents": (0, -1),
    "fee_cents": (0, -1), "seq_no": (0, -1), "prev_hash_u64": (0, _U64_MAX),
    "chain_hash_u64": (0, _U64_MAX),
}
_ID_F = frozenset({"id", "ref_id", "entry_id", "tx_id", "invoice_id", "primary_id"})
_STR_F = frozenset({"raw_entity", "norm_entity", "raw_payer", "memo"})
_TUP_F = frozenset({"invoice_ids", "secondary_ids"})
_ENUM_T: dict[str, type[Enum]] = {"account": AccountCode, "match_kind": MatchKind}

def _vi(v: object, lo: int, hi: int = -1) -> int:
    if type(v) is not int:
        raise TypeError("expected int")
    if v < lo or (hi >= 0 and v > hi):
        raise ValueError("int out of range")
    assert type(v) is int
    assert v >= lo
    return v

def ceil_div(n: int, d: int) -> int:
    _vi(n, 0)
    _vi(d, 1)
    res = -(-n // d)
    assert type(res) is int
    assert res >= 0 and res * d >= n
    return res

def parse_iso_day(s: str) -> int:
    if type(s) is not str:
        raise TypeError("expected str date")
    if _DATE_RE.match(s) is None:
        raise ValueError("invalid ISO date")
    day = date.fromisoformat(s).toordinal()
    assert type(day) is int
    assert 1 <= day <= _DAY_MAX
    return day

def format_iso_day(day_ordinal: int) -> str:
    _vi(day_ordinal, 1, _DAY_MAX)
    iso = date.fromordinal(day_ordinal).isoformat()
    assert type(iso) is str
    assert len(iso) == 10
    return iso

@dataclass(slots=True, frozen=True)
class FeeSchedule:
    fixed_fee_cents: int = 0
    rate_permille: int = 0
    min_fee_cents: int = 0
    max_fee_cents: int = 0
    tolerance_cents: int = 0
    max_settlement_lag_days: int = 5
    def __post_init__(self) -> None:
        _chk(self)
        assert self.rate_permille <= MAX_RATE_PERMILLE
        assert self.max_fee_cents == 0 or self.max_fee_cents >= self.min_fee_cents

@dataclass(slots=True, frozen=True)
class Invoice:
    id: str
    raw_entity: str
    norm_entity: str
    amount_cents: int
    day_ordinal: int
    ref_code: str = ""
    def __post_init__(self) -> None:
        _chk(self)
        assert self.amount_cents >= 1
        assert len(self.id) >= 1

@dataclass(slots=True, frozen=True)
class BankTx:
    id: str
    raw_payer: str
    norm_entity: str
    net_cents: int
    day_ordinal: int
    memo: str = ""
    def __post_init__(self) -> None:
        _chk(self)
        assert self.net_cents >= 1
        assert len(self.id) >= 1

@dataclass(slots=True, frozen=True)
class LedgerPosting:
    account: AccountCode
    debit_cents: int
    credit_cents: int
    ref_id: str
    def __post_init__(self) -> None:
        _chk(self)
        assert (self.debit_cents > 0) ^ (self.credit_cents > 0)
        assert self.debit_cents >= 0 and self.credit_cents >= 0

@dataclass(slots=True, frozen=True)
class JournalEntry:
    entry_id: str
    day_ordinal: int
    match_kind: MatchKind
    postings: tuple[LedgerPosting, ...]
    memo: str = ""
    def __post_init__(self) -> None:
        _chk(self)
        assert len(self.postings) >= 2
        assert sum(p.debit_cents for p in self.postings) >= 1

@dataclass(slots=True, frozen=True)
class ReconciliationMatch:
    tx_id: str
    match_kind: MatchKind
    invoice_ids: tuple[str, ...]
    gross_cents: int
    net_cents: int
    fee_cents: int
    day_ordinal: int
    def __post_init__(self) -> None:
        _chk(self)
        assert self.net_cents >= 1
        assert self.fee_cents >= 0

@dataclass(slots=True, frozen=True)
class UnpaidInvoiceRecord:
    invoice_id: str
    residual_cents: int
    day_ordinal: int
    norm_entity: str
    def __post_init__(self) -> None:
        _chk(self)
        assert self.residual_cents >= 1
        assert len(self.invoice_id) >= 1

@dataclass(slots=True, frozen=True)
class AuditLogEntry:
    seq_no: int
    match_kind: MatchKind
    primary_id: str
    secondary_ids: tuple[str, ...]
    gross_cents: int
    net_cents: int
    fee_cents: int
    day_ordinal: int
    prev_hash_u64: int
    chain_hash_u64: int
    def __post_init__(self) -> None:
        _chk(self)
        assert self.seq_no >= 0
        assert 0 <= self.chain_hash_u64 <= _U64_MAX

_Model = (
    FeeSchedule | Invoice | BankTx | LedgerPosting
    | JournalEntry | ReconciliationMatch | UnpaidInvoiceRecord | AuditLogEntry
)

def _chk(obj: _Model) -> None:
    for f in fields(obj):
        k, v = f.name, getattr(obj, f.name)
        if k == "net_cents":
            hi = MAX_AMOUNT_CENTS if isinstance(obj, BankTx) else -1
            _vi(v, 0 if isinstance(obj, AuditLogEntry) else 1, hi)
        elif k in _INT_BOUNDS:
            _vi(v, *_INT_BOUNDS[k])
        elif k in _ID_F or k in _TUP_F or k == "ref_code":
            if k in _TUP_F and type(v) is not tuple:
                raise TypeError("expected tuple of ids")
            for item in (v if k in _TUP_F else (v,)):
                if type(item) is not str:
                    raise TypeError("expected str id")
                if (k != "ref_code" or item != "") and _ID_RE.match(item) is None:
                    raise ValueError("invalid id")
        elif k in _STR_F:
            if type(v) is not str:
                raise TypeError("expected str")
            if "\x00" in v or (k == "raw_entity" and not v.strip()):
                raise ValueError("invalid str")
        elif k in _ENUM_T and not isinstance(v, _ENUM_T[k]):
            raise TypeError("invalid enum")
    if isinstance(obj, FeeSchedule) and obj.max_fee_cents and obj.max_fee_cents < obj.min_fee_cents:
        raise ValueError("max_fee_cents < min_fee_cents")
    if isinstance(obj, LedgerPosting) and not ((obj.debit_cents > 0) ^ (obj.credit_cents > 0)):
        raise ValueError("posting must be single-sided > 0")
    if isinstance(obj, JournalEntry):
        ps = obj.postings
        if type(ps) is not tuple:
            raise TypeError("postings must be tuple")
        if len(ps) < 2 or not all(isinstance(p, LedgerPosting) for p in ps):
            raise ValueError("invalid postings")
        d, c = sum(p.debit_cents for p in ps), sum(p.credit_cents for p in ps)
        if d <= 0 or d != c:
            raise ValueError("unbalanced journal entry")
    assert obj is not None
    assert len(fields(obj)) >= 4
