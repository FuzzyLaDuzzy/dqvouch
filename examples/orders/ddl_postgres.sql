CREATE TABLE landing.orders_raw (
    order_id bigint,
    status text NOT NULL,
    quantity integer NOT NULL,
    unit_price double precision NOT NULL,
    order_date date NOT NULL,
    customer_email text NOT NULL,
    customer_id bigint NOT NULL,
    channel text NOT NULL
);
COMMENT ON TABLE landing.orders_raw IS 'Raw orders as exported by the shop, one row per order';
COMMENT ON COLUMN landing.orders_raw.order_id IS 'Order identifier from the shop system';
COMMENT ON COLUMN landing.orders_raw.status IS 'Order status: pending, shipped, delivered or cancelled';
COMMENT ON COLUMN landing.orders_raw.quantity IS 'Units ordered';
COMMENT ON COLUMN landing.orders_raw.unit_price IS 'Price of one unit, in EUR';
COMMENT ON COLUMN landing.orders_raw.order_date IS 'Day the order was placed';
COMMENT ON COLUMN landing.orders_raw.customer_email IS 'Customer contact e-mail';
COMMENT ON COLUMN landing.orders_raw.customer_id IS 'Customer who placed the order, see landing.customers_raw';
COMMENT ON COLUMN landing.orders_raw.channel IS 'Sales channel: web, store or app';

CREATE TABLE landing.customers_raw (
    customer_id bigint,
    email text NOT NULL,
    country text NOT NULL,
    segment text NOT NULL,
    signup_date date NOT NULL
);
COMMENT ON TABLE landing.customers_raw IS 'Customers as exported by the CRM, a customer can appear more than once after an update';
COMMENT ON COLUMN landing.customers_raw.customer_id IS 'Customer identifier from the CRM';
COMMENT ON COLUMN landing.customers_raw.email IS 'Customer e-mail as typed at signup';
COMMENT ON COLUMN landing.customers_raw.country IS 'ISO 3166 two-letter country code';
COMMENT ON COLUMN landing.customers_raw.segment IS 'Customer segment: retail, business or vip';
COMMENT ON COLUMN landing.customers_raw.signup_date IS 'Day the customer signed up';

CREATE TABLE landing.returns_web (
    return_id bigint NOT NULL,
    order_id bigint NOT NULL,
    return_date date NOT NULL,
    reason text NOT NULL,
    quantity integer NOT NULL
);
COMMENT ON TABLE landing.returns_web IS 'Returns registered on the web shop';
COMMENT ON COLUMN landing.returns_web.return_id IS 'Return identifier, unique across all return sources';
COMMENT ON COLUMN landing.returns_web.order_id IS 'Order the items were returned from';
COMMENT ON COLUMN landing.returns_web.return_date IS 'Day the return was registered';
COMMENT ON COLUMN landing.returns_web.reason IS 'Return reason: damaged, wrong_item, late or other';
COMMENT ON COLUMN landing.returns_web.quantity IS 'Units returned';

CREATE TABLE landing.returns_store (
    return_id bigint NOT NULL,
    order_id bigint NOT NULL,
    return_date date NOT NULL,
    reason text NOT NULL,
    quantity integer NOT NULL
);
COMMENT ON TABLE landing.returns_store IS 'Returns registered at the stores';
COMMENT ON COLUMN landing.returns_store.return_id IS 'Return identifier, unique across all return sources';
COMMENT ON COLUMN landing.returns_store.order_id IS 'Order the items were returned from';
COMMENT ON COLUMN landing.returns_store.return_date IS 'Day the return was registered';
COMMENT ON COLUMN landing.returns_store.reason IS 'Return reason: damaged, wrong_item, late or other';
COMMENT ON COLUMN landing.returns_store.quantity IS 'Units returned';

CREATE TABLE staging.orders_trans (
    order_id bigint NOT NULL,
    status text NOT NULL,
    quantity integer NOT NULL,
    unit_price double precision NOT NULL,
    order_date date NOT NULL,
    customer_id bigint NOT NULL,
    channel text NOT NULL
) PARTITION BY RANGE (order_date);
CREATE TABLE staging.orders_trans_2026_10 PARTITION OF staging.orders_trans
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE staging.orders_trans_other PARTITION OF staging.orders_trans DEFAULT;
COMMENT ON TABLE staging.orders_trans IS 'Orders with an identifier, without customer data';
COMMENT ON COLUMN staging.orders_trans.order_id IS 'Order identifier';
COMMENT ON COLUMN staging.orders_trans.status IS 'Order status as received';
COMMENT ON COLUMN staging.orders_trans.quantity IS 'Units ordered';
COMMENT ON COLUMN staging.orders_trans.unit_price IS 'Price of one unit, in EUR';
COMMENT ON COLUMN staging.orders_trans.order_date IS 'Day the order was placed';
COMMENT ON COLUMN staging.orders_trans.customer_id IS 'Customer who placed the order';
COMMENT ON COLUMN staging.orders_trans.channel IS 'Sales channel as received';

CREATE TABLE staging.customers (
    customer_id bigint NOT NULL,
    email text NOT NULL,
    country text NOT NULL,
    segment text NOT NULL,
    signup_date date NOT NULL
);
COMMENT ON TABLE staging.customers IS 'One row per customer: the most recent version of each, with a normalised e-mail';
COMMENT ON COLUMN staging.customers.customer_id IS 'Customer identifier, one row per customer';
COMMENT ON COLUMN staging.customers.email IS 'E-mail trimmed and in lower case';
COMMENT ON COLUMN staging.customers.country IS 'ISO 3166 two-letter country code';
COMMENT ON COLUMN staging.customers.segment IS 'Customer segment: retail, business or vip';
COMMENT ON COLUMN staging.customers.signup_date IS 'Day the customer signed up';

CREATE TABLE staging.returns (
    return_id bigint NOT NULL,
    order_id bigint NOT NULL,
    return_date date NOT NULL,
    reason text NOT NULL,
    quantity integer NOT NULL,
    source text NOT NULL
);
COMMENT ON TABLE staging.returns IS 'Returns from every source, one row per return';
COMMENT ON COLUMN staging.returns.return_id IS 'Return identifier';
COMMENT ON COLUMN staging.returns.order_id IS 'Order the items were returned from';
COMMENT ON COLUMN staging.returns.return_date IS 'Day the return was registered';
COMMENT ON COLUMN staging.returns.reason IS 'Return reason';
COMMENT ON COLUMN staging.returns.quantity IS 'Units returned';
COMMENT ON COLUMN staging.returns.source IS 'Where the return was registered: web or store';

CREATE TABLE analytic.fact_orders (
    order_id bigint NOT NULL,
    status text NOT NULL,
    total_price double precision NOT NULL,
    order_date date NOT NULL,
    customer_id bigint NOT NULL,
    country text NOT NULL,
    segment text NOT NULL
);
COMMENT ON TABLE analytic.fact_orders IS 'Valid orders of known customers, one row per order';
COMMENT ON COLUMN analytic.fact_orders.order_id IS 'Order identifier';
COMMENT ON COLUMN analytic.fact_orders.status IS 'Order status, or unknown when the shop sent none';
COMMENT ON COLUMN analytic.fact_orders.total_price IS 'quantity times unit_price, in EUR';
COMMENT ON COLUMN analytic.fact_orders.order_date IS 'Day the order was placed';
COMMENT ON COLUMN analytic.fact_orders.customer_id IS 'Customer who placed the order';
COMMENT ON COLUMN analytic.fact_orders.country IS 'Country of the customer';
COMMENT ON COLUMN analytic.fact_orders.segment IS 'Segment of the customer';

CREATE TABLE analytic.daily_sales (
    order_date date NOT NULL,
    orders bigint NOT NULL,
    revenue double precision NOT NULL
);
COMMENT ON TABLE analytic.daily_sales IS 'Orders and revenue per day';
COMMENT ON COLUMN analytic.daily_sales.order_date IS 'Day';
COMMENT ON COLUMN analytic.daily_sales.orders IS 'Number of orders that day';
COMMENT ON COLUMN analytic.daily_sales.revenue IS 'Sum of total_price that day, in EUR';

CREATE TABLE analytic.sales_by_country (
    country text NOT NULL,
    segment text NOT NULL,
    orders bigint NOT NULL,
    revenue double precision NOT NULL
);
COMMENT ON TABLE analytic.sales_by_country IS 'Orders and revenue per country and customer segment';
COMMENT ON COLUMN analytic.sales_by_country.country IS 'Country of the customers';
COMMENT ON COLUMN analytic.sales_by_country.segment IS 'Customer segment';
COMMENT ON COLUMN analytic.sales_by_country.orders IS 'Number of orders';
COMMENT ON COLUMN analytic.sales_by_country.revenue IS 'Sum of total_price, in EUR';
