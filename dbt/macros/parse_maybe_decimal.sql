{#-
    parse_maybe_decimal(json_col, field)

    Emits TWO select-list expressions for a MaybeDecimal field
    (pipeline/extract/schema.py: MaybeDecimal = Decimal | CellError | None):

        <field>                 numeric — the value; NULL when absent, JSON null,
                                or when the source holds a CellError
        <field>_is_cell_error   boolean — true only when the source cell holds an
                                error object ({"raw": "#VALUE!", "cell": "Wksh 2!E22"})

    This preserves ADR 0006's CellError != None distinction in SQL — #VALUE! at
    source is not "missing", and a model that coalesces them is wrong: a bare
    null reads as "the document did not say" when the truth is "the document
    said, and the value is unusable". Downstream models must consult the flag
    before treating the NULL as absence.

    Mechanics: numerics arrive as JSON strings ("0.119"), CellErrors as objects.
    jsonb_typeof(col -> field) is SQL NULL when the key is absent and 'null' on
    JSON null, so coalesce(..., false) keeps the flag false in both non-error
    cases.
-#}
{% macro parse_maybe_decimal(json_col, field) -%}
case
        when jsonb_typeof({{ json_col }} -> '{{ field }}') = 'object' then null
        else ({{ json_col }} ->> '{{ field }}')::numeric
    end as {{ field }},
    coalesce(jsonb_typeof({{ json_col }} -> '{{ field }}') = 'object', false)
        as {{ field }}_is_cell_error
{%- endmacro %}
