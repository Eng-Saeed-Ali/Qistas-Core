from __future__ import annotations

import re
import unicodedata

_DIGITS_SRC = (
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
)
_DIGIT_TRANS = str.maketrans(_DIGITS_SRC, "01234567890123456789")
_LETTER_TRANS = str.maketrans(
    "\u0623\u0625\u0622\u0671\u0649\u0626\u06cc\u06a9\u0629\u0624",
    "\u0627\u0627\u0627\u0627\u064a\u064a\u064a\u0643\u0647\u0648",
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\u0621-\u064a]+")
_REF_RE = re.compile(
    r"(?<![A-Z0-9])(INV|FAT|REF|BILL|QST)[-_]?([0-9]{2,12}(?:[-_][0-9A-Z]{1,8})?)(?![A-Z0-9])"
)
_STOP_PHRASES: tuple[tuple[str, ...], ...] = (
    ("\u0634", "\u0645", "\u0645"),
    ("\u0630", "\u0645", "\u0645"),
)
_STOP_WORDS: frozenset[str] = frozenset({
    "شركه", "مؤسسه", "موسسه", "مصنع", "مكتب", "مجموعه", "مساهمه", "محدوده",
    "llc", "ltd", "inc", "co", "corp", "limited", "company", "plc",
})


def _clean_codepoints(raw: str, *, map_letters: bool) -> str:
    if type(raw) is not str:
        raise TypeError("raw must be str")
    if "\x00" in raw:
        raise ValueError("NUL byte not allowed")
    nfkc = unicodedata.normalize("NFKC", raw)
    stripped = "".join(
        ch for ch in nfkc
        if unicodedata.category(ch) not in ("Mn", "Cf") and ch != "\u0640"
    )
    out = stripped.translate(_DIGIT_TRANS)
    if map_letters:
        out = out.translate(_LETTER_TRANS)
    assert type(out) is str
    assert "\x00" not in out
    return out


def normalize_text(raw: str) -> str:
    cleaned = _clean_codepoints(raw, map_letters=True).casefold()
    res = " ".join(_NON_ALNUM_RE.sub(" ", cleaned).split())
    assert type(res) is str
    assert not res or (not res[0].isspace() and not res[-1].isspace())
    return res


def normalize_entity(raw: str) -> str:
    tokens = normalize_text(raw).split()
    for _ in range(len(tokens) + 1):
        changed = False
        for phrase in _STOP_PHRASES:
            n = len(phrase)
            if len(tokens) > n and tuple(tokens[:n]) == phrase:
                tokens = tokens[n:]
                changed = True
            elif len(tokens) > n and tuple(tokens[-n:]) == phrase:
                tokens = tokens[:-n]
                changed = True
        if len(tokens) > 1 and tokens[0] in _STOP_WORDS:
            tokens = tokens[1:]
            changed = True
        elif len(tokens) > 1 and tokens[-1] in _STOP_WORDS:
            tokens = tokens[:-1]
            changed = True
        if not changed:
            break
    res = " ".join(tokens)
    assert type(res) is str
    assert bool(res) == bool(tokens)
    return res


def extract_ref_codes(memo: str) -> tuple[str, ...]:
    cleaned = _clean_codepoints(memo, map_letters=False).upper()
    seen: set[str] = set()
    out: list[str] = []
    for match in _REF_RE.finditer(cleaned):
        code = f"{match.group(1)}-{match.group(2).replace('_', '-')}"
        if code not in seen:
            seen.add(code)
            out.append(code)
    res = tuple(out)
    assert type(res) is tuple
    assert len(res) == len(seen)
    return res
