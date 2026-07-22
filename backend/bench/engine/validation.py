"""Structured-output validation: JSON Schema + the cited-values cross-check.

The cross-check makes trust rule #1 mechanical: a value the model did not
actually retrieve via lookup_dataset cannot appear in cited_values.
"""
from __future__ import annotations

import jsonschema


def validate_payload(payload: dict, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{path}: {err.message}")
    return errors[:10]


def validate_cited_values(cited: list, retrieved: list[dict]) -> list[str]:
    """Every cited value must match a (dataset, row_ref, value) actually retrieved.

    Values must be quoted verbatim in cited_values (prose may approximate).
    """
    if not isinstance(cited, list):
        return ["cited_values must be an array"]
    retrieved_keys = {(r["dataset"], r["row_ref"], r["value"]) for r in retrieved}
    retrieved_loose = {(r["dataset"], r["value"]) for r in retrieved}
    errors = []
    for i, c in enumerate(cited):
        if not isinstance(c, dict):
            errors.append(f"cited_values[{i}]: must be an object")
            continue
        key = (str(c.get("dataset")), str(c.get("row_ref")), str(c.get("value")))
        if key in retrieved_keys:
            continue
        if (str(c.get("dataset")), str(c.get("value"))) in retrieved_loose:
            errors.append(
                f"cited_values[{i}]: value '{c.get('value')}' was retrieved but row_ref "
                f"'{c.get('row_ref')}' does not match the lookup_dataset result (_row field)"
            )
        else:
            errors.append(
                f"cited_values[{i}]: value '{c.get('value')}' from dataset '{c.get('dataset')}' "
                "was never retrieved via lookup_dataset in this run — quote values verbatim "
                "from tool results; do not compute or recall them"
            )
    return errors
