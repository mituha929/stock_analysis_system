-- Additive P2 migration. Apply to the reviewed, explicitly selected schema.
CREATE TABLE IF NOT EXISTS market_data_sources (
    source_id VARCHAR(96) PRIMARY KEY,
    origin VARCHAR(255) NOT NULL,
    license_status VARCHAR(32) NOT NULL DEFAULT 'pending'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_series (
    report_key VARCHAR(96) PRIMARY KEY,
    source_id VARCHAR(96) NOT NULL,
    dataset VARCHAR(32) NOT NULL,
    market VARCHAR(8) NOT NULL,
    period VARCHAR(10) NOT NULL,
    segment VARCHAR(24) NOT NULL,
    current_revision BIGINT,
    UNIQUE (dataset, market, period, segment),
    FOREIGN KEY (source_id) REFERENCES market_data_sources (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_snapshots (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    raw_path TEXT NOT NULL,
    source_metadata JSON NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    imported_at DATETIME(6) NOT NULL,
    published_at DATETIME(6),
    available_at DATETIME(6),
    row_count INT NOT NULL,
    PRIMARY KEY (report_key, revision),
    UNIQUE (report_key, raw_sha256),
    FOREIGN KEY (report_key) REFERENCES market_data_series (report_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_institutional_stock_daily (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    stock_name VARCHAR(128) NOT NULL,
    foreign_net_shares_excluding_foreign_dealers DECIMAL(38,12),
    investment_trust_net_shares DECIMAL(38,12),
    dealer_net_shares DECIMAL(38,12),
    PRIMARY KEY (report_key, revision, stock_code),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_institutional_market_daily (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    institution_type VARCHAR(128) NOT NULL,
    buy_amount DECIMAL(38,12) NOT NULL,
    sell_amount DECIMAL(38,12) NOT NULL,
    net_amount DECIMAL(38,12) NOT NULL,
    net_difference_flag TINYINT NOT NULL CHECK (net_difference_flag IN (0, 1)),
    net_difference_twd DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (report_key, revision, institution_type),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_margin_stock_daily (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    stock_name VARCHAR(128) NOT NULL,
    financing_previous_balance DECIMAL(38,12),
    financing_balance DECIMAL(38,12),
    short_previous_balance DECIMAL(38,12),
    short_balance DECIMAL(38,12),
    PRIMARY KEY (report_key, revision, stock_code),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_margin_market_daily (
    report_key VARCHAR(96) NOT NULL,
    revision INTEGER NOT NULL,
    measure VARCHAR(32) NOT NULL,
    balance_unit VARCHAR(20) NOT NULL,
    previous_balance DECIMAL(38,12) NULL,
    balance DECIMAL(38,12) NOT NULL,
    PRIMARY KEY (report_key, revision, measure),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_revenue_monthly (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    stock_name VARCHAR(128) NOT NULL,
    consolidation_scope VARCHAR(32),
    revenue DECIMAL(38,12),
    previous_month_revenue DECIMAL(38,12),
    previous_year_same_month_revenue DECIMAL(38,12),
    mom_percent DECIMAL(38,12),
    yoy_percent DECIMAL(38,12),
    year_to_date_revenue DECIMAL(38,12),
    previous_year_to_date_revenue DECIMAL(38,12),
    year_to_date_yoy_percent DECIMAL(38,12),
    source_note TEXT,
    PRIMARY KEY (report_key, revision, stock_code),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_financial_statements (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    stock_name VARCHAR(128) NOT NULL,
    fiscal_year INT NOT NULL,
    fiscal_quarter INT NOT NULL,
    statement_type VARCHAR(24) NOT NULL,
    industry_schema VARCHAR(24) NOT NULL,
    consolidation_scope VARCHAR(32),
    period_type VARCHAR(24) NOT NULL,
    PRIMARY KEY (report_key, revision, stock_code),
    FOREIGN KEY (report_key, revision) REFERENCES market_data_snapshots (report_key, revision)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_financial_facts (
    report_key VARCHAR(96) NOT NULL,
    revision BIGINT NOT NULL,
    stock_code VARCHAR(16) NOT NULL,
    source_label VARCHAR(191) NOT NULL,
    standard_code VARCHAR(64),
    amount DECIMAL(38,12),
    unit VARCHAR(24) NOT NULL,
    PRIMARY KEY (report_key, revision, stock_code, source_label),
    FOREIGN KEY (report_key, revision, stock_code)
        REFERENCES market_data_financial_statements (report_key, revision, stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS market_data_ingestion_runs (
    run_id CHAR(36) NOT NULL,
    report_key VARCHAR(96) NOT NULL,
    raw_sha256 CHAR(64),
    finished_at DATETIME(6) NOT NULL,
    status VARCHAR(24) NOT NULL,
    row_count INT NOT NULL,
    flagged_rows INT NOT NULL,
    error_code VARCHAR(255),
    PRIMARY KEY (run_id, report_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
