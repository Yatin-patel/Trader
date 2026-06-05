-- MySQL schema for Autonomous Trader
-- Converted from SQL Server schema

-- 1. Global application settings
CREATE TABLE IF NOT EXISTS app_settings (
    setting_key      VARCHAR(128) PRIMARY KEY,
    setting_value    TEXT NULL,
    value_type       VARCHAR(20) NOT NULL DEFAULT 'string',
    category         VARCHAR(64) NOT NULL DEFAULT 'general',
    description      VARCHAR(512) NULL,
    is_secret        TINYINT(1) NOT NULL DEFAULT 0,
    updated_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. Tenant trading projects
CREATE TABLE IF NOT EXISTS trading_projects (
    project_id              VARCHAR(64) NOT NULL PRIMARY KEY,
    project_name            VARCHAR(100) NOT NULL,
    user_id                 CHAR(36) NULL,
    broker_type             VARCHAR(16) NOT NULL DEFAULT 'alpaca',
    alpaca_api_key          VARCHAR(512) NULL,
    alpaca_secret_key       VARCHAR(512) NULL,
    alpaca_base_url         VARCHAR(256) NULL,
    alpaca_data_feed        VARCHAR(16) NOT NULL DEFAULT 'iex',
    etrade_consumer_key     VARCHAR(512) NULL,
    etrade_consumer_secret  VARCHAR(512) NULL,
    etrade_access_token     VARCHAR(512) NULL,
    etrade_access_token_secret VARCHAR(512) NULL,
    etrade_account_id_key   VARCHAR(64) NULL,
    etrade_environment      VARCHAR(16) NULL,
    max_equity_allocation   DECIMAL(18,2) NOT NULL,
    is_active               TINYINT(1) NOT NULL DEFAULT 1,
    created_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Per-project settings
CREATE TABLE IF NOT EXISTS project_settings (
    project_id        VARCHAR(64) NOT NULL,
    setting_key       VARCHAR(128) NOT NULL,
    setting_value     TEXT NULL,
    value_type        VARCHAR(20) NOT NULL DEFAULT 'string',
    updated_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (project_id, setting_key),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. Stock positions
CREATE TABLE IF NOT EXISTS stock_positions (
    position_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id            VARCHAR(64) NOT NULL,
    ticker                VARCHAR(12) NOT NULL,
    entry_price           DECIMAL(18,4) NOT NULL,
    current_price         DECIMAL(18,4) NULL,
    max_loss_threshold    DECIMAL(18,4) NOT NULL,
    quantity              INT NOT NULL,
    status                VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    adjusted_cost_basis   DECIMAL(18,4) NULL,
    opened_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    closed_at             DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_stock_positions_project_status (project_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Wheel contracts
CREATE TABLE IF NOT EXISTS wheel_contracts (
    contract_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id           VARCHAR(64) NOT NULL,
    ticker               VARCHAR(12) NOT NULL,
    strategy_phase       VARCHAR(20) NOT NULL,
    option_symbol        VARCHAR(64) NULL,
    strike_price         DECIMAL(18,4) NOT NULL,
    premium_collected    DECIMAL(18,4) NOT NULL,
    expiration_date      DATE NOT NULL,
    delta_at_entry       DECIMAL(8,4) NULL,
    is_assigned          TINYINT(1) NOT NULL DEFAULT 0,
    is_closed            TINYINT(1) NOT NULL DEFAULT 0,
    cycle_id             BIGINT NULL,
    quantity             INT NOT NULL DEFAULT 1,
    underlying_at_entry  DECIMAL(18,4) NULL,
    settings_snapshot    LONGTEXT NULL,
    opened_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_wheel_contracts_project_status (project_id, is_closed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. Agent events. project_id is NULLable so cross-project audit
-- events (password resets etc.) can live here with project_id NULL.
-- The FK only enforces non-NULL values against trading_projects.
CREATE TABLE IF NOT EXISTS agent_events (
    event_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id    VARCHAR(64) NULL,
    node_name     VARCHAR(64) NOT NULL,
    event_type    VARCHAR(32) NOT NULL,
    payload       TEXT NULL,
    created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_agent_events_project_time (project_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. Closed contracts
CREATE TABLE IF NOT EXISTS closed_contracts (
    closure_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    contract_id          BIGINT NULL,
    project_id           VARCHAR(64) NOT NULL,
    ticker               VARCHAR(12) NOT NULL,
    option_symbol        VARCHAR(64) NULL,
    strategy_phase       VARCHAR(20) NOT NULL,
    opened_at            DATETIME(6) NOT NULL,
    closed_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    days_held            INT NOT NULL,
    strike_price         DECIMAL(18,4) NOT NULL,
    quantity             INT NOT NULL,
    premium_collected    DECIMAL(18,4) NOT NULL,
    close_cost           DECIMAL(18,4) NOT NULL DEFAULT 0,
    realized_pnl         DECIMAL(18,4) NOT NULL,
    closure_reason       VARCHAR(32) NOT NULL,
    delta_at_entry       DECIMAL(8,4) NULL,
    dte_at_entry         INT NULL,
    underlying_at_entry  DECIMAL(18,4) NULL,
    underlying_at_close  DECIMAL(18,4) NULL,
    settings_snapshot    TEXT NULL,
    -- Broker-derived fees. NULL until the fees sync job populates them
    -- from /v2/account/activities (Alpaca) or /v1/accounts/{id}/transactions
    -- (ETrade). A NULL brokerage_fee means "not yet synced" — the P&L
    -- report distinguishes this from a real $0 fee.
    brokerage_fee        DECIMAL(18,4) NULL,
    fee_synced_at        DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_closed_contracts_project_time (project_id, closed_at DESC),
    INDEX IX_closed_contracts_ticker (project_id, ticker),
    INDEX IX_closed_contracts_pending_fees (project_id, fee_synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. Closed positions
CREATE TABLE IF NOT EXISTS closed_positions (
    closure_id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    position_id             BIGINT NULL,
    project_id              VARCHAR(64) NOT NULL,
    ticker                  VARCHAR(12) NOT NULL,
    quantity                INT NOT NULL,
    entry_price             DECIMAL(18,4) NOT NULL,
    exit_price              DECIMAL(18,4) NOT NULL,
    opened_at               DATETIME(6) NOT NULL,
    closed_at               DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    days_held               INT NOT NULL,
    realized_pnl            DECIMAL(18,4) NOT NULL,
    closure_reason          VARCHAR(32) NOT NULL,
    associated_contract_id  BIGINT NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_closed_positions_project_time (project_id, closed_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. Portfolio snapshots
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id         VARCHAR(64) NOT NULL,
    snapshot_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    cash               DECIMAL(18,4) NOT NULL,
    buying_power       DECIMAL(18,4) NOT NULL,
    equity             DECIMAL(18,4) NOT NULL,
    long_market_value  DECIMAL(18,4) NULL,
    short_market_value DECIMAL(18,4) NULL,
    realized_pnl_day   DECIMAL(18,4) NULL,
    unrealized_pnl     DECIMAL(18,4) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_portfolio_snapshots_project_time (project_id, snapshot_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 10. Risk limits
CREATE TABLE IF NOT EXISTS risk_limits (
    limit_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id         VARCHAR(64) NOT NULL,
    limit_type         VARCHAR(64) NOT NULL,
    threshold          DECIMAL(18,4) NOT NULL,
    window_minutes     INT NULL,
    action             VARCHAR(16) NOT NULL DEFAULT 'HALT',
    enabled            TINYINT(1) NOT NULL DEFAULT 1,
    created_at         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    breach_count       INT NOT NULL DEFAULT 0,
    last_breached_at   DATETIME(6) NULL,
    last_breach_value  DECIMAL(18,4) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_risk_limits_project_enabled (project_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 11. IV rank cache
CREATE TABLE IF NOT EXISTS iv_rank_cache (
    ticker          VARCHAR(12) NOT NULL PRIMARY KEY,
    iv_rank         DECIMAL(8,4) NULL,
    realized_vol    DECIMAL(8,4) NULL,
    sample_days     INT NULL,
    fetched_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 12. News sentiment cache
CREATE TABLE IF NOT EXISTS news_sentiment_cache (
    ticker          VARCHAR(12) NOT NULL PRIMARY KEY,
    sentiment_score DECIMAL(8,4) NULL,
    headlines       TEXT NULL,
    rationale       VARCHAR(1024) NULL,
    fetched_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 13. LLM usage tracking
CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id        VARCHAR(64) NULL,
    purpose           VARCHAR(32) NOT NULL,
    provider          VARCHAR(16) NOT NULL,
    model             VARCHAR(64) NOT NULL,
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens      INT NOT NULL DEFAULT 0,
    cost_usd          DECIMAL(10,6) NOT NULL DEFAULT 0,
    cache_hit         TINYINT(1) NOT NULL DEFAULT 0,
    created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX IX_llm_usage_time (created_at DESC),
    INDEX IX_llm_usage_project (project_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 14. AI recommendations
CREATE TABLE IF NOT EXISTS ai_recommendations (
    rec_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id        VARCHAR(64) NOT NULL,
    created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    title             VARCHAR(256) NOT NULL,
    rationale         TEXT NULL,
    suggested_changes TEXT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'pending',
    applied_at        DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_ai_recs_project_time (project_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 15. Anomalies
CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    kind            VARCHAR(64) NOT NULL,
    detected_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    severity        VARCHAR(16) NOT NULL DEFAULT 'warn',
    baseline_value  DECIMAL(18,4) NULL,
    observed_value  DECIMAL(18,4) NULL,
    details         TEXT NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_anomalies_project_time (project_id, detected_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 16. Orders
CREATE TABLE IF NOT EXISTS orders (
    order_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id          VARCHAR(64) NOT NULL,
    alpaca_order_id     VARCHAR(64) NOT NULL,
    symbol              VARCHAR(64) NOT NULL,
    side                VARCHAR(8) NOT NULL,
    order_type          VARCHAR(16) NOT NULL,
    qty                 DECIMAL(18,4) NOT NULL,
    limit_price         DECIMAL(18,4) NULL,
    status              VARCHAR(32) NOT NULL,
    filled_qty          DECIMAL(18,4) NOT NULL DEFAULT 0,
    filled_avg_price    DECIMAL(18,4) NULL,
    submitted_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_polled_at      DATETIME(6) NULL,
    terminal            TINYINT(1) NOT NULL DEFAULT 0,
    related_contract_id BIGINT NULL,
    last_error          VARCHAR(512) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_orders_project_status (project_id, terminal, status),
    UNIQUE INDEX UX_orders_alpaca_id (alpaca_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 17. Reconciliation log
CREATE TABLE IF NOT EXISTS reconciliation_log (
    recon_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    ran_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    mismatches      INT NOT NULL DEFAULT 0,
    auto_sync       TINYINT(1) NOT NULL DEFAULT 0,
    details         TEXT NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_recon_project_time (project_id, ran_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 18. Backup log
CREATE TABLE IF NOT EXISTS backup_log (
    backup_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    started_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at    DATETIME(6) NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'RUNNING',
    path            VARCHAR(512) NULL,
    size_bytes      BIGINT NULL,
    error_message   VARCHAR(512) NULL,
    INDEX IX_backup_log_time (started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 19. Wheel cycles
CREATE TABLE IF NOT EXISTS wheel_cycles (
    cycle_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id          VARCHAR(64) NOT NULL,
    ticker              VARCHAR(12) NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    started_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    ended_at            DATETIME(6) NULL,
    total_premium       DECIMAL(18,4) NOT NULL DEFAULT 0,
    realized_pnl        DECIMAL(18,4) NOT NULL DEFAULT 0,
    csp_count           INT NOT NULL DEFAULT 0,
    cc_count            INT NOT NULL DEFAULT 0,
    assignment_count    INT NOT NULL DEFAULT 0,
    cost_basis_adjusted DECIMAL(18,4) NULL,
    final_exit_price    DECIMAL(18,4) NULL,
    final_outcome       VARCHAR(32) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_wheel_cycles_project_ticker (project_id, ticker, status),
    INDEX IX_wheel_cycles_status_time (project_id, status, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 20. Notification channels
CREATE TABLE IF NOT EXISTS notification_channels (
    channel_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    channel_type    VARCHAR(16) NOT NULL,
    name            VARCHAR(128) NOT NULL,
    target          TEXT NOT NULL,
    config          TEXT NULL,
    events_filter   TEXT NULL,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_sent_at    DATETIME(6) NULL,
    last_error      VARCHAR(512) NULL,
    send_count      INT NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_notification_channels_project (project_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 21. Notifications
CREATE TABLE IF NOT EXISTS notifications (
    notification_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    channel_id      BIGINT NULL,
    title           VARCHAR(256) NOT NULL,
    body            TEXT NULL,
    severity        VARCHAR(16) NOT NULL DEFAULT 'info',
    event_type      VARCHAR(64) NULL,
    payload         TEXT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'queued',
    sent_at         DATETIME(6) NULL,
    read_at         DATETIME(6) NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    error_message   VARCHAR(512) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_notifications_project_time (project_id, created_at DESC),
    INDEX IX_notifications_unread (project_id, read_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 22. Backtest runs
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    name            VARCHAR(128) NULL,
    started_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at    DATETIME(6) NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'running',
    config          TEXT NULL,
    metrics         TEXT NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_backtest_runs_project_time (project_id, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 23. Users table
CREATE TABLE IF NOT EXISTS users (
    user_id         CHAR(36) NOT NULL PRIMARY KEY,
    email           VARCHAR(256) NOT NULL,
    password_hash   VARCHAR(512) NOT NULL,
    display_name    VARCHAR(128) NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 0,
    is_admin        TINYINT(1) NOT NULL DEFAULT 0,
    totp_enabled    TINYINT(1) NOT NULL DEFAULT 0,
    totp_secret     VARCHAR(64) NULL,
    email_verified  TINYINT(1) NOT NULL DEFAULT 0,
    -- 'pending' | 'active' | 'rejected'. New signups land in 'pending'
    -- and only an admin can flip them to 'active'. 'rejected' is
    -- permanent (the signup endpoint refuses re-registration).
    account_status  VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_login_at   DATETIME(6) NULL,
    UNIQUE INDEX UX_users_email (email),
    INDEX IX_users_status (account_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 24. Refresh tokens
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         CHAR(36) NOT NULL,
    token_hash      VARCHAR(512) NOT NULL,
    expires_at      DATETIME(6) NOT NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    revoked_at      DATETIME(6) NULL,
    INDEX IX_refresh_tokens_user (user_id),
    INDEX IX_refresh_tokens_hash (token_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 24b. User sessions
CREATE TABLE IF NOT EXISTS user_sessions (
    session_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         CHAR(36) NOT NULL,
    session_token   CHAR(36) NOT NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at      DATETIME(6) NOT NULL,
    revoked_at      DATETIME(6) NULL,
    ip_address      VARCHAR(45) NULL,
    user_agent      VARCHAR(500) NULL,
    INDEX IX_user_sessions_token (session_token),
    INDEX IX_user_sessions_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Day Trading tables

-- 25. Intraday signals
CREATE TABLE IF NOT EXISTS intraday_signals (
    signal_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    ticker          VARCHAR(12) NOT NULL,
    signal_type     VARCHAR(32) NOT NULL,
    signal_value    DECIMAL(18,4) NULL,
    rsi_value       DECIMAL(8,4) NULL,
    macd_value      DECIMAL(18,4) NULL,
    macd_signal     DECIMAL(18,4) NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at      DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_intraday_signals_project_time (project_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 26. Bracket orders
CREATE TABLE IF NOT EXISTS bracket_orders (
    bracket_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id          VARCHAR(64) NOT NULL,
    parent_order_id     BIGINT NULL,
    take_profit_order_id BIGINT NULL,
    stop_loss_order_id  BIGINT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    created_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_bracket_orders_project (project_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 27. Day trade log
CREATE TABLE IF NOT EXISTS day_trade_log (
    trade_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    symbol          VARCHAR(64) NOT NULL,
    open_order_id   BIGINT NULL,
    close_order_id  BIGINT NULL,
    trade_date      DATE NOT NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_day_trade_log_project_date (project_id, trade_date DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Long-Term Investing tables

-- 28. Dividend events
CREATE TABLE IF NOT EXISTS dividend_events (
    event_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    ticker          VARCHAR(12) NOT NULL,
    ex_date         DATE NOT NULL,
    pay_date        DATE NULL,
    amount          DECIMAL(18,4) NOT NULL,
    shares_held     INT NOT NULL,
    total_amount    DECIMAL(18,4) NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_dividend_events_project_date (project_id, ex_date DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 29. DCA schedules
CREATE TABLE IF NOT EXISTS dca_schedules (
    schedule_id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id          VARCHAR(64) NOT NULL,
    ticker              VARCHAR(12) NOT NULL,
    frequency           VARCHAR(16) NOT NULL,
    amount_dollars      DECIMAL(18,2) NOT NULL,
    next_execution_date DATE NOT NULL,
    enabled             TINYINT(1) NOT NULL DEFAULT 1,
    created_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_executed_at    DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_dca_schedules_project (project_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 30. Target allocations
CREATE TABLE IF NOT EXISTS target_allocations (
    allocation_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id              VARCHAR(64) NOT NULL,
    ticker                  VARCHAR(12) NOT NULL,
    target_pct              DECIMAL(8,4) NOT NULL,
    rebalance_threshold_pct DECIMAL(8,4) NOT NULL DEFAULT 5.0,
    created_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    UNIQUE INDEX UX_target_allocations_project_ticker (project_id, ticker)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 31. Multi-leg orders (for advanced options strategies)
CREATE TABLE IF NOT EXISTS multi_leg_orders (
    order_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    strategy_type   VARCHAR(32) NOT NULL,
    underlying      VARCHAR(12) NOT NULL,
    leg1_symbol     VARCHAR(64) NULL,
    leg1_side       VARCHAR(8) NULL,
    leg1_qty        INT NULL,
    leg2_symbol     VARCHAR(64) NULL,
    leg2_side       VARCHAR(8) NULL,
    leg2_qty        INT NULL,
    leg3_symbol     VARCHAR(64) NULL,
    leg3_side       VARCHAR(8) NULL,
    leg3_qty        INT NULL,
    leg4_symbol     VARCHAR(64) NULL,
    leg4_side       VARCHAR(8) NULL,
    leg4_qty        INT NULL,
    net_credit      DECIMAL(18,4) NULL,
    max_loss        DECIMAL(18,4) NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    closed_at       DATETIME(6) NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_multi_leg_orders_project (project_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 32. Trade journal
CREATE TABLE IF NOT EXISTS trade_journal (
    entry_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL,
    trade_date      DATE NOT NULL,
    ticker          VARCHAR(12) NULL,
    notes           TEXT NULL,
    tags            VARCHAR(256) NULL,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id) ON DELETE CASCADE,
    INDEX IX_trade_journal_project_date (project_id, trade_date DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 33. Earnings cache — keyed by ticker, refreshed from yfinance
CREATE TABLE IF NOT EXISTS earnings_cache (
    ticker              VARCHAR(12) NOT NULL PRIMARY KEY,
    next_earnings_date  DATE NULL,
    fetched_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    source              VARCHAR(32) NOT NULL DEFAULT 'yfinance'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 34. Market outlook cache — composite PK on (ticker, horizon_days)
CREATE TABLE IF NOT EXISTS market_outlook_cache (
    ticker         VARCHAR(12) NOT NULL,
    horizon_days   INT NOT NULL,
    quant_json     LONGTEXT NULL,
    llm_text       LONGTEXT NULL,
    confidence     VARCHAR(16) NULL,
    direction      VARCHAR(16) NULL,
    generated_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (ticker, horizon_days)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 35. User preferences — one row per user
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id          CHAR(36) NOT NULL PRIMARY KEY,
    default_broker   VARCHAR(16) NOT NULL DEFAULT 'alpaca',
    updated_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Column additions that drifted in from SQL Server.
-- These ALTERs are idempotent via the schema applier (which catches
-- "Duplicate column" errors).
ALTER TABLE trading_projects
    ADD COLUMN etrade_token_renewed_at DATETIME(6) NULL;
ALTER TABLE backtest_runs
    ADD COLUMN from_date DATE NOT NULL DEFAULT '1970-01-01';
ALTER TABLE wheel_contracts
    ADD COLUMN settings_snapshot LONGTEXT NULL;
-- Broker-derived fees on closed contracts. NULL means "not yet synced".
ALTER TABLE closed_contracts
    ADD COLUMN brokerage_fee DECIMAL(18,4) NULL;
ALTER TABLE closed_contracts
    ADD COLUMN fee_synced_at DATETIME(6) NULL;
ALTER TABLE closed_contracts
    ADD INDEX IX_closed_contracts_pending_fees (project_id, fee_synced_at);

-- 36. Tax lots (FIFO accounting)
CREATE TABLE IF NOT EXISTS tax_lots (
    lot_id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    project_id          VARCHAR(64) NOT NULL,
    ticker              VARCHAR(12) NOT NULL,
    quantity_opened     INT NOT NULL,
    quantity_remaining  INT NOT NULL,
    cost_per_share      DECIMAL(18,4) NOT NULL,
    opened_at           DATETIME(6) NOT NULL,
    closed_at           DATETIME(6) NULL,
    source              VARCHAR(32) NOT NULL DEFAULT 'assignment',
    linked_contract_id  BIGINT NULL,
    FOREIGN KEY (project_id) REFERENCES trading_projects(project_id)
        ON DELETE CASCADE,
    INDEX IX_tax_lots_project_ticker_opened
        (project_id, ticker, opened_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 37. Tax-lot consumptions (one row per FIFO debit)
CREATE TABLE IF NOT EXISTS tax_lot_consumptions (
    consumption_id  BIGINT AUTO_INCREMENT PRIMARY KEY,
    lot_id          BIGINT NOT NULL,
    project_id      VARCHAR(64) NOT NULL,
    ticker          VARCHAR(12) NOT NULL,
    quantity        INT NOT NULL,
    sale_price      DECIMAL(18,4) NOT NULL,
    proceeds        DECIMAL(18,4) NOT NULL,
    basis           DECIMAL(18,4) NOT NULL,
    realized_pnl    DECIMAL(18,4) NOT NULL,
    holding_days    INT NOT NULL,
    term            VARCHAR(8) NOT NULL,  -- 'short' | 'long'
    closed_at       DATETIME(6) NOT NULL,
    reason          VARCHAR(32) NOT NULL,
    FOREIGN KEY (lot_id) REFERENCES tax_lots(lot_id),
    INDEX IX_tlc_project_year (project_id, closed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
