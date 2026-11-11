INSERT INTO staging.customers
SELECT customer_id, lower(trim(email)) AS email, country, segment, signup_date
FROM (
    SELECT customer_id, email, country, segment, signup_date,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY signup_date DESC) AS rn
    FROM landing.customers_raw
    WHERE customer_id IS NOT NULL
) AS ranked
WHERE rn = 1;
