from __future__ import annotations

from dataclasses import dataclass

COOKIE_MARKERS = (
    "kp_uid",
    "kp_uidz",
    "kp_uidz-ssn",
    "kick_session",
    "session=",
    "csrf",
    "xsrf",
    "cf_clearance",
    "__cf_bm",
)


@dataclass(frozen=True)
class TokenInfo:
    kind: str
    mode: str
    mask: str
    message: str


def normalize_token(token: str | None) -> str:
    return (token or "").strip()


def looks_like_cookie_token(token: str | None) -> bool:
    text = normalize_token(token)
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in COOKIE_MARKERS):
        return True
    return ";" in text and "=" in text


def mask_token(token: str | None) -> str:
    text = normalize_token(token)
    if not text:
        return ""
    if looks_like_cookie_token(text):
        cookie_name = text.split("=", 1)[0].strip()[:24] or "cookie"
        return f"{cookie_name}=..."
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:6]}...{text[-4:]}"


def token_info(token: str | None) -> TokenInfo:
    text = normalize_token(token)
    if not text:
        return TokenInfo(
            kind="empty",
            mode="invalid",
            mask="",
            message="Brak tokenu.",
        )
    if looks_like_cookie_token(text):
        return TokenInfo(
            kind="cookie",
            mode="local_cookie_test",
            mask=mask_token(text),
            message="Token wyglada jak cookie sesji Kick; test jest tylko lokalny.",
        )
    return TokenInfo(
        kind="api",
        mode="live_kick",
        mask=mask_token(text),
        message="Token nie wyglada jak cookie; bot moze uzyc transportu live_kick.",
    )
