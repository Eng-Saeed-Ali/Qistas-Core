# `Qistas-Core` — Master Symbol Registry (SSOT) & Relay Handoff (`HANDOFF.md`)

**Project:** `qistas-core` (`1.0.0`)  
**Workspace Root:** `C:\projects\Qistas_Core`  
**Current Relay State:** Step 6-B & Full Zero-Trust 6-Axis Audit Complete (`100%` Verified & Locked in `FF-1..FF-8`).

---

## § 1. Locked Gate 6 Refinements (Mandatory Across All Coding Steps)

1. **Zero `rich` Dependency in `cli_visualizer.py`:**
   `src/qistas/cli_visualizer.py` uses **100% Python Standard Library** (`argparse`, `collections.abc`, `pathlib`) and pure deterministic ASCII formatting. Only `src/qistas/exporters.py` imports `openpyxl` and `duckdb`.
2. **Admissible `gross_upper` Bound in `Pass 3` (`src/qistas/matcher.py`):**
   - If `fee.rate_permille == 0 and fee.fixed_fee_cents == 0 and fee.min_fee_cents == 0`:
     `gross_upper = tx.net_cents + fee.tolerance_cents`
   - Else (with `fee.rate_permille < 1000`):
     `max_fixed = MAX_BATCH_SIZE * max(fee.fixed_fee_cents, fee.min_fee_cents) + fee.tolerance_cents + MAX_BATCH_SIZE`
     `gross_upper = ceil_div(1000 * (tx.net_cents + max_fixed), 1000 - fee.rate_permille)`
   - Else (`fee.rate_permille == 1000`):
     `gross_upper = MAX_BATCH_SIZE * MAX_AMOUNT_CENTS`
3. **Monotonic Residual Tracking in `Pass 4` (`src/qistas/matcher.py`):**
   Track open invoice `residual_cents` (initialized to `inv.amount_cents`) across `Pass 4`. Each partial payment targeting an open invoice checks `0 < tx.net_cents < residual_cents` and decrements `residual_cents -= tx.net_cents` (or triggers `OVERPAYMENT_CREDIT` if `tx.net_cents > residual_cents`, or settles exact residual if `tx.net_cents == residual_cents`).

---

## § 2. Master Symbol Registry (Single Source of Truth)

### 2.1. `src/qistas/models.py` (Layer 0)
- **Allowed Imports (`FF-2`):** `{"__future__", "datetime", "dataclasses", "enum", "re"}`
- **Constants:**
  - `MAX_AMOUNT_CENTS: int = 999_999_999_99`
  - `MAX_RATE_PERMILLE: int = 1_000`
  - `MAX_LAG_DAYS_LIMIT: int = 365`
  - `MAX_BATCH_SIZE: int = 4`
  - `MAX_EXPANSIONS: int = 10_000`
  - `GENESIS_HASH_U64: int = 0x5149535441533236`
- **Enums:**
  - `AccountCode(StrEnum)`: `AR_RECEIVABLE`, `BANK_CASH`, `GATEWAY_FEE_EXPENSE`, `SUSPENSE_CLEARING`, `CUSTOMER_CREDIT_LIABILITY`
  - `MatchKind(StrEnum)`: `EXACT_REF`, `FEE_ADJUSTED_REF`, `ENTITY_EXACT`, `ENTITY_FEE_ADJUSTED`, `BATCH_SUBSET_SUM`, `PARTIAL_PAYMENT`, `OVERPAYMENT_CREDIT`, `DUPLICATE_PAYMENT_ALERT`, `ORPHAN_BANK_DEPOSIT`
- **Functions:**
  - `ceil_div(n: int, d: int) -> int`
  - `parse_iso_day(s: str) -> int`
  - `format_iso_day(day_ordinal: int) -> str`
- **Dataclasses (`@dataclass(slots=True, frozen=True)` — `8` classes):**
  1. `FeeSchedule(fixed_fee_cents: int = 0, rate_permille: int = 0, min_fee_cents: int = 0, max_fee_cents: int = 0, tolerance_cents: int = 0, max_settlement_lag_days: int = 5)`
  2. `Invoice(id: str, raw_entity: str, norm_entity: str, amount_cents: int, day_ordinal: int, ref_code: str = "")`
  3. `BankTx(id: str, raw_payer: str, norm_entity: str, net_cents: int, day_ordinal: int, memo: str = "")`
  4. `LedgerPosting(account: AccountCode, debit_cents: int, credit_cents: int, ref_id: str)`
  5. `JournalEntry(entry_id: str, day_ordinal: int, match_kind: MatchKind, postings: tuple[LedgerPosting, ...], memo: str = "")`
  6. `ReconciliationMatch(tx_id: str, match_kind: MatchKind, invoice_ids: tuple[str, ...], gross_cents: int, net_cents: int, fee_cents: int, day_ordinal: int)`
  7. `UnpaidInvoiceRecord(invoice_id: str, residual_cents: int, day_ordinal: int, norm_entity: str)`
  8. `AuditLogEntry(seq_no: int, match_kind: MatchKind, primary_id: str, secondary_ids: tuple[str, ...], gross_cents: int, net_cents: int, fee_cents: int, day_ordinal: int, prev_hash_u64: int, chain_hash_u64: int)`

### 2.2. `src/qistas/normalizer.py` (Layer 1)
- **Allowed Imports (`FF-2`):** `{"__future__", "re", "unicodedata", "qistas.models"}`
- **Functions:**
  - `normalize_text(raw: str) -> str`
  - `normalize_entity(raw: str) -> str`
  - `extract_ref_codes(memo: str) -> tuple[str, ...]`

### 2.3. `src/qistas/ledger.py` (Layer 1)
- **Allowed Imports (`FF-2`):** `{"__future__", "collections.abc", "dataclasses", "hashlib", "qistas.models"}`
- **Dataclass (`@dataclass(slots=True, frozen=True)` — `1` class):**
  9. `TrialBalance(debits_by_account: tuple[tuple[AccountCode, int], ...], credits_by_account: tuple[tuple[AccountCode, int], ...], total_debit_cents: int, total_credit_cents: int)`
- **Functions:**
  - `expected_gateway_fee(gross_cents: int, fee: FeeSchedule) -> int`
  - `fee_matches(gross_cents: int, net_cents: int, fee: FeeSchedule) -> tuple[bool, int]`
  - `build_journal_entry(entry_id: str, match: ReconciliationMatch) -> JournalEntry`
  - `compute_trial_balance(entries: Sequence[JournalEntry]) -> TrialBalance`
  - `compute_audit_hash(seq_no: int, match: ReconciliationMatch, prev_hash_u64: int) -> int`
  - `build_audit_chain(matches: Sequence[ReconciliationMatch]) -> tuple[AuditLogEntry, ...]`
  - `verify_audit_chain(chain: Sequence[AuditLogEntry]) -> bool`

### 2.4. `src/qistas/matcher.py` (Layer 2)
- **Allowed Imports (`FF-2`):** `{"__future__", "collections.abc", "dataclasses", "qistas.models", "qistas.normalizer", "qistas.ledger"}`
- **Dataclass (`@dataclass(slots=True, frozen=True)` — `1` class):**
  10. `ReconciliationResult(matches: tuple[ReconciliationMatch, ...], journal_entries: tuple[JournalEntry, ...], unpaid_invoices: tuple[UnpaidInvoiceRecord, ...], audit_chain: tuple[AuditLogEntry, ...], trial_balance: TrialBalance, expansions: int, proven_optimal_batches: bool = True)`
- **Functions:**
  - `solve_subset_sum_batch(candidates: Sequence[Invoice], tx: BankTx, fee: FeeSchedule, max_expansions: int = MAX_EXPANSIONS) -> tuple[tuple[Invoice, ...], int, int, bool]`
  - `reconcile(invoices: Sequence[Invoice], bank_txs: Sequence[BankTx], fee: FeeSchedule = FeeSchedule(), max_expansions: int = MAX_EXPANSIONS) -> ReconciliationResult`

### 2.5. `src/qistas/intake_gate.py` (Layer 2)
- **Allowed Imports (`FF-2`):** `{"__future__", "collections.abc", "dataclasses", "json", "re", "qistas.models", "qistas.normalizer"}`
- **Exception & Dataclass (`@dataclass(slots=True, frozen=True)` — `1` class):**
  - `class IntakeError(ValueError)`
  11. `RecordRejection(record_type: str, index: int, record_id: str | None, code: str, detail: str)`
- **Functions:**
  - `parse_decimal_to_cents(s: str) -> int`
  - `ingest_json_bundle_report(raw: str | bytes) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule, tuple[RecordRejection, ...]]`
  - `ingest_json_bundle(raw: str | bytes) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]`

### 2.6. `src/qistas/exporters.py` (Layer 3)
- **Allowed Imports (`FF-2`):** `{"__future__", "collections.abc", "os", "pathlib", "unicodedata", "duckdb", "openpyxl", "openpyxl.styles", "openpyxl.utils", "openpyxl.worksheet.worksheet", "qistas.models", "qistas.ledger", "qistas.matcher"}`
- **Functions:**
  - `neutralize_formula_cell(val: str) -> str`
  - `export_xlsx_atomic(result: ReconciliationResult, target_path: str | os.PathLike[str]) -> None`
  - `query_duckdb_summary(result: ReconciliationResult) -> dict[str, int]`

### 2.7. `src/qistas/cli_visualizer.py` & `src/qistas/__init__.py` (Layer 3 & Public API)
- **`cli_visualizer.py` Allowed Imports (`FF-2`):** `{"__future__", "argparse", "collections.abc", "pathlib", "qistas.models", "qistas.normalizer", "qistas.ledger", "qistas.matcher", "qistas.intake_gate", "qistas.exporters"}`
- **Functions:**
  - `format_money(cents: int, currency: str = "EGP") -> str`
  - `build_demo_scenario() -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]`
  - `render_reconciliation_report(result: ReconciliationResult, currency: str = "EGP") -> str`
  - `main(argv: Sequence[str] | None = None) -> int`
- **`__init__.py` Allowed Imports (`FF-2`):** `{"__future__", "qistas.models", "qistas.normalizer", "qistas.ledger", "qistas.matcher", "qistas.intake_gate", "qistas.exporters", "qistas.cli_visualizer"}`

---

## § 3. 7-Step Execution Tracker

| Step | Phase / Milestone | Target Artifacts | Status |
| :---: | :--- | :--- | :---: |
| `0-A` | Mathematical & Architectural Blueprint | `BLUEPRINT.md` | ✅ COMPLETE |
| `0-B` | Normative Contracts, Registry, ADRs & Packaging | `SPEC.md`, `HANDOFF.md`, `ADR.md`, `pyproject.toml`, `.gitignore` | ✅ COMPLETE |
| `1` | Layer 0 & Layer 1 Text Normalization | `src/qistas/models.py`, `src/qistas/normalizer.py`, `tests/test_models_and_normalizer.py` | ✅ COMPLETE |
| `2` | Layer 1 Ledger & Layer 2 4-Pass Matcher | `src/qistas/ledger.py`, `src/qistas/matcher.py`, `tests/test_matcher_and_ledger.py` | ✅ COMPLETE |
| `3` | Layer 2 Intake Gate & Layer 3 Exporters | `src/qistas/intake_gate.py`, `src/qistas/exporters.py`, `tests/test_intake_and_exporters.py` | ✅ COMPLETE |
| `4` | Layer 3 CLI Visualizer & Public Package Facade | `src/qistas/cli_visualizer.py`, `src/qistas/__init__.py` | ✅ COMPLETE |
| `5` | Reference Oracle, AST Fitness Suite & Final Audit | `tests/_oracle.py`, `tests/test_architecture_fitness.py`, full `pytest` verification | ✅ COMPLETE |
| `6` | Adversarial Audit, Oracle Hardening, Vector SVGs, `LICENSE` & `README.md` | `assets/*.svg`, `LICENSE`, `README.md`, `FF-8`, 4-tier verification | ✅ COMPLETE |

---

## § 4. Final Verified Repository & LOC Inventory

- **Production Core (`src/qistas/` — 8 modules):** `2,048` lines (`models.py`: 226, `normalizer.py`: 93, `ledger.py`: 261, `matcher.py`: 403, `intake_gate.py`: 392, `exporters.py`: 283, `cli_visualizer.py`: 270, `__init__.py`: 120)
- **Verification & Brute-Force Oracle Suite (`tests/` — 5 modules):** `1,593` lines (`_oracle.py`: 285, `test_architecture_fitness.py`: 307, `test_intake_and_exporters.py`: 458, `test_matcher_and_ledger.py`: 290, `test_models_and_normalizer.py`: 253)
- **Architecture Diagrams (`assets/` — 2 SVGs):** `213` lines (`architecture_pipeline.svg`: 108, `reconciliation_ledger_flow.svg`: 105)
- **Specifications, Docs & Packaging:** `BLUEPRINT.md` (212), `SPEC.md` (311), `ADR.md` (71), `HANDOFF.md` (132), `README.md` (225), `LICENSE` (21), `pyproject.toml` (48), `.gitignore` (25)
- **Verification Gates:** `110 passed` (`pytest -v`), `0` lint issues (`ruff check src/ tests/`), `0` type issues (`ty check src/`)
