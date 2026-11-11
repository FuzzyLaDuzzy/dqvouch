CREATE TABLE landing.orders_raw (
    order_id Nullable(UInt64) COMMENT 'Order identifier from the shop system',
    status String COMMENT 'Order status: pending, shipped, delivered or cancelled',
    quantity Int32 COMMENT 'Units ordered',
    unit_price Float64 COMMENT 'Price of one unit, in EUR',
    order_date Date COMMENT 'Day the order was placed',
    customer_email String COMMENT 'Customer contact e-mail',
    customer_id UInt64 COMMENT 'Customer who placed the order, see landing.customers_raw',
    channel String COMMENT 'Sales channel: web, store or app'
) ENGINE = MergeTree ORDER BY tuple()
COMMENT 'Raw orders as exported by the shop, one row per order';

CREATE TABLE landing.customers_raw (
    customer_id Nullable(UInt64) COMMENT 'Customer identifier from the CRM',
    email String COMMENT 'Customer e-mail as typed at signup',
    country String COMMENT 'ISO 3166 two-letter country code',
    segment String COMMENT 'Customer segment: retail, business or vip',
    signup_date Date COMMENT 'Day the customer signed up'
) ENGINE = MergeTree ORDER BY tuple()
COMMENT 'Customers as exported by the CRM, a customer can appear more than once after an update';

CREATE TABLE landing.returns_web (
    return_id UInt64 COMMENT 'Return identifier, unique across all return sources',
    order_id UInt64 COMMENT 'Order the items were returned from',
    return_date Date COMMENT 'Day the return was registered',
    reason String COMMENT 'Return reason: damaged, wrong_item, late or other',
    quantity Int32 COMMENT 'Units returned'
) ENGINE = MergeTree ORDER BY tuple()
COMMENT 'Returns registered on the web shop';

CREATE TABLE landing.returns_store (
    return_id UInt64 COMMENT 'Return identifier, unique across all return sources',
    order_id UInt64 COMMENT 'Order the items were returned from',
    return_date Date COMMENT 'Day the return was registered',
    reason String COMMENT 'Return reason: damaged, wrong_item, late or other',
    quantity Int32 COMMENT 'Units returned'
) ENGINE = MergeTree ORDER BY tuple()
COMMENT 'Returns registered at the stores';

CREATE TABLE staging.orders_trans (
    order_id UInt64 COMMENT 'Order identifier',
    status String COMMENT 'Order status as received',
    quantity Int32 COMMENT 'Units ordered',
    unit_price Float64 COMMENT 'Price of one unit, in EUR',
    order_date Date COMMENT 'Day the order was placed',
    customer_id UInt64 COMMENT 'Customer who placed the order',
    channel String COMMENT 'Sales channel as received'
) ENGINE = MergeTree PARTITION BY toYYYYMM(order_date) ORDER BY order_id
COMMENT 'Orders with an identifier, without customer data';

CREATE TABLE staging.customers (
    customer_id UInt64 COMMENT 'Customer identifier, one row per customer',
    email String COMMENT 'E-mail trimmed and in lower case',
    country String COMMENT 'ISO 3166 two-letter country code',
    segment String COMMENT 'Customer segment: retail, business or vip',
    signup_date Date COMMENT 'Day the customer signed up'
) ENGINE = MergeTree ORDER BY customer_id
COMMENT 'One row per customer: the most recent version of each, with a normalised e-mail';

CREATE TABLE staging.returns (
    return_id UInt64 COMMENT 'Return identifier',
    order_id UInt64 COMMENT 'Order the items were returned from',
    return_date Date COMMENT 'Day the return was registered',
    reason String COMMENT 'Return reason',
    quantity Int32 COMMENT 'Units returned',
    source String COMMENT 'Where the return was registered: web or store'
) ENGINE = MergeTree ORDER BY return_id
COMMENT 'Returns from every source, one row per return';

CREATE TABLE analytic.fact_orders (
    order_id UInt64 COMMENT 'Order identifier',
    status String COMMENT 'Order status, or unknown when the shop sent none',
    total_price Float64 COMMENT 'quantity times unit_price, in EUR',
    order_date Date COMMENT 'Day the order was placed',
    customer_id UInt64 COMMENT 'Customer who placed the order',
    country String COMMENT 'Country of the customer',
    segment String COMMENT 'Segment of the customer'
) ENGINE = MergeTree PARTITION BY toYYYYMM(order_date) ORDER BY order_id
COMMENT 'Valid orders of known customers, one row per order';

CREATE TABLE analytic.daily_sales (
    order_date Date COMMENT 'Day',
    orders UInt64 COMMENT 'Number of orders that day',
    revenue Float64 COMMENT 'Sum of total_price that day, in EUR'
) ENGINE = MergeTree ORDER BY order_date
COMMENT 'Orders and revenue per day';

CREATE TABLE analytic.sales_by_country (
    country String COMMENT 'Country of the customers',
    segment String COMMENT 'Customer segment',
    orders UInt64 COMMENT 'Number of orders',
    revenue Float64 COMMENT 'Sum of total_price, in EUR'
) ENGINE = MergeTree ORDER BY (country, segment)
COMMENT 'Orders and revenue per country and customer segment';
