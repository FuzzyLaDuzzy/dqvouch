INSERT INTO staging.returns
SELECT return_id, order_id, return_date, reason, quantity, 'web' AS source
FROM landing.returns_web
UNION ALL
SELECT return_id, order_id, return_date, reason, quantity, 'store' AS source
FROM landing.returns_store;
