INSERT INTO analytic.fact_orders
SELECT o.order_id,
       CASE WHEN o.status = '' THEN 'unknown' ELSE o.status END AS status,
       o.quantity * o.unit_price AS total_price,
       o.order_date,
       o.customer_id,
       c.country,
       c.segment
FROM staging.orders_trans AS o
INNER JOIN staging.customers AS c ON c.customer_id = o.customer_id
WHERE o.status != 'test';
