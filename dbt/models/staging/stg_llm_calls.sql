-- Source: raw.llm_calls — the Phase 2 per-call cost/token log (pipeline/extract/costlog.py).
-- Typing only; all runs pass through. The intermediate layer aggregates cost per
-- document / per target_section; prices were stamped at call time and are never
-- recomputed from a later rate card.

select
    payload ->> 'call_id' as call_id,
    payload ->> 'run_id' as run_id,
    payload ->> 'filing_id' as filing_id,
    payload ->> 'document_role' as document_role,
    payload ->> 'target_section' as target_section,

    payload ->> 'model' as model,
    payload ->> 'effort' as effort,

    -- pre-flight
    (payload ->> 'estimated_input_tokens')::int as estimated_input_tokens,

    -- actual usage
    (payload ->> 'input_tokens')::int as input_tokens,
    (payload ->> 'output_tokens')::int as output_tokens,
    (payload ->> 'cache_creation_input_tokens')::int as cache_creation_input_tokens,
    (payload ->> 'cache_read_input_tokens')::int as cache_read_input_tokens,

    payload ->> 'stop_reason' as stop_reason,
    (payload ->> 'latency_ms')::int as latency_ms,
    (payload ->> 'attempt')::int as attempt,

    -- pricing, stamped at call time (serialized as strings; numeric here)
    (payload ->> 'input_price_per_mtok')::numeric as input_price_per_mtok,
    (payload ->> 'output_price_per_mtok')::numeric as output_price_per_mtok,
    (payload ->> 'cost_usd')::numeric as cost_usd,

    payload ->> 'prompt_sha256' as prompt_sha256,
    payload ->> 'request_id' as request_id,
    payload ->> 'error' as error,

    (payload ->> 'cost_log_version')::int as cost_log_version,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'llm_calls') }}
