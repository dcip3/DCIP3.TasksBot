"""User-friendly error helpers."""

from typing import Optional


def describe_error(exc: Exception) -> Optional[str]:
    text = str(exc).lower()

    if any(token in text for token in ("access_token", "invalid access token", "unauthorized", "401")):
        return "Authorization failed. Please check the credentials."
    if "429" in text or "rate limit" in text:
        return "Rate limit reached. Please try again in a few minutes."
    if "timeout" in text or "timed out" in text:
        return "Network timeout. Please try again."
    if "ssl" in text or "tls" in text:
        return "Secure connection error. Please try again."
    if "not found" in text:
        return "The requested file was not found."

    return None
