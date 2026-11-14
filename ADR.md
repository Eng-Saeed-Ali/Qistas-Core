# `Qistas-Core` — Architecture Decision Records (`ADR.md`)

**Grounding:** Codex #17 (*Software Architecture: The Hard Parts* — Ford, Richards, Sadalage, Dehghani), Codex #01 (*TigerStyle*), Codex #02 (*NASA JPL Power of 10*), Codex #11 (*OSTEP Ch. 42*), and Codex #12 (*Ousterhout Deep Modules*).

---

## `ADR-001` — 64-Bit Integer Cents & Permille Algebra (`R0.1`, `R0.2`)

- **Status:** Accepted
- **Context:** Financial reconciliation across MENA and international gateways suffers from IEEE-754 binary floating-point representation drift (`10 + 20 ≠ 30` in scaled tenths) and `decimal.Decimal` context-mutation hazards (thread-local rounding mode or precision overrides altering fee calculations across modules). We evaluated three options: (A) `float` with `round()`, (B) `decimal.Decimal` with explicit `quantize()`, and (C) exact 64-bit integer minor units (`cents`, `piastres`, or `halalas`: `int`, scaled `× 100`) paired with integer `permille` (`‰`, parts per `1_000`) and proleptic Gregorian day ordinals (`day_ordinal: int`).
- **Decision:** Adopt Option C (`R0.1` & `R0.2`). Ban `float`, `Decimal`, `Fraction`, and the true-division operator across the entire repository (`FF-1`). Represent all monetary amounts as `int` minor units (`1 <= amount_cents <= 999_999_999_99`), fee rates as `int` permille (`0 <= rate_permille <= 1_000`), and dates as `int` ordinals via `datetime.date.toordinal()`. Compute percentage gateway fees via conservative ceiling integer division:
  `pct_fee_cents = ceil_div(gross_cents * rate_permille, 1000) = -(-(gross_cents * rate_permille) // 1000)` (`⌈(gross_cents × rate_permille) ÷ 1000⌉`).
  Enforce `type(v) is int` inside every `@dataclass(slots=True, frozen=True)` `__post_init__` to defeat Python's `isinstance(True, int) == True` footgun (`F1`).
- **Consequences:**
  - *Positive:* 100% bit-exact arithmetic across x86-64 and ARM64; zero rounding drift; zero dependency on thread-local decimal contexts; statically verifiable via `ast.Walk` (`FF-1`).
  - *Trade-Off Accepted:* Wire boundary strings (`"1250.50"`) and CLI display strings must pass through explicit integer scaling and formatting helpers (`parse_decimal_to_cents`, `format_money`).

---

## `ADR-002` — 5-Stage Arabic & English NFKC & Bidi Normalization (`F2`, `EC-A`)

- **Status:** Accepted
- **Context:** Bilingual Arabic and English bank memos and invoice counterparty names arrive with inconsistent Unicode encodings: Arabic Presentation Forms-B ligatures (`ﻻ U+FEFB` vs `ل + ا`), optional harakat diacritics (`Mn`), invisible Right-to-Left and Left-to-Right Bidi format controls (`Cf`), Kashida or Tatweel (`\u0640`), Persian and Arabic letter variants (`أ, إ, آ, ٱ → ا`, `ى, ئ, ی → ي`, `ک → ك`, `ة → ه`), Eastern Arabic and Persian digits (`٠..٩`, `۰..۹`), and legal entity affixes (`شركة`, `ذ.م.م`, `LLC`, `Ltd`). Naive string comparison causes false-negative reconciliation misses.
- **Decision:** Implement a pure, stateless 5-stage normalization pipeline in `src/qistas/normalizer.py`:
  1. `unicodedata.normalize("NFKC", raw)` to decompose Presentation Forms-B ligatures (`ﻻ → ل + ا`, `ﻷ → ل + أ`) and full-width Latin symbols.
  2. Strip all combining marks and invisible Bidi or format controls (`unicodedata.category(c) not in ("Mn", "Cf")`) and Tatweel (`\u0640`).
  3. Unify Arabic and Persian orthographic variants (`أ, إ, آ, ٱ → ا`, `ى, ئ, ی → ي`, `ک → ك`, `ة → ه`, `ؤ → و`) and map Eastern Arabic and Persian digits to ASCII `0..9`.
  4. Casefold, fold punctuation to spaces, collapse whitespace, and iteratively strip leading/trailing legal entity stop-tokens (`شركه`, `مؤسسه`, `مصنع`, `ذ م م`, `ش م م`, `llc`, `ltd`, `inc`, `co`, `corp`, etc.) **iff** stripping leaves `≥ 1` non-empty core token (`EC-A`).
  5. Extract canonicalized invoice reference codes (`INV-1001`, `FAT-2026-05`) via `extract_ref_codes(memo)`.
- **Consequences:**
  - *Positive:* Resilient, deterministic matching across heterogeneous ERP and bank statement encodings; immune to invisible Bidi spoofing and stop-word-only entity erasure (`EC-A`).
  - *Trade-Off Accepted:* Both raw and normalized entity strings (`raw_entity`, `norm_entity`) are preserved on `Invoice` and `BankTx` so human audit exports retain original legal names.

---

## `ADR-003` — 4-Pass Deterministic Reconciliation & Bounded B&B Subset-Sum (`F3`–`F5`, `EC-B`–`EC-D`)

- **Status:** Accepted
- **Context:** Real-world bank deposits combine single-invoice wires, gateway fee-deducted settlements, weekend clearing delays (`0 ≤ tx.day_ordinal - inv.day_ordinal ≤ max_settlement_lag_days`), `N-to-1` batch payouts (`2 ≤ k ≤ 4`), duplicate payments, partial payments, and overpayments. Unbounded subset-sum is NP-hard (`O(2^N)`), and hash-set iteration order varies with `PYTHONHASHSEED`.
- **Decision:** Execute a strictly ordered 4-Pass matching hierarchy in `src/qistas/matcher.py` (`R0.5`):
  - **Pass 1:** Exact reference match (`extract_ref_codes(tx.memo)`) within `F4` lag window and `F3` fee schedule; detect duplicate payments against already-settled invoices (`EC-D` → `DUPLICATE_PAYMENT_ALERT`).
  - **Pass 2:** 1-to-1 normalized entity match within `F4` and `F3`, ranked by minimal `(fee_error, lag_days, inv.day_ordinal, inv.id)`.
  - **Pass 3:** Iterative explicit-stack Branch-and-Bound subset-sum over `2 ≤ k ≤ MAX_BATCH_SIZE (4)` open invoices of the same `norm_entity`, bounded by `MAX_EXPANSIONS = 10_000` (zero recursion per NASA Rule 1), using suffix-sum underflow pruning (`running_gross + suffix_sum[idx] < tx.net_cents`) and sorted-descending overflow pruning against the Gate 6 admissible `gross_upper` bound:
    `max_fixed = MAX_BATCH_SIZE * max(fee.fixed_fee_cents, fee.min_fee_cents) + fee.tolerance_cents + MAX_BATCH_SIZE`
    `gross_upper = ceil_div(1000 * (tx.net_cents + max_fixed), 1000 - fee.rate_permille)` (for `rate_permille < 1000`).
  - **Pass 4:** Classify remaining unmatched deposits using monotonic `residual_cents` tracking (`PARTIAL_PAYMENT` when `0 < tx.net_cents < residual_cents` decrementing `residual_cents -= tx.net_cents`, `OVERPAYMENT_CREDIT` when `tx.net_cents > residual_cents`, or `ORPHAN_BANK_DEPOSIT` into `SUSPENSE_CLEARING`).
- **Consequences:**
  - *Positive:* Predictable worst-case latency (`≤ 10,000` expansions per batch candidate), zero recursion stack risk, and 100% deterministic tie-breaking across all platforms.
  - *Trade-Off Accepted:* Batch payouts exceeding `MAX_BATCH_SIZE = 4` invoices without explicit reference codes fall through to `Pass 4` suspense review rather than risking exponential search stalls.

---

## `ADR-004` — TigerStyle Double-Entry Conservation & 64-Bit BLAKE2b Audit Chain (`R0.3`, `R0.4`, `F6`)

- **Status:** Accepted
- **Context:** Single-entry reconciliation status flags fail to track gateway fees, customer credit liabilities, or suspense clearing balances and can be silently mutated after matching.
- **Decision:** Every `ReconciliationMatch` generates an immutable `JournalEntry` across `AccountCode` (`AR_RECEIVABLE`, `BANK_CASH`, `GATEWAY_FEE_EXPENSE`, `SUSPENSE_CLEARING`, `CUSTOMER_CREDIT_LIABILITY`) enforcing `∑ debit_cents == ∑ credit_cents > 0` and single-sided XOR posting positivity (`R0.3`). Furthermore, every match appends an `AuditLogEntry` chaining `prev_hash_u64` from `GENESIS_HASH_U64 = 0x5149535441533236` via 8-byte big-endian `hashlib.blake2b(..., digest_size=8)` (`R0.4`).
- **Consequences:**
  - *Positive:* Mathematical conservation of money across all 9 `MatchKind` outcomes; tamper-evident OSTEP Ch. 42 hash chain verifiable in `O(N)` via `verify_audit_chain`.
  - *Trade-Off Accepted:* Every classified bank transaction—including orphan deposits and duplicate payment alerts—must produce a balanced journal entry.

---

## `ADR-005` — CWE-1236 Formula Neutralization, Atomic `.xlsx` Export & DuckDB 2.0 Adapter Isolation (`F7`, `F8`, `EC-E`)

- **Status:** Accepted
- **Context:** External payer names and memos in bank feeds can contain spreadsheet formula payloads (`=CMD|...`, `+SUM(...)`, full-width `＝`, or leading whitespace before `@`/`-`), triggering CWE-1236 CSV/XLSX injection when opened by auditors. Additionally, direct in-place file writes corrupt workbooks on crash, and heavy third-party analytics libraries must not pollute core domain layers.
- **Decision:** Isolate `openpyxl` and `duckdb` strictly inside Layer 3 `src/qistas/exporters.py` (`FF-2`), keeping `cli_visualizer.py` 100% stdlib. Before writing any text cell, `neutralize_formula_cell(val)` inspects both `val` and `unicodedata.normalize("NFKC", val).lstrip()`; if either starts with `("=", "+", "-", "@", "|", "%", "\t", "\r", "\n")`, it prefixes `"'"` and forces `cell.data_type = "s"`. Workbooks are written to `.tmp_<name>.<pid>`, flushed with `os.fsync()`, and atomically swapped via `os.replace()`.
- **Consequences:**
  - *Positive:* Complete neutralization of raw and NFKC/whitespace-obfuscated CWE-1236 payloads; crash-consistent Copy-on-Write persistence; zero third-party coupling in Layers 0–2 and CLI.
  - *Trade-Off Accepted:* Strings legitimately starting with `+` or `-` (e.g., phone numbers or signed notes) receive a leading single-quote escape in exported cells.
