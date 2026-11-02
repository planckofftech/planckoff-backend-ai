-- Suggestions while a model number is typed: GET /api/v1/price-books/search.
--
-- The search asks for one maker's products whose match_key starts with what was
-- typed (match_key LIKE 'BB11%'). The index from 022 is in the database's
-- collation, which a LIKE prefix cannot use; text_pattern_ops can.

create index if not exists price_rows_search_idx
    on price_rows (manufacturer, match_key text_pattern_ops)
    where kind = 'base';
