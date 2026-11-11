INSERT INTO analytic.daily_sales
SELECT order_date, count(*) AS orders, sum(total_price) AS revenue
FROM analytic.fact_orders
GROUP BY order_date;
