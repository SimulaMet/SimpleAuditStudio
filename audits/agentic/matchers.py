"""Robust argument matchers for agentic checks."""
import re
from typing import Any, Callable, Dict
from urllib.parse import urlparse


# Custom predicates registered by identifier (no eval of JSON)
_custom_predicates: Dict[str, Callable[[Any, Any], bool]] = {}


def register_predicate(identifier: str, predicate: Callable[[Any, Any], bool]) -> None:
    """Register a server-side custom predicate by identifier."""
    _custom_predicates[identifier] = predicate


def get_predicate(identifier: str) -> Callable[[Any, Any], bool] | None:
    """Retrieve a registered predicate."""
    return _custom_predicates.get(identifier)


def match_exact(observed: Any, expected: Any) -> bool:
    """Exact equality match."""
    return observed == expected


def match_subset(observed: Any, expected: Any) -> bool:
    """Recursive subset match (observed must contain all expected keys/values)."""
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and match_subset(observed[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) >= len(expected) and all(
            match_subset(actual, value) for actual, value in zip(observed, expected)
        )
    return observed == expected


def match_json_schema(observed: Any, expected: Dict[str, Any]) -> bool:
    """Validate against JSON schema (simplified check)."""
    try:
        import jsonschema
        jsonschema.validate(observed, expected)
        return True
    except ImportError:
        # Fallback: basic schema validation without jsonschema library
        return _basic_schema_check(observed, expected)
    except Exception:
        return False


def _basic_schema_check(observed: Any, schema: Dict[str, Any]) -> bool:
    """Basic JSON schema validation without jsonschema library."""
    schema_type = schema.get("type")
    if schema_type and not isinstance(observed, _type_map.get(schema_type)):
        return False
    if "enum" in schema and observed not in schema["enum"]:
        return False
    return True


_type_map = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def match_regex(observed: Any, pattern: str) -> bool:
    """Regex match against string representation."""
    try:
        text = str(observed)
        return bool(re.search(pattern, text))
    except (TypeError, re.error):
        return False


def match_one_of(observed: Any, values: list[Any]) -> bool:
    """Match if observed is in the list of allowed values."""
    return observed in values


def match_numeric_range(observed: Any, range_spec: Dict[str, Any]) -> bool:
    """Match if observed is within numeric range."""
    try:
        num = float(observed)
        min_val = range_spec.get("min")
        max_val = range_spec.get("max")
        inclusive_min = range_spec.get("min_inclusive", True)
        inclusive_max = range_spec.get("max_inclusive", True)

        if min_val is not None:
            if inclusive_min and num < min_val:
                return False
            elif not inclusive_min and num <= min_val:
                return False

        if max_val is not None:
            if inclusive_max and num > max_val:
                return False
            elif not inclusive_max and num >= max_val:
                return False

        return True
    except (TypeError, ValueError):
        return False


def match_case_insensitive(observed: Any, expected: str) -> bool:
    """Case-insensitive string match."""
    try:
        return str(observed).lower() == expected.lower()
    except (TypeError, AttributeError):
        return False


def match_normalized_uri(observed: str, expected: str) -> bool:
    """Normalize URIs and compare (scheme + netloc + path)."""
    try:
        obs_parsed = urlparse(observed)
        exp_parsed = urlparse(expected)

        obs_normalized = (obs_parsed.scheme, obs_parsed.netloc, obs_parsed.path)
        exp_normalized = (exp_parsed.scheme, exp_parsed.netloc, exp_parsed.path)

        return obs_normalized == exp_normalized
    except Exception:
        return False


def match_entity_equality(observed: Any, expected: Any) -> bool:
    """Compare objects by identity (id field or __eq__)."""
    if isinstance(observed, dict) and isinstance(expected, dict):
        obs_id = observed.get("id") or observed.get("_id")
        exp_id = expected.get("id") or expected.get("_id")
        if obs_id and exp_id:
            return obs_id == exp_id
    return observed == expected


def match_custom(observed: Any, predicate_id: str, value: Any = None) -> bool:
    """Match using a registered custom predicate."""
    predicate = get_predicate(predicate_id)
    if predicate is None:
        return False
    try:
        return predicate(observed, value)
    except Exception:
        return False


def match_arguments(observed: Any, expected: Dict[str, Any]) -> bool:
    """Match arguments using the specified match type."""
    match_type = expected.get("match", "subset")
    value = expected.get("value")

    if match_type == "exact":
        return match_exact(observed, value)
    elif match_type == "subset":
        return match_subset(observed, value)
    elif match_type == "json_schema":
        return match_json_schema(observed, value)
    elif match_type == "regex":
        return match_regex(observed, value)
    elif match_type == "one_of":
        return match_one_of(observed, value)
    elif match_type == "numeric_range":
        return match_numeric_range(observed, value)
    elif match_type == "case_insensitive":
        return match_case_insensitive(observed, value)
    elif match_type == "normalized_uri":
        return match_normalized_uri(observed, value)
    elif match_type == "entity_equality":
        return match_entity_equality(observed, value)
    elif match_type == "custom":
        predicate_id = expected.get("predicate_id")
        return match_custom(observed, predicate_id, value)
    else:
        return False
