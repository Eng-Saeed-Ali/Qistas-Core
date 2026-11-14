# `Qistas-Core` — Normative Module & Interface Specification (`SPEC.md`)

**Version:** `1.0.0`  
**Grounding:** `BLUEPRINT.md` + Gate 6 Architectural Refinements (`saeed-engineering-canon`, `modern-python`, `guidelines-advisor`, `sharp-edges`).  
**Enforcement:** All module imports, dataclass decorators (`@dataclass(slots=True, frozen=True)`), function signatures, loop bounds, and numeric operators in this document are mechanically enforced by `tests/test_architecture_fitness.py` (`FF-1` – `FF-6`).

---

## § 0. Global Mechanical Invariants & Gate 6 Refinements

1. **Zero-Float & Zero-Division Law (`R0.1` — `FF-1`):**
   - No `ast.Div` (true division operator), `float`, `decimal.Decimal`, `fractions.Fraction`, `complex`, `round()`, `math.floor()`, `math.ceil()`, or float literals anywhere in `src/qistas/` or `tests/`.
   - All integer division uses floor division `//` or `ceil_div(n, d) = -(-n // d)`.
2. **Strict Import Allowlist & Acyclic DAG (`FF-2`):**
   - Every module in `src/qistas/` may import **only** from its declared `FF-2 ALLOWLIST` below.
   - Zero third-party dependencies in `cli_visualizer.py` (**Gate 6 Refinement #1**: 100% Python Standard Library `argparse`, `pathlib`, and deterministic ASCII table rendering; no `rich` dependency). Only `exporters.py` may import `openpyxl` and `duckdb`.
3. **NASA Power of 10 Control Flow & TigerStyle Function Discipline (`FF-3` – `FF-6`):**
   - Zero recursion (`FF-3`), function length `<= 60` LOC (`FF-4`), side-effect-free assertion density `>= 2` per function (`FF-5`), and `@dataclass(slots=True, frozen=True)` on all `11` domain dataclasses (`FF-6`).

---

## § 1. Layer 0 — `src/qistas/models.py`

### 1.1. Allowed Imports (`FF-2 ALLOWLIST`)
`{"__future__", "datetime", "dataclasses", "enum", "re"}`

### 1.2. Normative Constants
```python
MAX_AMOUNT_CENTS: int = 999_999_999_99
MAX_RATE_PERMILLE: int = 1_000
MAX_LAG_DAYS_LIMIT: int = 365
MAX_BATCH_SIZE: int = 4
MAX_EXPANSIONS: int = 10_000
GENESIS_HASH_U64: int = 0x5149535441533236
```

### 1.3. Enums
```python
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
```

### 1.4. Pure Helper Functions
- `ceil_div(n: int, d: int) -> int`:
  - **Contract:** Requires `type(n) is int and type(d) is int and d > 0 and n >= 0` (raises `ValueError` or `TypeError` on invalid input). Returns `-(-n // d)`.
- `parse_iso_day(s: str) -> int`:
  - **Contract:** Validates `type(s) is str` matching `r"^\d{4}-\d{2}-\d{2}\Z"`, parses via `datetime.date.fromisoformat(s).toordinal()`, returns `day_ordinal: int` (`>= 1`). Raises `ValueError` on malformed date.
- `format_iso_day(day_ordinal: int) -> str`:
  - **Contract:** Validates `type(day_ordinal) is int and 1 <= day_ordinal <= 3_652_059`. Returns `datetime.date.fromordinal(day_ordinal).isoformat()`.

### 1.5. Frozen+Slots Dataclasses (`8` in `models.py`)
All 8 classes use `@dataclass(slots=True, frozen=True)` and enforce `F1` in `__post_init__` (`type(v) is int` to reject `bool`, ASCII ID regex `r"^[A-Z0-9_-]{1,32}\Z"`, NUL-byte ban `"\x00" not in s`, and domain bounds):

1. **`FeeSchedule`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class FeeSchedule:
       fixed_fee_cents: int = 0
       rate_permille: int = 0
       min_fee_cents: int = 0
       max_fee_cents: int = 0
       tolerance_cents: int = 0
       max_settlement_lag_days: int = 5
   ```
   - **Invariants:** All fields `type(v) is int`; `0 <= fixed_fee_cents <= MAX_AMOUNT_CENTS`; `0 <= rate_permille <= MAX_RATE_PERMILLE`; `0 <= min_fee_cents <= MAX_AMOUNT_CENTS`; `0 <= max_fee_cents <= MAX_AMOUNT_CENTS`; `max_fee_cents == 0 or max_fee_cents >= min_fee_cents`; `0 <= tolerance_cents <= MAX_AMOUNT_CENTS`; `0 <= max_settlement_lag_days <= MAX_LAG_DAYS_LIMIT`.

2. **`Invoice`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class Invoice:
       id: str
       raw_entity: str
       norm_entity: str
       amount_cents: int
       day_ordinal: int
       ref_code: str = ""
   ```
   - **Invariants:** `id` matches `^[A-Z0-9_-]{1,32}\Z`; `ref_code == ""` or matches `^[A-Z0-9_-]{1,32}\Z`; `raw_entity` and `norm_entity` are `str` without `"\x00"` and `len(raw_entity.strip()) >= 1`; `1 <= amount_cents <= MAX_AMOUNT_CENTS`; `1 <= day_ordinal <= 3_652_059`.

3. **`BankTx`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class BankTx:
       id: str
       raw_payer: str
       norm_entity: str
       net_cents: int
       day_ordinal: int
       memo: str = ""
   ```
   - **Invariants:** `id` matches `^[A-Z0-9_-]{1,32}\Z`; `raw_payer`, `norm_entity`, `memo` are `str` without `"\x00"`; `1 <= net_cents <= MAX_AMOUNT_CENTS`; `1 <= day_ordinal <= 3_652_059`.

4. **`LedgerPosting`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class LedgerPosting:
       account: AccountCode
       debit_cents: int
       credit_cents: int
       ref_id: str
   ```
   - **Invariants:** `isinstance(account, AccountCode)`; `type(debit_cents) is int and type(credit_cents) is int`; `(debit_cents > 0 and credit_cents == 0) or (credit_cents > 0 and debit_cents == 0)`; `ref_id` matches `^[A-Z0-9_-]{1,32}\Z`.

5. **`JournalEntry`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class JournalEntry:
       entry_id: str
       day_ordinal: int
       match_kind: MatchKind
       postings: tuple[LedgerPosting, ...]
       memo: str = ""
   ```
   - **Invariants:** `entry_id` matches `^[A-Z0-9_-]{1,32}\Z`; `day_ordinal >= 1`; `isinstance(match_kind, MatchKind)`; `type(postings) is tuple and len(postings) >= 2`; all `isinstance(p, LedgerPosting)`; `sum(p.debit_cents for p in postings) == sum(p.credit_cents for p in postings) > 0`; `"\x00" not in memo`.

6. **`ReconciliationMatch`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class ReconciliationMatch:
       tx_id: str
       match_kind: MatchKind
       invoice_ids: tuple[str, ...]
       gross_cents: int
       net_cents: int
       fee_cents: int
       day_ordinal: int
   ```
   - **Invariants:** `tx_id` valid ID; `isinstance(match_kind, MatchKind)`; `type(invoice_ids) is tuple` of valid IDs; `gross_cents >= 0`, `net_cents >= 1`, `fee_cents >= 0`, `day_ordinal >= 1`.

7. **`UnpaidInvoiceRecord`**:
   ```python
   @dataclass(slots=True, frozen=True)
   class UnpaidInvoiceRecord:
       invoice_id: str
       residual_cents: int
       day_ordinal: int
       norm_entity: str
   ```
   - **Invariants:** `invoice_id` valid ID; `1 <= residual_cents <= MAX_AMOUNT_CENTS`; `day_ordinal >= 1`; `"\x00" not in norm_entity`.

8. **`AuditLogEntry`**:
   ```python
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
   ```
   - **Invariants:** `seq_no >= 0`; `primary_id` valid ID; `type(secondary_ids) is tuple`; `0 <= prev_hash_u64 < (1 << 64)`; `0 <= chain_hash_u64 < (1 << 64)`.

---

## § 2. Layer 1 — `src/qistas/normalizer.py`

### 2.1. Allowed Imports (`FF-2 ALLOWLIST`)
`{"__future__", "re", "unicodedata", "qistas.models"}`

### 2.2. Normative Functions
- `normalize_text(raw: str) -> str`:
  - **Contract:** Validates `type(raw) is str` and `"\x00" not in raw`. Executes `F2` Stages 1–3 + casefold + punctuation-to-space + whitespace collapse:
    1. `unicodedata.normalize("NFKC", raw)` (decomposes Arabic Presentation Forms-B ligatures `ﻻ` -> `ل + ا`, `ﻷ` -> `ل + أ`, etc.).
    2. Strip codepoints `c` where `unicodedata.category(c) in ("Mn", "Cf") or c == "\u0640"`.
    3. Unify Arabic/Persian letters (`أ, إ, آ, ٱ -> ا`, `ى, ئ, ی -> ي`, `ک -> ك`, `ة -> ه`, `ؤ -> و`) and map Eastern Arabic (`٠..٩`) and Persian (`۰..۹`) digits to ASCII `0..9`.
    4. Apply `.casefold()`, replace any char outside `[a-z0-9\u0621-\u064a]` with `" "`, and join whitespace-split tokens with `" "`.
- `normalize_entity(raw: str) -> str`:
  - **Contract:** Calls `normalize_text(raw)` and iteratively strips leading/trailing legal entity stop-phrases/tokens (`("ش م م", "ذ م م")` and `{"شركه", "مؤسسه", "مصنع", "مكتب", "مجموعه", "مساهمه", "محدوده", "llc", "ltd", "inc", "co", "corp", "limited", "company", "plc"}`) **only if** stripping leaves at least 1 non-empty token (`EC-A`).
- `extract_ref_codes(memo: str) -> tuple[str, ...]`:
  - **Contract:** Validates `type(memo) is str` and `"\x00" not in memo`. Applies NFKC, strips `"Mn"`/`"Cf"`/`"\u0640"`, maps Arabic/Persian digits to ASCII `0..9`, uppercases, and extracts matches of `r"(?<![A-Z0-9])(INV|FAT|REF|BILL|QST)[-_]?([0-9]{2,12}(?:[-_][0-9A-Z]{1,8})?)(?![A-Z0-9])"`, canonicalizing internal separators (`_`, missing hyphen after prefix) to `"-"` (e.g., `"INV-1001"`, `"FAT-2026-05"`), returning deduplicated `tuple[str, ...]` in first-seen order.

---

## § 3. Layer 1 — `src/qistas/ledger.py`

### 3.1. Allowed Imports (`FF-2 ALLOWLIST`)
`{"__future__", "collections.abc", "dataclasses", "hashlib", "qistas.models"}`

### 3.2. Frozen+Slots Dataclass (`1` in `ledger.py`)
```python
@dataclass(slots=True, frozen=True)
class TrialBalance:
    debits_by_account: tuple[tuple[AccountCode, int], ...]
    credits_by_account: tuple[tuple[AccountCode, int], ...]
    total_debit_cents: int
    total_credit_cents: int
```
- **Invariants:** `total_debit_cents == total_credit_cents >= 0`; account tuples are ordered deterministically by `tuple(AccountCode)`.

### 3.3. Normative Functions
- `expected_gateway_fee(gross_cents: int, fee: FeeSchedule) -> int`:
  - Computes `raw_fee = fee.fixed_fee_cents + ceil_div(gross_cents * fee.rate_permille, 1000)`.
  - Returns `min(max(raw_fee, fee.min_fee_cents), fee.max_fee_cents)` when `fee.max_fee_cents > 0`, else `max(raw_fee, fee.min_fee_cents)`.
- `fee_matches(gross_cents: int, net_cents: int, fee: FeeSchedule) -> tuple[bool, int]`:
  - Let `actual_fee = gross_cents - net_cents`. If `actual_fee < 0 or actual_fee > gross_cents`, returns `(False, abs(actual_fee))`.
  - If `actual_fee == 0`: returns `(True, 0)`.
  - Let `exp_fee = expected_gateway_fee(gross_cents, fee)` and `fee_err = abs(actual_fee - exp_fee)`. Returns `(fee_err <= fee.tolerance_cents, fee_err)`.
- `build_journal_entry(entry_id: str, match: ReconciliationMatch) -> JournalEntry`:
  - Constructs a balanced `JournalEntry` according to `BLUEPRINT.md` `§ 1 F6` table (using `match.tx_id` as `ref_id` on `BANK_CASH` / `GATEWAY_FEE_EXPENSE` / `SUSPENSE_CLEARING` / `CUSTOMER_CREDIT_LIABILITY` postings and `match.invoice_ids[0]` if non-empty else `match.tx_id` on `AR_RECEIVABLE`).
- `compute_trial_balance(entries: Sequence[JournalEntry]) -> TrialBalance`:
  - Aggregates debits and credits per `AccountCode` in enum definition order and verifies `total_debit_cents == total_credit_cents`.
- `compute_audit_hash(seq_no: int, match: ReconciliationMatch, prev_hash_u64: int) -> int`:
  - Formats `f"{seq_no}|{match.match_kind.value}|{match.tx_id}|{','.join(match.invoice_ids)}|{match.gross_cents}|{match.net_cents}|{match.fee_cents}|{match.day_ordinal}|{prev_hash_u64}".encode("utf-8")` and returns `int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")`.
- `build_audit_chain(matches: Sequence[ReconciliationMatch]) -> tuple[AuditLogEntry, ...]`:
  - Iteratively builds the `AuditLogEntry` chain starting from `prev_hash_u64 = GENESIS_HASH_U64`.
- `verify_audit_chain(chain: Sequence[AuditLogEntry]) -> bool`:
  - Recomputes every entry's BLAKE2b digest and verifies `seq_no == idx`, `prev_hash_u64` linkage from `GENESIS_HASH_U64`, and `chain_hash_u64` equality.

---

## § 4. Layer 2 — `src/qistas/matcher.py`

### 4.1. Allowed Imports (`FF-2 ALLOWLIST`)
`{"__future__", "collections.abc", "dataclasses", "qistas.models", "qistas.normalizer", "qistas.ledger"}`

### 4.2. Frozen+Slots Dataclass (`1` in `matcher.py`)
```python
@dataclass(slots=True, frozen=True)
class ReconciliationResult:
    matches: tuple[ReconciliationMatch, ...]
    journal_entries: tuple[JournalEntry, ...]
    unpaid_invoices: tuple[UnpaidInvoiceRecord, ...]
    audit_chain: tuple[AuditLogEntry, ...]
    trial_balance: TrialBalance
    expansions: int
    proven_optimal_batches: bool = True
```

### 4.3. Normative Functions
- `solve_subset_sum_batch(candidates: Sequence[Invoice], tx: BankTx, fee: FeeSchedule, max_expansions: int = MAX_EXPANSIONS) -> tuple[tuple[Invoice, ...], int, int, bool]`:
  - Filters `candidates` satisfying `0 <= tx.day_ordinal - inv.day_ordinal <= fee.max_settlement_lag_days` and sorts descending by `amount_cents`, tie-breaking by ascending `(day_ordinal, id)`.
  - **Gate 6 Refinement #2 (`gross_upper` Admissibility):**
    - If `fee.rate_permille == 0 and fee.fixed_fee_cents == 0 and fee.min_fee_cents == 0`:
      `gross_upper = tx.net_cents + fee.tolerance_cents`
    - Else (when `fee.rate_permille < 1000`):
      `max_fixed = MAX_BATCH_SIZE * max(fee.fixed_fee_cents, fee.min_fee_cents) + fee.tolerance_cents + MAX_BATCH_SIZE`
      `gross_upper = ceil_div(1000 * (tx.net_cents + max_fixed), 1000 - fee.rate_permille)`
    - Else (`fee.rate_permille == 1000`): `gross_upper = MAX_BATCH_SIZE * MAX_AMOUNT_CENTS`.
  - Runs an iterative explicit-stack Branch-and-Bound over subsets of cardinality `2 <= k <= MAX_BATCH_SIZE (4)` with suffix-sum underflow pruning (`running_gross + suffix_sum[idx] < tx.net_cents`) and sorted-descending overflow pruning (`running_gross + C[idx].amount_cents > gross_upper`), capped at `max_expansions`.
  - Evaluates both single-batch fee (`fee_matches(gross_cents, tx.net_cents, fee)`) and per-invoice fee sum (`sum(expected_gateway_fee(inv.amount_cents, fee) for inv in subset)` within `fee.tolerance_cents`).
  - Ranks valid subsets by minimal `(fee_error, len(subset), tuple(sorted(inv.id for inv in subset)))` and returns `(best_invoices_sorted_by_day_id, actual_fee_cents, expansions_used, not_truncated)`.
- `reconcile(invoices: Sequence[Invoice], bank_txs: Sequence[BankTx], fee: FeeSchedule = FeeSchedule(), max_expansions: int = MAX_EXPANSIONS) -> ReconciliationResult`:
  - Executes the 4-Pass Deterministic Matching Hierarchy (`Pass 1` Exact Ref + `EC-D` Duplicate Payment Alert; `Pass 2` 1-to-1 Entity + Window + Fee; `Pass 3` Bounded B&B Batch Subset-Sum; `Pass 4` Partial/Overpayment/Orphan Suspense).
  - **Gate 6 Refinement #3 (Monotonic `residual_cents` in `Pass 4`):** Initializes `residual_by_id[inv.id] = inv.amount_cents` for all open invoices entering `Pass 4`. When an unmatched `tx` targets `inv_target` (by explicit ref or unique open invoice for `tx.norm_entity` within `F4` where `residual_by_id[inv.id] > 0`):
    - If `tx.net_cents > residual`: emits `MatchKind.OVERPAYMENT_CREDIT` with `gross_cents = residual`, `fee_cents = 0`, sets `residual_by_id[inv_target.id] = 0`.
    - Else if `tx.net_cents == residual`: emits `MatchKind.EXACT_REF` (if explicit ref) or `MatchKind.ENTITY_EXACT` with `gross_cents = residual`, `fee_cents = 0`, sets `residual_by_id[inv_target.id] = 0`.
    - Else (`0 < tx.net_cents < residual`): emits `MatchKind.PARTIAL_PAYMENT` with `gross_cents = tx.net_cents`, `fee_cents = 0`, and decrements `residual_by_id[inv_target.id] -= tx.net_cents`.
  - Emits `matches` ordered by original `bank_txs` processing sequence (or `(tx.day_ordinal, tx.id)`), builds `journal_entries` (`ENTRY-0001`, ...), `unpaid_invoices` for all `residual_by_id[inv.id] > 0` sorted by `(inv.day_ordinal, inv.id)`, `audit_chain`, and `trial_balance`.

---

## § 5. Layer 2 — `src/qistas/intake_gate.py`

### 5.1. Allowed Imports (`FF-2 ALLOWLIST`)
`{"__future__", "collections.abc", "dataclasses", "json", "re", "qistas.models", "qistas.normalizer"}`

### 5.2. Types & Functions
- `class IntakeError(ValueError)`: Raised on envelope-level boundary failures (`> 4 MiB`, invalid UTF-8, NUL byte, JSON depth `> 8`, malformed JSON syntax, float/NaN/Infinity in fee schedule, or missing `"qistas.bundle/v1"`).
- `@dataclass(slots=True, frozen=True) class RecordRejection`:
  `record_type: str` (`"invoice"` or `"bank_tx"`), `index: int`, `record_id: str | None`, `code: str`, `detail: str`.
- `parse_decimal_to_cents(s: str) -> int`:
  - Requires `type(s) is str` matching `r"^(0|[1-9][0-9]{0,11})(?:\.([0-9]{1,2}))?\Z"`. Computes `whole * 100 + frac_2` via pure integer math (`1 <= cents <= MAX_AMOUNT_CENTS`). Raises `IntakeError` on violation.
- `ingest_json_bundle_report(raw: str | bytes) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule, tuple[RecordRejection, ...]]`:
  - Enforces 4-stage `F7` gate (`4_194_304` byte limit, iterative depth-8 scanner, `json.loads(..., parse_float=str, parse_constant=...)`, per-record `RecordRejection` isolation including duplicate ID rejection).
- `ingest_json_bundle(raw: str | bytes) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]`:
  - Calls `ingest_json_bundle_report(raw)`; raises `IntakeError` if `len(rejections) > 0`, else returns `(invoices, bank_txs, fee_schedule)`.

---

## § 6. Layer 3 — `src/qistas/exporters.py`, `src/qistas/cli_visualizer.py` & `src/qistas/__init__.py`

### 6.1. `src/qistas/exporters.py`
- **Allowed Imports (`FF-2 ALLOWLIST`):** `{"__future__", "collections.abc", "os", "pathlib", "unicodedata", "duckdb", "openpyxl", "openpyxl.styles", "openpyxl.utils", "openpyxl.worksheet.worksheet", "qistas.models", "qistas.ledger", "qistas.matcher"}`
- `neutralize_formula_cell(val: str) -> str`:
  - Validates `type(val) is str`. Let `norm_s = unicodedata.normalize("NFKC", val).lstrip()`. If `val.startswith(("=", "+", "-", "@", "|", "%", "\t", "\r", "\n"))` or `norm_s.startswith(("=", "+", "-", "@", "|", "%", "\t", "\r", "\n"))`, returns `"'" + val`; else returns `val`.
- `export_xlsx_atomic(result: ReconciliationResult, target_path: str | os.PathLike[str]) -> None`:
  - Builds an `openpyxl.Workbook` with 4 sheets (`Matches`, `JournalEntries`, `TrialBalance`, `UnpaidInvoices`), formats `day_ordinal` via `format_iso_day` in `"date"` columns, appends live `=SUM(...)` `"TOTAL"` rows on `Matches` and `TrialBalance`, styles headers (`#0F172A`), total rows (`#D1FAE5`), borders (`#CBD5E1`), and `#,##0` integer number formats via `_style_worksheet`, applies `neutralize_formula_cell` and sets `cell.data_type = "s"` on every string cell, writes to sibling `.tmp_<name>.<pid>`, flushes + `os.fsync()`, and commits via `os.replace()`.
- `query_duckdb_summary(result: ReconciliationResult) -> dict[str, int]`:
  - Connects to in-memory `duckdb.connect(":memory:")`, creates integer tables, queries analytical totals, and returns `{"matched_tx_count": ..., "total_gross_cents": ..., "total_net_cents": ..., "total_fee_cents": ..., "unpaid_invoice_count": ..., "unpaid_residual_cents": ..., "total_debit_cents": ..., "total_credit_cents": ...}`.

### 6.2. `src/qistas/cli_visualizer.py` & `src/qistas/__init__.py`
- **`cli_visualizer.py` Allowed Imports (`FF-2 ALLOWLIST`):** `{"__future__", "argparse", "collections.abc", "pathlib", "qistas.models", "qistas.normalizer", "qistas.ledger", "qistas.matcher", "qistas.intake_gate", "qistas.exporters"}`
- `format_money(cents: int, currency: str = "EGP") -> str`:
  - Pure integer formatting using `abs_c // 100` and `abs_c % 100`: `f"{sign}{whole:,}.{frac:02d} {currency}"`.
- `build_demo_scenario() -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]`:
  - Returns a deterministic bilingual (Arabic/English) reconciliation scenario exercising all passes.
- `render_reconciliation_report(result: ReconciliationResult, currency: str = "EGP") -> str`:
  - Pure stdlib deterministic ASCII table report of matches, trial balance, unpaid invoices, and audit chain root/tail hashes.
- `main(argv: Sequence[str] | None = None) -> int`:
  - CLI entrypoint supporting `--demo`, `--bundle <path>`, `--export-xlsx <path>`, `--currency <code=EGP>`, returning `0` on valid audit/trial-balance verification.
- **`src/qistas/__init__.py` Allowed Imports:** `{"__future__", "qistas.models", "qistas.normalizer", "qistas.ledger", "qistas.matcher", "qistas.intake_gate", "qistas.exporters", "qistas.cli_visualizer"}`.
