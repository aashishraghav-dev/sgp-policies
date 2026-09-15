"""Post-OCR repair and confidence reporting for scanned circulars.

Why this exists
---------------
These are banking policy documents, so a wrong figure is worse than a
missing one. ``tools/ocr_benchmark.py`` measured three OCR engines against
hand-transcribed ground truth and found one failure that *every* engine
shares: **none of them read the rupee sign.** Measured outputs for a
ground-truth ``₹5,000``::

    tesseract   %5,000   ¥5000   $5000   25,000
    easyocr     <5,000   <10,000
    rapidocr     5,000    10,000          (symbol dropped entirely)

Raising the render scale to 6x and adding Devanagari language data changed
none of it. So this is not an engine-selection problem to be solved by
picking a better engine -- it is a property of the input, and the pipeline
has to handle it explicitly.

What this module does about it
------------------------------
Three tiers, by what can honestly be known:

``REPAIRED``
    The glyph has no valid reading as a numeric prefix (``%``, ``°``, ``~``
    ...). ``%5,000`` is not a number in any notation, so rewriting it to
    ``₹5,000`` cannot destroy a correct reading.

``SUBSTITUTED``
    The glyph *is* a real currency sign or operator (``$``, ``¥``, ``£``,
    ``<``). In an Indian regulator's circular ``₹`` is overwhelmingly the
    intended character, so it is rewritten -- but the original span is
    recorded, so the rewrite is auditable and reversible rather than
    silent.

``UNVERIFIABLE``
    An amount with no currency marker at all. The symbol may have been
    dropped (rapidocr) or absorbed into the digits (tesseract's ``25,000``
    for ``₹5,000``). Nothing in the text distinguishes this from a
    correctly-read bare number, so it is **never** rewritten -- only
    counted, so a reader knows how much of the document is unverified.

Every finding travels with the document into its YAML front matter. That
is the actual guarantee on offer: not that OCR is correct, but that where
it cannot be trusted, the document says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

RUPEE = "₹"

#: Glyphs with no valid reading immediately before a number. Rewriting
#: these cannot turn a correct reading into a wrong one.
_IMPOSSIBLE_PREFIXES = "%°~¤¢*"

#: Real currency signs and operators that OCR substitutes for the rupee
#: sign. Rewritten, but always reported -- ``<5,000`` could in principle
#: have meant "less than 5,000".
_PLAUSIBLE_PREFIXES = "$¥£<>₩₫"

#: An amount: at least three digits, or grouped with separators. Two-digit
#: numbers are section references and clause counts far more often than
#: they are sums of money.
_AMOUNT = r"(?:\d{1,3}(?:[,.]\d{2,3})+(?:\.\d{1,2})?|\d{3,})"

_PREFIXED = re.compile(
    rf"(?P<prefix>[{re.escape(_IMPOSSIBLE_PREFIXES + _PLAUSIBLE_PREFIXES)}])"
    rf"\s?(?P<amount>{_AMOUNT})"
)

#: A bare amount not already carrying a currency marker, and not glued to
#: a reference like ``OC-186/2023-24``. The trailing ``\.`` must allow a
#: sentence-ending period -- only a period that *continues* the number is
#: disqualifying, and forbidding both hid every amount that ends a clause.
_BARE = re.compile(
    rf"(?<![\w/\-{re.escape(RUPEE)}])(?P<amount>{_AMOUNT})(?![\w/\-]|\.\d)"
)

_HAS_MARKER = re.compile(rf"(?:{RUPEE}|\bRs\.?|\bINR\b|\bRupees\b)", re.IGNORECASE)

#: A bare number is only *suspicious as a lost amount* if money is being
#: discussed. Without this the report is swamped by years and circular
#: numbers, and a report nobody can read protects nobody.
_MONEY_CONTEXT = re.compile(
    r"\b(?:amount|limit|limits|cap|capped|ceiling|value|fee|fees|charge|"
    r"charges|penalt(?:y|ies)|fine|balance|sum|worth|exceed(?:s|ing)?|"
    r"maximum|minimum|upto|up\s+to|per\s+transaction|threshold|"
    r"remittance|deposit|withdrawal|"
    # Revision verbs: a circular that changes a figure says so, and the
    # figure it changes is money far more often than it is anything else.
    r"enhanced|revised|raised|increased|reduced|lowered)\b",
    re.IGNORECASE,
)

#: Years read as bare numbers are dates, not money, even in a sentence
#: that also mentions a limit.
_YEAR = re.compile(r"^(?:19|20)\d{2}$")

#: How far back to look for the money cue. One clause, roughly.
_CONTEXT_WINDOW = 80


class Confidence(str, Enum):
    """What is known about one numeric span."""

    REPAIRED = "repaired"
    SUBSTITUTED = "substituted"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Finding:
    """One numeric span OCR could not be trusted on."""

    confidence: Confidence
    original: str
    replacement: str | None
    context: str

    def as_dict(self) -> dict[str, str]:
        record = {
            "confidence": self.confidence.value,
            "original": self.original,
            "context": self.context,
        }
        if self.replacement is not None:
            record["replacement"] = self.replacement
        return record


@dataclass(frozen=True)
class RepairReport:
    """Outcome of a repair pass, summarised for front matter."""

    markdown: str
    findings: list[Finding]

    @property
    def repaired(self) -> int:
        return sum(f.confidence is Confidence.REPAIRED for f in self.findings)

    @property
    def substituted(self) -> int:
        return sum(f.confidence is Confidence.SUBSTITUTED for f in self.findings)

    @property
    def unverifiable(self) -> int:
        return sum(f.confidence is Confidence.UNVERIFIABLE for f in self.findings)

    def summary(self) -> dict[str, object]:
        """The block that goes into the document's front matter.

        Omitted entirely when there is nothing to say, so clean documents
        do not carry noise.
        """
        if not self.findings:
            return {}
        return {
            "currency_repaired": self.repaired,
            "currency_substituted": self.substituted,
            "amounts_unverifiable": self.unverifiable,
            "findings": [f.as_dict() for f in self.findings[:_MAX_REPORTED]],
        }


#: Front matter is read by an agent, not archived. A document with 200
#: unverifiable amounts is described by its counts; listing every span
#: would bury the document itself.
_MAX_REPORTED = 25

_CONTEXT_CHARS = 40


def _context(text: str, start: int, end: int) -> str:
    left = text[max(0, start - _CONTEXT_CHARS) : start].rsplit("\n", 1)[-1]
    right = text[end : end + _CONTEXT_CHARS].split("\n", 1)[0]
    return f"{left}{text[start:end]}{right}".strip()


def apply_repair(markdown: str, *, enabled: bool, origin: str = "") -> tuple[str, dict]:
    """Run the repair pass and return markdown plus a front-matter block.

    Both PDF backends call this, so switching ``conversion.pdf.backend``
    from in-process docling to a docling *service* cannot silently drop the
    accuracy guarantee.
    """
    if not enabled:
        return markdown, {}

    report = repair_currency(markdown)
    if report.findings:
        logger.info(
            "%s: repaired %d currency glyph(s), %d substituted, %d amount(s) unverifiable",
            origin or "document",
            report.repaired,
            report.substituted,
            report.unverifiable,
        )
    return report.markdown, report.summary()


def repair_currency(markdown: str) -> RepairReport:
    """Repair OCR'd currency prefixes and report what stays uncertain.

    Returns the corrected markdown alongside a finding per uncertain span.
    The text is only ever rewritten where a rewrite cannot destroy a
    correct reading, or where the original is preserved in the report.
    """
    findings: list[Finding] = []

    def _replace(match: re.Match[str]) -> str:
        prefix, amount = match.group("prefix"), match.group("amount")
        confidence = (
            Confidence.REPAIRED
            if prefix in _IMPOSSIBLE_PREFIXES
            else Confidence.SUBSTITUTED
        )
        replacement = f"{RUPEE}{amount}"
        findings.append(
            Finding(
                confidence=confidence,
                original=match.group(0),
                replacement=replacement,
                context=_context(markdown, *match.span()),
            )
        )
        return replacement

    repaired = _PREFIXED.sub(_replace, markdown)

    # Bare amounts are counted on the *repaired* text, so a span fixed
    # above is not also reported as unmarked.
    for match in _BARE.finditer(repaired):
        if _YEAR.match(match.group("amount")):
            continue
        before = repaired[max(0, match.start() - _CONTEXT_WINDOW) : match.start()]
        if _HAS_MARKER.search(before[-24:]) or not _MONEY_CONTEXT.search(before):
            continue
        findings.append(
            Finding(
                confidence=Confidence.UNVERIFIABLE,
                original=match.group("amount"),
                replacement=None,
                context=_context(repaired, *match.span()),
            )
        )

    return RepairReport(markdown=repaired, findings=findings)
