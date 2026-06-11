from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)


@dataclass
class RedactionRule:
    name: str
    pattern: str
    replacement: str


@dataclass
class RedactionConfig:
    enabled: bool = False
    rules: list[RedactionRule] = field(default_factory=list)


DEFAULT_RULES = [
    RedactionRule(
        name="api_key",
        pattern=r"\bsk-[A-Za-z0-9\-]+\b",
        replacement="[REDACTED_API_KEY]"
    ),
    RedactionRule(
        name="bearer_token",
        pattern=r"\bBearer\s+[A-Za-z0-9\-\._~\+\/]+=*",
        replacement="Bearer [REDACTED_TOKEN]"
    ),
    RedactionRule(
        name="password",
        pattern=r"(?i)\b(password|passwd|pwd)\s*([:=])\s*(['\"]?)([^\s'\"]+)\3",
        replacement=r"\1\2\3[REDACTED_PASSWORD]\3"
    ),
    RedactionRule(
        name="secret",
        pattern=r"(?i)\b(secret|private_key|privatekey|secret_key|api_secret)\s*([:=])\s*(['\"]?)([^\s'\"]+)\3",
        replacement=r"\1\2\3[REDACTED_SECRET]\3"
    )
]


def redact_text(text: str, config: RedactionConfig) -> str:
    """Redact sensitive information from text according to configured rules."""
    if not config.enabled or not text:
        return text

    redacted = text
    for rule in config.rules:
        try:
            compiled = re.compile(rule.pattern)
            redacted = compiled.sub(rule.replacement, redacted)
        except re.error as exc:
            LOGGER.warning("Invalid regex pattern '%s' in redaction rule '%s': %s", rule.pattern, rule.name, exc)
    return redacted


def load_redaction_config() -> RedactionConfig:
    """Load redaction configuration from environment variables."""
    enabled_str = os.environ.get("WAGGLE_REDACTION_ENABLED", "false").strip().lower()
    enabled = enabled_str in ("true", "1", "yes")

    rules_json = os.environ.get("WAGGLE_REDACTION_RULES_JSON")
    rules = []

    if rules_json:
        try:
            parsed = json.loads(rules_json)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "name" in item and "pattern" in item and "replacement" in item:
                        rules.append(
                            RedactionRule(
                                name=str(item["name"]),
                                pattern=str(item["pattern"]),
                                replacement=str(item["replacement"])
                            )
                        )
            else:
                LOGGER.warning("WAGGLE_REDACTION_RULES_JSON must be a JSON array. Falling back to default rules.")
                rules = list(DEFAULT_RULES)
        except Exception as exc:
            LOGGER.warning("Failed to parse WAGGLE_REDACTION_RULES_JSON: %s. Falling back to default rules.", exc)
            rules = list(DEFAULT_RULES)
    else:
        rules = list(DEFAULT_RULES)

    return RedactionConfig(enabled=enabled, rules=rules)
