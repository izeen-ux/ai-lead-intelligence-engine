"""Adapt a DOMAIN JSON Schema (Pydantic) to OpenAI Structured Outputs (strict mode).

Strict mode (verified 2026-10-05, developers.openai.com/api/docs/guides/structured-outputs):
every object lists ALL its properties in ``required`` and sets ``additionalProperties: false``.
Value constraints (lengths, ranges, patterns) are not guaranteed to be accepted, so they are
moved into the field ``description`` - the model still sees them, and the project's own
Pydantic validation of the answer remains the authority.
"""

from __future__ import annotations

from typing import Any

_HINTS = {
    "minLength": "min {} characters",
    "maxLength": "max {} characters",
    "minItems": "at least {} item(s)",
    "maxItems": "at most {} item(s)",
    "minimum": "minimum {}",
    "maximum": "maximum {}",
    "exclusiveMinimum": "greater than {}",
    "exclusiveMaximum": "less than {}",
    "pattern": "must match {}",
}
_DROP = {"title", "default", "format", "examples"}


def _adapt(node: Any) -> Any:
    if isinstance(node, list):
        return [_adapt(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    hints: list[str] = []
    for key, value in node.items():
        if key in _DROP:
            continue
        if key in _HINTS:
            hints.append(_HINTS[key].format(value))
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _adapt(sub) for name, sub in value.items()}
        else:
            out[key] = _adapt(value)
    if "properties" in out:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    if hints:
        text = "; ".join(hints)
        out["description"] = f"{out['description']} ({text})" if out.get("description") else text
    return out


def strict_schema(domain_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a strict-mode schema; the input is not modified."""
    adapted = _adapt(domain_schema)
    if not isinstance(adapted, dict) or adapted.get("type") != "object":
        raise ValueError("structured output needs an object schema at the root")
    return adapted


def text_format(name: str, domain_schema: dict[str, Any]) -> dict[str, Any]:
    """The ``text`` parameter of ``responses.create`` for a strict JSON-schema answer."""
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "strict": True,
            "schema": strict_schema(domain_schema),
        }
    }
