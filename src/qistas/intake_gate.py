from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from qistas.models import (
    MAX_AMOUNT_CENTS,
    BankTx,
    FeeSchedule,
    Invoice,
    parse_iso_day,
)
from qistas.normalizer import normalize_entity

MAX_PAYLOAD_BYTES: int = 4_194_304
MAX_JSON_DEPTH: int = 8
MAX_RECORDS: int = 10_000
MAX_INT_DIGITS: int = 15

_DEC_RE: re.Pattern[str] = re.compile(r"^(0|[1-9][0-9]{0,11})(?:\.([0-9]{1,2}))?\Z")
_ROOT_ALLOWED: frozenset[str] = frozenset({
    "schema_version", "fee_schedule", "invoices", "bank_txs"
})
_ROOT_REQUIRED: frozenset[str] = frozenset({"schema_version", "invoices", "bank_txs"})
_FEE_ALLOWED: frozenset[str] = frozenset({
    "fixed_fee_cents",
    "rate_permille",
    "min_fee_cents",
    "max_fee_cents",
    "tolerance_cents",
    "max_settlement_lag_days",
})
_INV_ALLOWED: frozenset[str] = frozenset({"id", "entity", "amount", "date", "ref_code"})
_INV_REQUIRED: frozenset[str] = frozenset({"id", "entity", "amount", "date"})
_TX_ALLOWED: frozenset[str] = frozenset({"id", "payer", "net_amount", "date", "memo"})
_TX_REQUIRED: frozenset[str] = frozenset({"id", "payer", "net_amount", "date"})


class IntakeError(ValueError):
    """Raised on envelope-level intake gate failures."""


@dataclass(slots=True, frozen=True)
class RecordRejection:
    record_type: str
    index: int
    record_id: str | None
    code: str
    detail: str

    def __post_init__(self) -> None:
        if self.record_type not in ("invoice", "bank_tx"):
            raise ValueError("record_type must be 'invoice' or 'bank_tx'")
        if type(self.index) is not int or self.index < 0:
            raise ValueError("index must be non-negative int")
        if self.record_id is not None and type(self.record_id) is not str:
            raise TypeError("record_id must be str or None")
        if type(self.code) is not str or not self.code or "\x00" in self.code:
            raise ValueError("invalid rejection code")
        if type(self.detail) is not str or not self.detail or "\x00" in self.detail:
            raise ValueError("invalid rejection detail")
        assert self.record_type in ("invoice", "bank_tx")
        assert self.index >= 0


def parse_decimal_to_cents(s: str) -> int:
    if type(s) is not str:
        raise IntakeError("monetary string must be str")
    match = _DEC_RE.match(s)
    if match is None:
        raise IntakeError(f"invalid monetary decimal string: {s!r}")
    whole_str = match.group(1)
    frac_str = match.group(2)
    frac_cents = int(frac_str.ljust(2, "0")) if frac_str is not None else 0
    cents = int(whole_str) * 100 + frac_cents
    if cents < 1 or cents > MAX_AMOUNT_CENTS:
        raise IntakeError("monetary amount out of allowed cents range")
    assert type(cents) is int
    assert 1 <= cents <= MAX_AMOUNT_CENTS
    return cents


def _stage1_decode_utf8(raw: str | bytes) -> str:
    if type(raw) is str:
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise IntakeError("payload exceeds MAX_PAYLOAD_BYTES")
        try:
            raw_bytes = raw.encode()
        except UnicodeEncodeError as exc:
            raise IntakeError("invalid UTF-8 surrogate in str payload") from exc
        if len(raw_bytes) > MAX_PAYLOAD_BYTES:
            raise IntakeError("UTF-8 encoded payload exceeds MAX_PAYLOAD_BYTES")
        text = raw
    elif type(raw) is bytes:
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise IntakeError("byte payload exceeds MAX_PAYLOAD_BYTES")
        try:
            text = raw.decode()
        except UnicodeDecodeError as exc:
            raise IntakeError("invalid UTF-8 bytes in payload") from exc
    else:
        raise IntakeError("payload must be str or bytes")
    if "\x00" in text:
        raise IntakeError("NUL byte forbidden in payload")
    assert type(text) is str
    assert "\x00" not in text
    return text


def _stage2_scan_brackets(text: str) -> None:
    in_string = False
    escaped = False
    stack: list[str] = []
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            if len(stack) > MAX_JSON_DEPTH:
                raise IntakeError("JSON nesting depth exceeds MAX_JSON_DEPTH")
        elif ch in "}]" and (not stack or stack.pop() != ch):
            raise IntakeError("mismatched JSON closing bracket")
    if in_string or stack:
        raise IntakeError("unclosed string or bracket in JSON payload")
    assert not in_string
    assert not stack


def _bounded_int(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > MAX_INT_DIGITS:
        raise IntakeError("integer token exceeds MAX_INT_DIGITS")
    val = int(token)
    assert type(val) is int
    assert len(digits) <= MAX_INT_DIGITS
    return val


def _reject_const(token: str) -> object:
    assert type(token) is str
    assert len(token) >= 1
    raise IntakeError(f"forbidden non-finite JSON constant: {token!r}")


def _no_dup_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, val in pairs:
        if key in out:
            raise IntakeError(f"duplicate JSON object key: {key!r}")
        out[key] = val
    assert type(out) is dict
    assert len(out) == len(pairs)
    return out


def _stage3_loads_json(text: str) -> dict[str, object]:
    try:
        loaded: object = json.loads(
            text,
            parse_float=str,
            parse_int=_bounded_int,
            parse_constant=_reject_const,
            object_pairs_hook=_no_dup_keys,
        )
    except json.JSONDecodeError as exc:
        raise IntakeError(f"malformed JSON syntax: {exc.msg}") from exc
    if type(loaded) is not dict:
        raise IntakeError("JSON root must be an object")
    out: dict[str, object] = {k: v for k, v in loaded.items() if type(k) is str}
    assert type(out) is dict
    assert len(out) == len(loaded)
    return out


def _parse_fee_schedule(raw_fee: object) -> FeeSchedule:
    if raw_fee is None:
        fee = FeeSchedule()
    else:
        if type(raw_fee) is not dict:
            raise IntakeError("fee_schedule must be a JSON object")
        keys = set(raw_fee.keys())
        if not keys.issubset(_FEE_ALLOWED):
            raise IntakeError("unknown keys in fee_schedule")
        int_map: dict[str, int] = {}
        for k, val in raw_fee.items():
            if type(k) is not str or type(val) is not int:
                raise IntakeError("fee_schedule values must be exact integers")
            int_map[k] = val
        try:
            fee = FeeSchedule(
                fixed_fee_cents=int_map.get("fixed_fee_cents", 0),
                rate_permille=int_map.get("rate_permille", 0),
                min_fee_cents=int_map.get("min_fee_cents", 0),
                max_fee_cents=int_map.get("max_fee_cents", 0),
                tolerance_cents=int_map.get("tolerance_cents", 0),
                max_settlement_lag_days=int_map.get("max_settlement_lag_days", 5),
            )
        except (TypeError, ValueError) as exc:
            raise IntakeError(f"invalid fee_schedule values: {exc}") from exc
    assert isinstance(fee, FeeSchedule)
    assert fee.max_fee_cents == 0 or fee.max_fee_cents >= fee.min_fee_cents
    return fee


def _parse_amount_field(val: object) -> int:
    if type(val) is str:
        cents = parse_decimal_to_cents(val)
    elif type(val) is int:
        cents = parse_decimal_to_cents(str(val))
    else:
        raise IntakeError("amount field must be decimal string or major integer")
    assert type(cents) is int
    assert 1 <= cents <= MAX_AMOUNT_CENTS
    return cents


def _validate_record_shape(
    item: object,
    allowed: frozenset[str],
    required: frozenset[str],
    seen_ids: set[str],
) -> tuple[dict[str, object], str]:
    if type(item) is not dict:
        raise IntakeError("INVALID_RECORD_TYPE: record must be JSON object")
    rec: dict[str, object] = {k: v for k, v in item.items() if type(k) is str}
    keys = set(rec.keys())
    if not keys.issubset(allowed) or len(rec) != len(item):
        raise IntakeError("UNKNOWN_KEYS: unexpected fields in record")
    if not required.issubset(keys):
        raise IntakeError("MISSING_REQUIRED_KEYS: required fields missing")
    rid = rec.get("id")
    if type(rid) is not str or not rid:
        raise IntakeError("INVALID_ID: id must be non-empty string")
    if rid in seen_ids:
        raise IntakeError("DUPLICATE_ID: duplicate record id in bundle")
    assert type(rid) is str
    assert type(rec) is dict
    return rec, rid


def _make_rejection(record_type: str, idx: int, item: object, exc: Exception) -> RecordRejection:
    rid_raw = item.get("id") if type(item) is dict else None
    rid: str | None = rid_raw if type(rid_raw) is str else None
    msg = str(exc) or "invalid record"
    code = msg.split(":", 1)[0] if ":" in msg else "INVALID_FIELD"
    detail = msg.split(":", 1)[1].strip() if ":" in msg else msg
    rej = RecordRejection(
        record_type=record_type,
        index=idx,
        record_id=rid,
        code=code,
        detail=detail or msg,
    )
    assert isinstance(rej, RecordRejection)
    assert rej.index == idx
    return rej


def _parse_single_invoice(
    item: object, idx: int, seen_ids: set[str]
) -> Invoice | RecordRejection:
    try:
        rec, rid = _validate_record_shape(item, _INV_ALLOWED, _INV_REQUIRED, seen_ids)
        raw_entity = rec["entity"]
        if type(raw_entity) is not str or not raw_entity.strip():
            raise IntakeError("INVALID_ENTITY: entity must be non-empty string")
        norm_entity = normalize_entity(raw_entity)
        if not norm_entity:
            raise IntakeError("INVALID_ENTITY: entity normalized to empty")
        cents = _parse_amount_field(rec["amount"])
        date_raw = rec["date"]
        if type(date_raw) is not str:
            raise IntakeError("INVALID_DATE: date must be string")
        day = parse_iso_day(date_raw)
        ref_raw = rec.get("ref_code")
        if ref_raw is not None and type(ref_raw) is not str:
            raise IntakeError("INVALID_REF_CODE: ref_code must be string")
        ref_code = ref_raw.strip() if type(ref_raw) is str else ""
        inv = Invoice(
            id=rid, raw_entity=raw_entity, norm_entity=norm_entity,
            amount_cents=cents, day_ordinal=day, ref_code=ref_code,
        )
        seen_ids.add(inv.id)
    except (IntakeError, TypeError, ValueError) as exc:
        return _make_rejection("invoice", idx, item, exc)
    assert isinstance(inv, Invoice)
    assert inv.id in seen_ids
    return inv


def _parse_single_bank_tx(
    item: object, idx: int, seen_ids: set[str]
) -> BankTx | RecordRejection:
    try:
        rec, rid = _validate_record_shape(item, _TX_ALLOWED, _TX_REQUIRED, seen_ids)
        raw_payer = rec["payer"]
        if type(raw_payer) is not str or not raw_payer.strip():
            raise IntakeError("INVALID_PAYER: payer must be non-empty string")
        norm_entity = normalize_entity(raw_payer)
        cents = _parse_amount_field(rec["net_amount"])
        date_raw = rec["date"]
        if type(date_raw) is not str:
            raise IntakeError("INVALID_DATE: date must be string")
        day = parse_iso_day(date_raw)
        memo_raw = rec.get("memo")
        if memo_raw is not None and type(memo_raw) is not str:
            raise IntakeError("INVALID_MEMO: memo must be string")
        memo = memo_raw if type(memo_raw) is str else ""
        tx = BankTx(
            id=rid, raw_payer=raw_payer, norm_entity=norm_entity,
            net_cents=cents, day_ordinal=day, memo=memo,
        )
        seen_ids.add(tx.id)
    except (IntakeError, TypeError, ValueError) as exc:
        return _make_rejection("bank_tx", idx, item, exc)
    assert isinstance(tx, BankTx)
    assert tx.id in seen_ids
    return tx


def _validate_root_envelope(
    root: dict[str, object],
) -> tuple[list[object], list[object], FeeSchedule]:
    keys = set(root.keys())
    if not keys.issubset(_ROOT_ALLOWED) or not _ROOT_REQUIRED.issubset(keys):
        raise IntakeError("invalid root envelope keys")
    if root.get("schema_version") != "qistas.bundle/v1":
        raise IntakeError("schema_version must equal 'qistas.bundle/v1'")
    raw_invs = root["invoices"]
    raw_txs = root["bank_txs"]
    if type(raw_invs) is not list or type(raw_txs) is not list:
        raise IntakeError("invoices and bank_txs must be JSON arrays")
    invs: list[object] = list(raw_invs)
    txs: list[object] = list(raw_txs)
    if len(invs) + len(txs) > MAX_RECORDS:
        raise IntakeError("total record count exceeds MAX_RECORDS")
    fee = _parse_fee_schedule(root.get("fee_schedule"))
    assert type(invs) is list
    assert len(invs) + len(txs) <= MAX_RECORDS
    return invs, txs, fee


def ingest_json_bundle_report(
    raw: str | bytes,
) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule, tuple[RecordRejection, ...]]:
    text = _stage1_decode_utf8(raw)
    _stage2_scan_brackets(text)
    root = _stage3_loads_json(text)
    raw_invs, raw_txs, fee = _validate_root_envelope(root)
    invoices: list[Invoice] = []
    bank_txs: list[BankTx] = []
    rejections: list[RecordRejection] = []
    seen_inv_ids: set[str] = set()
    seen_tx_ids: set[str] = set()
    for idx, item in enumerate(raw_invs):
        res_inv = _parse_single_invoice(item, idx, seen_inv_ids)
        if isinstance(res_inv, Invoice):
            invoices.append(res_inv)
        else:
            rejections.append(res_inv)
    for idx, item in enumerate(raw_txs):
        res_tx = _parse_single_bank_tx(item, idx, seen_tx_ids)
        if isinstance(res_tx, BankTx):
            bank_txs.append(res_tx)
        else:
            rejections.append(res_tx)
    assert len(invoices) + len(bank_txs) + len(rejections) == len(raw_invs) + len(raw_txs)
    assert len(invoices) == len(seen_inv_ids) and len(bank_txs) == len(seen_tx_ids)
    return tuple(invoices), tuple(bank_txs), fee, tuple(rejections)


def ingest_json_bundle(
    raw: str | bytes,
) -> tuple[tuple[Invoice, ...], tuple[BankTx, ...], FeeSchedule]:
    invoices, bank_txs, fee, rejections = ingest_json_bundle_report(raw)
    if rejections:
        first = rejections[0]
        raise IntakeError(
            f"bundle contains {len(rejections)} rejected record(s): {first.code} ({first.detail})"
        )
    assert type(invoices) is tuple and type(bank_txs) is tuple
    assert isinstance(fee, FeeSchedule)
    return invoices, bank_txs, fee
