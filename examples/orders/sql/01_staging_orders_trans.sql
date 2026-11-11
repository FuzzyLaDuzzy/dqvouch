INSERT INTO staging.orders_trans
SELECT order_id, status, quantity, unit_price, order_date, customer_id, channel
FROM landing.orders_raw
WHERE order_id IS NOT NULL;
