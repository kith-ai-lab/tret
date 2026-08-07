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
    """Every cited value must match a cell (dataset, row_ref, column, value) retrieved.

    Values must be quoted verbatim in cited_values (prose may approximate).

    The `column` is checked whenever a citation carries one. Without that check a
    real value from a real row could be attributed to the wrong field of it — a
    vintage year presented as a hazard score reads as a plausible number to any
    reader who does not go back to the dataset, and the point of the cross-check
    is that nobody should have to. Citations that omit `column` (the field is
    optional in the shipped schemas) still validate on dataset/row_ref/value
    alone, so older packs are unaffected.
    """
    if not isinstance(cited, list):
        return ["cited_values must be an array"]
    retrieved_cells = {
        (r["dataset"], r["row_ref"], str(r.get("column")), r["value"]) for r in retrieved
    }
    retrieved_keys = {(r["dataset"], r["row_ref"], r["value"]) for r in retrieved}
    retrieved_loose = {(r["dataset"], r["value"]) for r in retrieved}
    errors = []
    for i, c in enumerate(cited):
        if not isinstance(c, dict):
            errors.append(f"cited_values[{i}]: must be an object")
            continue
        dataset, row_ref = str(c.get("dataset")), str(c.get("row_ref"))
        value, column = str(c.get("value")), c.get("column")
        key = (dataset, row_ref, value)
        if key in retrieved_keys:
            if column is None or (dataset, row_ref, str(column), value) in retrieved_cells:
                continue
            held_by = sorted(
                str(r.get("column"))
                for r in retrieved
                if (r["dataset"], r["row_ref"], r["value"]) == key
            )
            errors.append(
                f"cited_values[{i}]: value '{value}' was retrieved from row '{row_ref}' but it "
                f"is the value of column {held_by}, not '{column}' — cite the column the value "
                "actually came from; attributing it to another column misstates the row"
            )
        elif (dataset, value) in retrieved_loose:
            errors.append(
                f"cited_values[{i}]: value '{value}' was retrieved but row_ref "
                f"'{row_ref}' does not match the lookup_dataset result (_row field)"
            )
        else:
            errors.append(
                f"cited_values[{i}]: value '{value}' from dataset '{dataset}' "
                "was never retrieved via lookup_dataset in this run — quote values verbatim "
                "from tool results; do not compute or recall them"
            )
    return errors
