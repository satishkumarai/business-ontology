-- =============================================================================
-- 10  SAMPLE semantic views (governed computation; demo only)
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE WAREHOUSE {{WAREHOUSE}};
CREATE SCHEMA IF NOT EXISTS {{DB}}.SEMANTIC COMMENT = 'Demo semantic views (governed computation)';
USE SCHEMA {{DB}}.SEMANTIC;

CREATE OR REPLACE SEMANTIC VIEW FINANCE_REVENUE_SV
  TABLES (
    INVOICES AS {{DB}}.DEMO.V_REPORTABLE_INVOICES PRIMARY KEY (INVOICE_ID)
      COMMENT = 'Invoices in Finance reporting scope (intercompany and test accounts excluded)',
    CUSTOMERS AS {{DB}}.DEMO.CUSTOMERS PRIMARY KEY (CUSTOMER_ID)
      COMMENT = 'Customer master'
  )
  RELATIONSHIPS (
    INVOICE_CUSTOMER AS INVOICES (CUSTOMER_ID) REFERENCES CUSTOMERS
  )
  FACTS (
    INVOICES.INVOICE_AMOUNT_F AS INVOICE_AMOUNT COMMENT = 'Invoiced amount',
    INVOICES.REFUND_AMOUNT_F AS REFUND_AMOUNT COMMENT = 'Refunded amount',
    INVOICES.DISCOUNT_AMOUNT_F AS DISCOUNT_AMOUNT COMMENT = 'Discount amount'
  )
  DIMENSIONS (
    INVOICES.RECOGNIZED_DATE AS RECOGNIZED_DATE COMMENT = 'Date revenue is recognized; use for all Finance revenue time filters',
    INVOICES.RECOGNIZED_QUARTER AS DATE_TRUNC('QUARTER', RECOGNIZED_DATE) COMMENT = 'Fiscal quarter (calendar) of recognition',
    INVOICES.INVOICE_DATE AS INVOICE_DATE COMMENT = 'Invoice issue date',
    CUSTOMERS.REGION AS REGION COMMENT = 'Customer sales region: AMER, EMEA, APJ, LATAM',
    CUSTOMERS.SEGMENT AS SEGMENT COMMENT = 'Customer segment: Enterprise, Mid-Market, SMB'
  )
  METRICS (
    INVOICES.GROSS_REVENUE AS SUM(INVOICES.INVOICE_AMOUNT_F)
      COMMENT = 'Invoiced amount before refunds and discounts',
    INVOICES.NET_REVENUE AS SUM(INVOICES.INVOICE_AMOUNT_F) - SUM(INVOICES.REFUND_AMOUNT_F) - SUM(INVOICES.DISCOUNT_AMOUNT_F)
      WITH SYNONYMS = ('net sales', 'recognized revenue')
      COMMENT = 'Recognized revenue after refunds and discounts; excludes intercompany invoices and test accounts'
  )
  COMMENT = 'Finance: recognized revenue (owner: Finance data team)';

CREATE OR REPLACE SEMANTIC VIEW SALES_BOOKINGS_SV
  TABLES (
    BOOKINGS AS {{DB}}.DEMO.BOOKINGS PRIMARY KEY (BOOKING_ID) COMMENT = 'Signed bookings',
    CUSTOMERS AS {{DB}}.DEMO.CUSTOMERS PRIMARY KEY (CUSTOMER_ID) COMMENT = 'Customer master'
  )
  RELATIONSHIPS (
    BOOKING_CUSTOMER AS BOOKINGS (CUSTOMER_ID) REFERENCES CUSTOMERS
  )
  FACTS (
    BOOKINGS.LIST_AMOUNT_F AS LIST_AMOUNT COMMENT = 'List price at signing',
    BOOKINGS.DISCOUNT_AMOUNT_F AS DISCOUNT_AMOUNT COMMENT = 'Discount granted at signing'
  )
  DIMENSIONS (
    BOOKINGS.SIGNED_DATE AS SIGNED_DATE COMMENT = 'Contract signing date; use for all Sales bookings time filters',
    BOOKINGS.SIGNED_QUARTER AS DATE_TRUNC('QUARTER', SIGNED_DATE) COMMENT = 'Quarter of signing',
    BOOKINGS.BOOKING_TYPE AS BOOKING_TYPE COMMENT = 'New Business, Expansion or Renewal',
    CUSTOMERS.REGION AS REGION COMMENT = 'Customer sales region',
    CUSTOMERS.SEGMENT AS SEGMENT COMMENT = 'Customer segment'
  )
  METRICS (
    BOOKINGS.GROSS_BOOKINGS AS SUM(BOOKINGS.LIST_AMOUNT_F) COMMENT = 'Bookings at list price',
    BOOKINGS.NET_REVENUE AS SUM(BOOKINGS.LIST_AMOUNT_F) - SUM(BOOKINGS.DISCOUNT_AMOUNT_F)
      WITH SYNONYMS = ('net bookings')
      COMMENT = 'Bookings net of discounts at signing'
  )
  COMMENT = 'Sales: bookings (owner: Sales Ops)';

GRANT USAGE ON SCHEMA {{DB}}.SEMANTIC TO ROLE {{CONSUMER_ROLE}};
GRANT USAGE ON SCHEMA {{DB}}.DEMO TO ROLE {{CONSUMER_ROLE}};
GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA {{DB}}.SEMANTIC TO ROLE {{CONSUMER_ROLE}};
GRANT SELECT ON ALL TABLES IN SCHEMA {{DB}}.DEMO TO ROLE {{CONSUMER_ROLE}};
GRANT SELECT ON ALL VIEWS IN SCHEMA {{DB}}.DEMO TO ROLE {{CONSUMER_ROLE}};
