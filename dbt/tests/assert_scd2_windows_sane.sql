-- SCD2 window invariants on the real build (the fixture rename exercises the logic;
-- this holds the result honest on whatever the data actually contains):
--   * exactly one current version per issuer;
--   * no overlapping [valid_from, valid_to) windows;
--   * no gap between consecutive versions (each valid_to equals the next valid_from).

with versions as (

    select
        hios_issuer_id,
        valid_from,
        valid_to,
        is_current,
        lead(valid_from) over (
            partition by hios_issuer_id order by valid_from
        ) as next_valid_from
    from {{ ref('dim_company') }}

),

multiple_current as (

    select hios_issuer_id, 'more or fewer than one current version' as problem
    from versions
    group by hios_issuer_id
    having count(*) filter (where is_current) <> 1

),

bad_windows as (

    select hios_issuer_id, 'window overlap or gap' as problem
    from versions
    where next_valid_from is not null
        and (valid_to is null or valid_to <> next_valid_from)

)

select * from multiple_current
union all
select * from bad_windows
