INSERT INTO analytic.sales_by_country
SELECT country, segment, count(*) AS orders, sum(total_price) AS revenue
FROM analytic.fact_orders
GROUP BY country, segment;
