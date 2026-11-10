-- Additive workflow checkpoints. Apply after market_data_p2_mysql.sql.
CREATE TABLE IF NOT EXISTS market_data_checkpoints (
    report_key VARCHAR(96) PRIMARY KEY,
    dataset VARCHAR(32) NOT NULL,
    market VARCHAR(8) NOT NULL,
    period VARCHAR(10) NOT NULL,
    segment VARCHAR(24) NOT NULL,
    last_status VARCHAR(32) NOT NULL,
    last_attempt_at DATETIME(6) NOT NULL,
    last_error VARCHAR(255),
    last_success_at DATETIME(6),
    accepted_raw_sha256 CHAR(64),
    coverage_verified TINYINT NOT NULL DEFAULT 0 CHECK (coverage_verified IN (0, 1)),
    UNIQUE (dataset, market, period, segment)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
