# PII facade: regex-only redaction for EMAIL / PHONE / AADHAAR / PAN.
# Names (PERSON) are NOT treated as PII — students are addressed by name in
# the assistant's replies, so name detection was removed.
from dataclasses import dataclass

from app.src.pii.patterns import PATTERNS


@dataclass(frozen=True)
class PIIHit:
    category: str
    start: int
    end: int
    original: str


class PII:
    def redact(self, text: str) -> tuple[str, list[PIIHit]]:
        if not text:
            return text, []
        hits: list[PIIHit] = []

        for category, pat in PATTERNS.items():
            for m in pat.finditer(text):
                hits.append(
                    PIIHit(
                        category=category,
                        start=m.start(),
                        end=m.end(),
                        original=m.group(0),
                    )
                )

        if not hits:
            return text, []

        # apply replacements right-to-left so spans stay valid
        hits.sort(key=lambda h: h.start, reverse=True)
        redacted = text
        for h in hits:
            redacted = redacted[: h.start] + f"<PII:{h.category}>" + redacted[h.end:]
        hits.sort(key=lambda h: h.start)
        return redacted, hits

    @staticmethod
    def categories(hits: list[PIIHit]) -> list[str]:
        # de-duplicated, sorted list of categories for eval/log emission
        return sorted({h.category for h in hits})
