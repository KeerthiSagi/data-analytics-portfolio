"""Convert data type: explicit, previewed conversion of selected columns.

Settings (all frozen in the recipe):

    columns     the columns to convert (all of the same kind)
    target      string | integer | decimal | boolean | date | datetime
    on_error    strict  (default) refuse the step if any value fails
                coerce  failed values become missing, counted and warned
                preserve is refused with an explanation: a typed column
                         cannot hold the original text of failed values
    numeric     thousands_separator, decimal_separator, currency,
                parentheses_negative, percent, fraction (integer only)
    dates       date_formats, custom_format, time_of_day (date only),
                timezone (datetime only)
    boolean     true_values, false_values, case_sensitive

Text columns are parsed with these rules (refine/conversion.py). Typed
columns can be converted to text, between whole and decimal numbers, and
between dates and date & time; their values are read from REFINE's
canonical text, so the parsing options do not apply to them.

resolve() parses every distinct value once and freezes the expected
counts. apply() parses again with the frozen settings and refuses data
that does not give the same counts, so replay never converts differently.
Row count, row order, row identities, column order and other columns are
unchanged.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

import numpy as np
import pandas as pd

from refine.conversion import (
    BOOLEAN_PRESETS,
    DATE_FORMATS,
    DECIMALS,
    FRACTION,
    KIND_LABELS,
    NUMERIC_DEFAULTS,
    PERCENT,
    TARGET_KIND,
    TARGET_LABELS,
    TARGETS,
    THOUSANDS,
    TIME_OF_DAY,
    TIMEZONE,
    canonical_text,
    check_custom_format,
    column_kind,
    convert_column,
    dtype_name,
    is_utc,
)
from refine.errors import CleaningError
from refine.source import TEXT_DTYPE
from refine.notes import plural, quote, verb
from refine.operations.base import Operation, OperationResult
from refine.operations.missing_ops import _ids, no_change
from refine.operations.params import check_keys, choice, column_list, expect, flag, invalid, unique_label
from refine.operations.registry import register

ON_ERROR = ("strict", "coerce", "preserve")
NUMERIC_KEYS = {"thousands_separator", "decimal_separator", "currency", "parentheses_negative", "percent"}
DATE_KEYS = {"date_formats", "custom_format"}
BOOLEAN_KEYS = {"true_values", "false_values", "case_sensitive"}
TARGET_KEYS = {
    "string": set(),
    "integer": NUMERIC_KEYS | {"fraction"},
    "decimal": set(NUMERIC_KEYS),
    "boolean": set(BOOLEAN_KEYS),
    "date": DATE_KEYS | {"time_of_day"},
    "datetime": DATE_KEYS | {"timezone"},
}
PARSING_KEYS = NUMERIC_KEYS | DATE_KEYS | BOOLEAN_KEYS
TYPED_PAIRS = {("integer", "decimal"), ("decimal", "integer"), ("datetime", "date"), ("date", "datetime")}
MAX_BOOLEAN_TOKENS = 50
PRESERVE_EXPLANATION = (
    "Keeping the original text of values that cannot be converted is not possible: a {target} column can only "
    "hold {what}, so it cannot also hold text such as 'unknown'. Choose 'Refuse the step' to fix those values "
    "first, or 'Convert invalid values to missing' to replace them with no value. To keep the text, leave the "
    "column as text."
)
WHAT = {"integer": "whole numbers", "decimal": "numbers", "boolean": "true and false", "date": "dates",
        "datetime": "dates and times", "string": "text"}
FRACTION_LABELS = {"round_half_even": "half to even (2.5 → 2, 3.5 → 4)",
                   "round_half_up": "half away from zero (2.5 → 3, -2.5 → -3)"}


def _tokens(value: Any, key: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise invalid(f"Give '{key}' as a list of text values.")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str):
            raise invalid(f"Every value in '{key}' must be text.")
        v = v.strip()
        if not v:
            raise invalid(f"'{key}' cannot contain blank values.")
        if len(v) > 100:
            raise invalid(f"Values in '{key}' can be at most 100 characters.")
        if v not in out:
            out.append(v)
    if len(out) > MAX_BOOLEAN_TOKENS:
        raise invalid(f"'{key}' can list at most {MAX_BOOLEAN_TOKENS} values.")
    return out


class ConvertType(Operation):
    op_id = "convert_type"
    label = "Convert data type"
    description = ("Convert the columns you choose to text, whole numbers, decimal numbers, true/false, dates or "
                   "dates with times, using parsing rules you choose. Values that cannot be converted are "
                   "shown and never hidden.")

    # ---- validate ------------------------------------------------------

    def validate(self, params, data):
        check_keys(params, {"columns", "target", "on_error"} | PARSING_KEYS | {"fraction", "time_of_day", "timezone"},
                   self.label)
        columns = column_list(params.get("columns"), data, what="columns to convert")
        target = choice(params, "target", TARGETS)
        on_error = choice(params, "on_error", ON_ERROR, "strict")
        kinds = {c: column_kind(data[c].dtype) for c in columns}
        other = [c for c, k in kinds.items() if k == "other"]
        if other:
            raise invalid(f"{quote(other[0])} is stored as {data[other[0]].dtype}, which REFINE cannot convert.")
        if len(set(kinds.values())) > 1:
            shown = ", ".join(f"{quote(c)} ({KIND_LABELS[k].lower()})" for c, k in kinds.items())
            raise invalid(f"Convert columns of one kind at a time; the selection mixes {shown}.")
        source = next(iter(kinds.values()))
        target_kind = TARGET_KIND[target]
        if source == target_kind:
            raise no_change(f"{verb(len(columns), 'The column is', 'These columns are')} already stored as "
                            f"{KIND_LABELS[source].lower()}, so nothing would change.")
        if source != "text" and target != "string" and (source, target) not in TYPED_PAIRS:
            raise invalid(
                f"{quote(columns[0])} is stored as {KIND_LABELS[source].lower()} ({data[columns[0]].dtype}). "
                f"REFINE converts typed columns to text, between whole and decimal numbers, and between dates "
                f"and dates with times. Convert it to text first, then from text to {TARGET_LABELS[target]}, "
                "so every parsing choice is explicit."
            )
        if source == "datetime" and target == "date" and any(is_utc(data[c].dtype) for c in columns):
            raise invalid("Columns in UTC cannot be converted straight to dates: the date depends on the time "
                          "zone you want. Convert to text first and choose how to read it.")
        if on_error == "preserve":
            raise invalid(PRESERVE_EXPLANATION.format(target=TARGET_LABELS[target], what=WHAT[target]))

        allowed = TARGET_KEYS[target]
        given = {k for k in params if k not in ("columns", "target", "on_error") and params[k] is not None}
        unused = sorted(given - allowed)
        if unused:
            raise invalid(f"The setting '{unused[0]}' is not used when converting to {TARGET_LABELS[target]}.")
        out: dict[str, Any] = {"columns": columns, "target": target, "on_error": on_error}
        typed = source != "text"
        if typed:
            parsing = sorted(k for k in given & PARSING_KEYS
                             if not (k in NUMERIC_DEFAULTS and params[k] == NUMERIC_DEFAULTS[k])
                             and not (k == "date_formats" and params[k] == ["ymd"]))
            if parsing:
                raise invalid(f"Parsing options such as '{parsing[0]}' apply to text columns only. "
                              f"{quote(columns[0])} is already stored as {KIND_LABELS[source].lower()}.")
        if target in ("integer", "decimal"):
            if not typed:
                out.update(self._numeric(params))
            if target == "integer":
                out["fraction"] = choice(params, "fraction", FRACTION, "reject")
        elif target == "boolean":
            out.update(self._boolean(params))
        elif target in ("date", "datetime"):
            if not typed:
                out.update(self._dates(params))
            if target == "date":
                out["time_of_day"] = choice(params, "time_of_day", TIME_OF_DAY, "reject")
            else:
                out["timezone"] = choice(params, "timezone", TIMEZONE, "reject")
                if typed and out["timezone"] == "to_utc":
                    raise invalid("Dates without a time zone cannot be converted to UTC: the offset would be a "
                                  "guess. Keep the time zone setting at 'Refuse values with a time zone'.")
        return out

    def _numeric(self, params) -> dict[str, Any]:
        thousands = params.get("thousands_separator", None)
        if thousands not in THOUSANDS:
            raise invalid("The thousands separator must be none, a comma, a period, a space or an apostrophe.")
        decimal = params.get("decimal_separator", ".")
        if decimal not in DECIMALS:
            raise invalid("The decimal separator must be a period or a comma.")
        if thousands == decimal:
            raise invalid("The thousands separator and the decimal separator must be different.")
        return {"thousands_separator": thousands, "decimal_separator": decimal,
                "currency": flag(params, "currency", False),
                "parentheses_negative": flag(params, "parentheses_negative", False),
                "percent": choice(params, "percent", PERCENT, "reject")}

    def _dates(self, params) -> dict[str, Any]:
        formats = params.get("date_formats")
        if isinstance(formats, str) or not isinstance(formats, (list, tuple)) or not formats:
            raise invalid("Choose at least one date format. REFINE never guesses the order of day and month.")
        if any(f not in DATE_FORMATS for f in formats):
            raise invalid(f"Date formats must be from: {', '.join(DATE_FORMATS)}.")
        if len(set(formats)) != len(formats):
            raise invalid("Each date format can be chosen once.")
        if "mdy" in formats and "dmy" in formats:
            raise invalid("Choose month-first or day-first, not both: a value such as 05/06/2026 would then "
                          "have two meanings.")
        custom = params.get("custom_format")
        if "custom" in formats:
            if len(formats) > 1:
                raise invalid("A custom format is used on its own; remove the other formats.")
            if not isinstance(custom, str):
                raise invalid("Enter the custom format, for example %d.%m.%Y %H:%M.")
            problem = check_custom_format(custom)
            if problem:
                raise invalid(problem)
        elif custom is not None:
            raise invalid("A custom format is only used with the 'custom' date format.")
        ordered = [f for f in DATE_FORMATS if f in formats]
        return {"date_formats": ordered, "custom_format": custom if "custom" in formats else None}

    def _boolean(self, params) -> dict[str, Any]:
        true_values = _tokens(params.get("true_values", []), "true_values")
        false_values = _tokens(params.get("false_values", []), "false_values")
        if not true_values or not false_values:
            raise invalid("List at least one value that means true and one that means false. REFINE does not "
                          "assume which text means true.")
        case_sensitive = flag(params, "case_sensitive", False)
        norm = (lambda v: v) if case_sensitive else str.casefold
        both = sorted({norm(v) for v in true_values} & {norm(v) for v in false_values})
        if both:
            case = "" if case_sensitive else " (ignoring case)"
            raise invalid(f"{', '.join(map(quote, both))} {verb(len(both), 'is', 'are')} listed as both true "
                          f"and false{case}. Each value can mean only one of them.")
        return {"true_values": true_values, "false_values": false_values, "case_sensitive": case_sensitive}

    # ---- resolve / apply ---------------------------------------------

    _memo = threading.local()

    def _converted(self, data, column, resolved):
        """convert_column, remembered for the last table seen on this thread.

        A preview plans, applies and samples the same immutable state, and a
        confirmed apply uses that state again; parsing every distinct value
        once instead of four times keeps large conversions responsive. The
        memo holds a weak reference to the table, so it never keeps a table
        alive and never answers for a different one (or a changed column).
        """
        from refine.fingerprint import frame_fingerprint
        from refine.operations.base import canonical_json
        options = canonical_json({k: v for k, v in resolved.items()
                                  if k not in ("stats", "failure_reasons", "failures", "source_dtypes")})
        # The column's content hash too, so a table edited in place is never answered from the memo.
        options += frame_fingerprint(data[[column]])
        memo = getattr(self._memo, "entry", None)
        if memo is not None and memo[0]() is data:
            hit = memo[1].get((column, options))
            if hit is not None:
                return hit
        else:
            memo = (weakref.ref(data), {})
            self._memo.entry = memo
        result = convert_column(data[column], resolved["target"], resolved)
        memo[1][(column, options)] = result
        return result

    def _convert(self, data, resolved):
        for c in resolved["columns"]:
            if c not in data.columns:
                raise CleaningError("replay_mismatch", f"{self.label}: the table has no column {quote(c)}.")
            expected = resolved.get("source_dtypes", {}).get(c)
            if expected is not None and dtype_name(data[c].dtype) != expected:
                raise CleaningError("replay_mismatch",
                                    f"{self.label}: {quote(c)} is stored as {dtype_name(data[c].dtype)}, but the "
                                    f"recorded step expects {expected}.")
            new, stats, failed = self._converted(data, c, resolved)
            yield c, new, stats, failed

    def resolve(self, params, data):
        target = params["target"]
        warnings: list[str] = []
        stats_out: dict[str, dict[str, int]] = {}
        reasons_out: dict[str, dict[str, int]] = {}
        failures_out: dict[str, list[list]] = {}
        refused: list[str] = []
        for c, _new, st, _failed in self._convert(data, params):
            stats_out[c] = st.counts()
            reasons_out[c] = st.failure_reasons
            failures_out[c] = [[f.before, f.count, f.reason] for f in st.failures]
            if st.failed:
                shown = "; ".join(f"{quote(f.before)} × {f.count:,}: {f.reason}" for f in st.failures[:3])
                if params["on_error"] == "strict":
                    refused.append(f"{quote(c)}: {plural(st.failed, 'value')} of {st.present:,} cannot be "
                                   f"converted ({shown}).")
                    continue
                if not st.converted:
                    raise invalid(f"No value in {quote(c)} converts to {TARGET_LABELS[target]} with these "
                                  f"settings, so every value would become missing ({shown}). Check the options.")
                warnings.append(f"{quote(c)}: {plural(st.failed, 'value')} of {st.present:,} ({st.failed_pct:.1f}%) "
                                f"cannot be converted and would become missing ({shown}). The original text is "
                                "removed from the working table; undo or reset brings it back.")
            if st.precision_loss:
                warnings.append(f"{quote(c)}: {plural(st.precision_loss, 'value')} cannot be stored exactly as a "
                                "decimal number (Float64 holds about 15-17 significant digits) and would be "
                                "rounded slightly.")
            if st.rounded:
                warnings.append(f"{quote(c)}: {plural(st.rounded, 'value')} with a fractional part would be "
                                f"rounded to whole numbers, {FRACTION_LABELS[params['fraction']]}.")
            if st.time_dropped:
                warnings.append(f"{quote(c)}: {plural(st.time_dropped, 'value')} would lose {verb(st.time_dropped, 'its', 'their')} "
                                "time of day; only the date is kept.")
            if st.shifted:
                warnings.append(f"{quote(c)}: {plural(st.shifted, 'value')} with a time zone offset other than UTC "
                                "would be converted to UTC. The instant is kept; the original offset is not.")
            if st.ambiguous:
                if target in ("date", "datetime"):
                    order = "month-first" if "mdy" in params["date_formats"] else "day-first"
                    other = "day-first" if order == "month-first" else "month-first"
                    warnings.append(f"{quote(c)}: {plural(st.ambiguous, 'date')} such as 05/06/2026 "
                                    f"{verb(st.ambiguous, 'is', 'are')} read {order}, as you chose. Read {other} "
                                    f"{verb(st.ambiguous, 'it', 'they')} would be different dates.")
                else:
                    warnings.append(f"{quote(c)}: {plural(st.ambiguous, 'value')} such as '1,250' or '1.250' "
                                    f"{verb(st.ambiguous, 'depends', 'depend')} on the separator convention; "
                                    f"{verb(st.ambiguous, 'it is', 'they are')} read with the separators you chose.")
        if refused:
            raise invalid("Nothing was converted, because 'Refuse the step' is chosen and some values cannot be "
                          "converted. " + " ".join(refused) + " Fix or standardize those values first, or choose "
                          "'Convert invalid values to missing'.")
        if target == "string":
            warnings.append("Values become text: they keep their meaning when read, but no longer sort, compare "
                            "or calculate as numbers, dates or true/false.")
        resolved = {
            **params,
            "source_dtypes": {c: dtype_name(data[c].dtype) for c in params["columns"]},
            "stats": stats_out, "failure_reasons": reasons_out, "failures": failures_out,
        }
        return resolved, tuple(params["columns"]), tuple(warnings)

    def apply(self, data, row_ids, resolved):
        result = data.copy(deep=True)
        converted = failed = 0
        for c, new, st, failed_mask in self._convert(data, resolved):
            recorded = resolved["stats"][c]
            expect(st.converted, recorded["converted"], f"converted values in {quote(c)}", self.label)
            expect(st.failed, recorded["failed"], f"values that cannot be converted in {quote(c)}", self.label)
            if st.failed and resolved["on_error"] != "coerce":
                raise CleaningError("replay_mismatch", f"{self.label}: {quote(c)} has values that cannot be "
                                                       "converted.")
            result[c] = new.array
            converted += st.converted
            failed += st.failed
        return OperationResult(result, _ids(row_ids), {
            "columns_converted": len(resolved["columns"]), "cells_converted": converted,
            "cells_failed": failed, "new_missing": failed if resolved["on_error"] == "coerce" else 0,
        })

    def sample(self, data, resolved, limit):
        """Rows that fail first (they need attention), then rows whose value converts, before and after."""
        parts = list(self._convert(data, resolved))
        failed = np.logical_or.reduce([f for *_, f in parts])
        present = np.logical_or.reduce([~data[c].isna().to_numpy(dtype=bool) for c, *_ in parts])
        first = np.flatnonzero(failed)[: max(1, limit // 2)]
        rest = np.flatnonzero(present & ~failed)[: limit - len(first)]
        positions = np.sort(np.concatenate([first, rest]))[:limit].astype(np.int64)
        kind = TARGET_KIND[resolved["target"]]
        taken = set(data.columns)
        out = {}
        for c, new, *_ in parts:
            out[c] = data[c].iloc[positions].to_numpy(dtype=object, na_value=None)
            label = unique_label(f"{c} (after)", taken)
            taken.add(label)
            after = new.iloc[positions].to_numpy(dtype=object, na_value=None)
            out[label] = pd.array([None if v is None or v is pd.NaT else
                                   (v if kind == "text" else canonical_text(v, kind)) for v in after],
                                  dtype=TEXT_DTYPE)
        return pd.DataFrame(out, index=pd.Index(positions, dtype=np.int64))

    def summarize(self, resolved):
        cols = resolved["columns"]
        where = quote(cols[0]) if len(cols) == 1 else f"{plural(len(cols), 'column')}"
        converted = sum(s["converted"] for s in resolved["stats"].values())
        failed = sum(s["failed"] for s in resolved["stats"].values())
        extra = f"; {plural(failed, 'invalid value')} {verb(failed, 'becomes', 'become')} missing" if failed else ""
        return f"Convert {where} to {TARGET_LABELS[resolved['target']]}: {plural(converted, 'value')} converted{extra}."


CONVERT_TYPE = register(ConvertType())

__all__ = ["BOOLEAN_PRESETS", "CONVERT_TYPE", "ON_ERROR", "PRESERVE_EXPLANATION", "ConvertType"]
