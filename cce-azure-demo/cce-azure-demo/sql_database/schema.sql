-- schema.sql
-- Mirrors the customers/accounts/transactions schema used on the AWS RDS
-- side of this POC, for narrative consistency across both clouds' demos.
-- (If you'd rather branch the schema out to more banking domains for this
-- demo - e.g. splitting off a separate payments or cards table - this is
-- the file to extend; populate_sql_database.py's generator functions are
-- written per-table so adding one is additive, not a rewrite.)

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'customers')
BEGIN
    CREATE TABLE customers (
        customer_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
        title             NVARCHAR(10)   NOT NULL,
        first_name        NVARCHAR(50)   NOT NULL,
        last_name         NVARCHAR(50)   NOT NULL,
        date_of_birth     DATE           NOT NULL,
        segment           NVARCHAR(20)   NOT NULL,   -- RETAIL / PREMIER / BUSINESS / CORPORATE / PRIVATE_BANKING
        home_branch       NVARCHAR(60)   NOT NULL,
        home_sort_code    CHAR(8)        NOT NULL,
        email             NVARCHAR(100)  NOT NULL,
        phone             NVARCHAR(20)   NOT NULL,
        address_line1     NVARCHAR(100)  NOT NULL,
        city               NVARCHAR(50)  NOT NULL,
        postcode          NVARCHAR(10)   NOT NULL,
        onboarded_at      DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
END;

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'accounts')
BEGIN
    CREATE TABLE accounts (
        account_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        customer_id       BIGINT         NOT NULL REFERENCES customers(customer_id),
        account_type      NVARCHAR(20)   NOT NULL,   -- CURRENT / SAVINGS / BUSINESS / ISA / MORTGAGE_OFFSET
        sort_code         CHAR(8)        NOT NULL,
        account_number    CHAR(8)        NOT NULL,
        iban              CHAR(22)       NOT NULL,
        currency          CHAR(3)        NOT NULL DEFAULT 'GBP',
        balance           DECIMAL(15,2)  NOT NULL DEFAULT 0,
        opened_at         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        status            NVARCHAR(20)   NOT NULL DEFAULT 'ACTIVE'
    );
    CREATE INDEX idx_accounts_customer_id ON accounts(customer_id);
END;

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'transactions')
BEGIN
    CREATE TABLE transactions (
        transaction_id         BIGINT IDENTITY(1,1) PRIMARY KEY,
        account_id             BIGINT         NOT NULL REFERENCES accounts(account_id),
        transaction_type       NVARCHAR(10)   NOT NULL,   -- FPS / BACS / CHAPS / DD / SO / CARD / ATM
        amount                 DECIMAL(15,2)  NOT NULL,
        currency               CHAR(3)        NOT NULL DEFAULT 'GBP',
        counterparty_sort_code CHAR(8),
        counterparty_account   CHAR(8),
        counterparty_iban      CHAR(22),
        reference              NVARCHAR(40),
        description            NVARCHAR(100),
        branch                 NVARCHAR(60),
        status                 NVARCHAR(20)   NOT NULL DEFAULT 'SETTLED',
        metadata_json          NVARCHAR(MAX),   -- optional audit/fraud-scoring blob; see --transaction-metadata-bytes
        transacted_at           DATETIME2      NOT NULL
    );
    CREATE INDEX idx_transactions_account_id ON transactions(account_id);
    CREATE INDEX idx_transactions_transacted_at ON transactions(transacted_at);
END;
