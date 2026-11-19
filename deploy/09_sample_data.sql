-- =============================================================================
-- 09  SAMPLE source data (demo only; skipped with include_sample: false)
--     Deterministic synthetic data that reproduces the "two net revenues" case:
--     Finance recognizes revenue from invoices after refunds and discounts,
--     excluding intercompany invoices and test accounts. Sales counts bookings
--     net of discounts at signing.
-- =============================================================================
USE ROLE {{ADMIN_ROLE}};
USE WAREHOUSE {{WAREHOUSE}};
CREATE SCHEMA IF NOT EXISTS {{DB}}.DEMO COMMENT = 'Demo source data';
USE SCHEMA {{DB}}.DEMO;

CREATE OR REPLACE TABLE CUSTOMERS AS
SELECT 'C' || LPAD(SEQ4() + 1, 4, '0')                                              AS CUSTOMER_ID,
       'Customer ' || (SEQ4() + 1)                                                  AS CUSTOMER_NAME,
       DECODE(MOD(SEQ4(), 4), 0, 'AMER', 1, 'EMEA', 2, 'APJ', 'LATAM')               AS REGION,
       DECODE(MOD(SEQ4(), 3), 0, 'Enterprise', 1, 'Mid-Market', 'SMB')              AS SEGMENT,
       MOD(SEQ4(), 50) = 7                                                          AS IS_TEST_ACCOUNT,
       MOD(SEQ4(), 40) = 3                                                          AS IS_INTERNAL_ENTITY
  FROM TABLE(GENERATOR(ROWCOUNT => 200));

CREATE OR REPLACE TABLE INVOICES AS
WITH g AS (SELECT SEQ4() AS N FROM TABLE(GENERATOR(ROWCOUNT => 6000)))
SELECT 'INV' || LPAD(N + 1, 6, '0')                                                   AS INVOICE_ID,
       'C' || LPAD(MOD(ABS(HASH(N, 'c')), 200) + 1, 4, '0')                            AS CUSTOMER_ID,
       DATEADD('day', MOD(ABS(HASH(N, 'd')), 670), '2025-01-01'::DATE)               AS INVOICE_DATE,
       DATEADD('day', MOD(ABS(HASH(N, 'r')), 45),
               DATEADD('day', MOD(ABS(HASH(N, 'd')), 670), '2025-01-01'::DATE))      AS RECOGNIZED_DATE,
       ROUND(500 + MOD(ABS(HASH(N, 'a')), 950000) / 100, 2)                           AS INVOICE_AMOUNT,
       IFF(MOD(ABS(HASH(N, 'f')), 20) = 0, ROUND((500 + MOD(ABS(HASH(N, 'a')), 950000) / 100) * 0.5, 2), 0) AS REFUND_AMOUNT,
       ROUND((500 + MOD(ABS(HASH(N, 'a')), 950000) / 100) * MOD(ABS(HASH(N, 'x')), 11) / 100, 2)           AS DISCOUNT_AMOUNT,
       MOD(ABS(HASH(N, 'i')), 33) = 0                                                 AS IS_INTERCOMPANY
  FROM g;

CREATE OR REPLACE TABLE BOOKINGS AS
WITH g AS (SELECT SEQ4() AS N FROM TABLE(GENERATOR(ROWCOUNT => 4500)))
SELECT 'BKG' || LPAD(N + 1, 6, '0')                                                   AS BOOKING_ID,
       'C' || LPAD(MOD(ABS(HASH(N, 'bc')), 200) + 1, 4, '0')                           AS CUSTOMER_ID,
       DATEADD('day', MOD(ABS(HASH(N, 'bd')), 670), '2025-01-01'::DATE)              AS SIGNED_DATE,
       ROUND(1000 + MOD(ABS(HASH(N, 'ba')), 1500000) / 100, 2)                        AS LIST_AMOUNT,
       ROUND((1000 + MOD(ABS(HASH(N, 'ba')), 1500000) / 100) * MOD(ABS(HASH(N, 'bx')), 21) / 100, 2)       AS DISCOUNT_AMOUNT,
       DECODE(MOD(ABS(HASH(N, 'bs')), 3), 0, 'New Business', 1, 'Expansion', 'Renewal') AS BOOKING_TYPE
  FROM g;

-- Finance reporting scope: exclusions are applied in the governed source, not left to the agent.
CREATE OR REPLACE VIEW V_REPORTABLE_INVOICES
  COMMENT = 'Invoices in Finance reporting scope: excludes intercompany invoices and test accounts'
AS
SELECT I.*
  FROM INVOICES I
  JOIN CUSTOMERS C ON C.CUSTOMER_ID = I.CUSTOMER_ID
 WHERE NOT I.IS_INTERCOMPANY
   AND NOT C.IS_TEST_ACCOUNT;
