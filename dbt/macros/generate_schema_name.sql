{#-
    Standard override: a custom schema name is used AS-IS (raw / staging /
    intermediate / marts / seeds) rather than dbt's default of prefixing it with
    the target schema ("analytics_staging"). The layer names are the contract
    between the loader (which writes schema `raw`) and every downstream reader,
    so they must not vary with the profile's target schema. Nodes with no custom
    schema still land in the target schema.
-#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
