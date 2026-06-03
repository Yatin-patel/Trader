/*
  SQL Server Express schema for the autonomous trader.
  Every operational parameter lives in *_settings tables — no hardcoded
  thresholds in application code.
*/

IF DB_ID('TraderDB') IS NULL
    CREATE DATABASE TraderDB;
GO

USE TraderDB;
GO

-- ---------------------------------------------------------------------------
-- 1. Global application settings (singleton key/value store)
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.app_settings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.app_settings (
        setting_key      VARCHAR(128) PRIMARY KEY,
        setting_value    NVARCHAR(MAX) NULL,
        value_type       VARCHAR(20)   NOT NULL DEFAULT 'string', -- string|int|float|bool|json|secret
        category         VARCHAR(64)   NOT NULL DEFAULT 'general',
        description      NVARCHAR(512) NULL,
        is_secret        BIT           NOT NULL DEFAULT 0,
        updated_at       DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- ---------------------------------------------------------------------------
-- 2. Tenant trading projects
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.trading_projects', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.trading_projects (
        project_id              VARCHAR(64)  NOT NULL PRIMARY KEY,
        project_name            NVARCHAR(100) NOT NULL,
        alpaca_api_key          NVARCHAR(512) NOT NULL,    -- encrypted at rest
        alpaca_secret_key       NVARCHAR(512) NOT NULL,    -- encrypted at rest
        alpaca_base_url         NVARCHAR(256) NOT NULL,
        alpaca_data_feed        VARCHAR(16)   NOT NULL DEFAULT 'iex',
        max_equity_allocation   DECIMAL(18,2) NOT NULL,
        is_active               BIT           NOT NULL DEFAULT 1,
        created_at              DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at              DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- ---------------------------------------------------------------------------
-- 3. Per-project risk + strategy parameters (replaces hardcoded values)
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.project_settings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.project_settings (
        project_id        VARCHAR(64)  NOT NULL,
        setting_key       VARCHAR(128) NOT NULL,
        setting_value     NVARCHAR(MAX) NULL,
        value_type        VARCHAR(20)  NOT NULL DEFAULT 'string',
        updated_at        DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_project_settings PRIMARY KEY (project_id, setting_key),
        CONSTRAINT FK_project_settings_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
END
GO

-- ---------------------------------------------------------------------------
-- 4. Stock positions with configurable loss threshold
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.stock_positions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.stock_positions (
        position_id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id            VARCHAR(64)  NOT NULL,
        ticker                VARCHAR(12)  NOT NULL,
        entry_price           DECIMAL(18,4) NOT NULL,
        current_price         DECIMAL(18,4) NULL,
        max_loss_threshold    DECIMAL(18,4) NOT NULL,   -- computed at insert from project_settings
        quantity              INT           NOT NULL,
        status                VARCHAR(20)   NOT NULL DEFAULT 'OPEN', -- OPEN|CLOSED|STOPPED_OUT
        opened_at             DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        closed_at             DATETIME2     NULL,
        CONSTRAINT FK_stock_positions_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_stock_positions_project_status
        ON dbo.stock_positions(project_id, status);
END
GO

-- ---------------------------------------------------------------------------
-- 5. Options wheel state machine
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.wheel_contracts', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.wheel_contracts (
        contract_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id         VARCHAR(64)   NOT NULL,
        ticker             VARCHAR(12)   NOT NULL,
        strategy_phase     VARCHAR(20)   NOT NULL, -- CASH_SECURED_PUT|STOCK_ASSIGNED|COVERED_CALL
        option_symbol      VARCHAR(64)   NULL,
        strike_price       DECIMAL(18,4) NOT NULL,
        premium_collected  DECIMAL(18,4) NOT NULL,
        expiration_date    DATE          NOT NULL,
        delta_at_entry     DECIMAL(8,4)  NULL,
        is_assigned        BIT           NOT NULL DEFAULT 0,
        is_closed          BIT           NOT NULL DEFAULT 0,
        opened_at          DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at         DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_wheel_contracts_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_wheel_contracts_project_status
        ON dbo.wheel_contracts(project_id, is_closed);
END
GO

-- ---------------------------------------------------------------------------
-- 6. Decision / execution audit log
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.agent_events', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.agent_events (
        event_id      BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id    VARCHAR(64)  NOT NULL,
        node_name     VARCHAR(64)  NOT NULL,
        event_type    VARCHAR(32)  NOT NULL, -- SCAN|DECIDE|RISK|EXECUTE|LOOP|ERROR
        payload       NVARCHAR(MAX) NULL,    -- JSON
        created_at    DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_agent_events_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_agent_events_project_time
        ON dbo.agent_events(project_id, created_at DESC);
END
GO

-- ---------------------------------------------------------------------------
-- 7. Analytics tables (P&L, equity curve, performance attribution)
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.closed_contracts', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.closed_contracts (
        closure_id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        contract_id          BIGINT NULL,
        project_id           VARCHAR(64) NOT NULL,
        ticker               VARCHAR(12) NOT NULL,
        option_symbol        VARCHAR(64) NULL,
        strategy_phase       VARCHAR(20) NOT NULL,
        opened_at            DATETIME2 NOT NULL,
        closed_at            DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
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
        settings_snapshot    NVARCHAR(MAX) NULL,
        CONSTRAINT FK_closed_contracts_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_closed_contracts_project_time
        ON dbo.closed_contracts(project_id, closed_at DESC);
    CREATE INDEX IX_closed_contracts_ticker
        ON dbo.closed_contracts(project_id, ticker);
END
GO

IF OBJECT_ID('dbo.closed_positions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.closed_positions (
        closure_id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        position_id             BIGINT NULL,
        project_id              VARCHAR(64) NOT NULL,
        ticker                  VARCHAR(12) NOT NULL,
        quantity                INT NOT NULL,
        entry_price             DECIMAL(18,4) NOT NULL,
        exit_price              DECIMAL(18,4) NOT NULL,
        opened_at               DATETIME2 NOT NULL,
        closed_at               DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        days_held               INT NOT NULL,
        realized_pnl            DECIMAL(18,4) NOT NULL,
        closure_reason          VARCHAR(32) NOT NULL,
        associated_contract_id  BIGINT NULL,
        CONSTRAINT FK_closed_positions_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_closed_positions_project_time
        ON dbo.closed_positions(project_id, closed_at DESC);
END
GO

IF OBJECT_ID('dbo.portfolio_snapshots', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.portfolio_snapshots (
        snapshot_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id         VARCHAR(64) NOT NULL,
        snapshot_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        cash               DECIMAL(18,4) NOT NULL,
        buying_power       DECIMAL(18,4) NOT NULL,
        equity             DECIMAL(18,4) NOT NULL,
        long_market_value  DECIMAL(18,4) NULL,
        short_market_value DECIMAL(18,4) NULL,
        realized_pnl_day   DECIMAL(18,4) NULL,
        unrealized_pnl     DECIMAL(18,4) NULL,
        CONSTRAINT FK_portfolio_snapshots_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_portfolio_snapshots_project_time
        ON dbo.portfolio_snapshots(project_id, snapshot_at DESC);
END
GO

-- Risk limits / kill switches per project
IF OBJECT_ID('dbo.risk_limits', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.risk_limits (
        limit_id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id         VARCHAR(64) NOT NULL,
        limit_type         VARCHAR(64) NOT NULL,  -- daily_loss|drawdown_pct|consecutive_losses|error_storm
        threshold          DECIMAL(18,4) NOT NULL,
        window_minutes     INT NULL,              -- for error_storm
        action             VARCHAR(16) NOT NULL DEFAULT 'HALT',  -- HALT|LIQUIDATE
        enabled            BIT NOT NULL DEFAULT 1,
        created_at         DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        breach_count       INT NOT NULL DEFAULT 0,
        last_breached_at   DATETIME2 NULL,
        last_breach_value  DECIMAL(18,4) NULL,
        CONSTRAINT FK_risk_limits_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_risk_limits_project_enabled
        ON dbo.risk_limits(project_id, enabled);
END
GO

-- IV rank cache (per-ticker 1-year realized-vol rank)
IF OBJECT_ID('dbo.iv_rank_cache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.iv_rank_cache (
        ticker          VARCHAR(12) NOT NULL PRIMARY KEY,
        iv_rank         DECIMAL(8,4) NULL,    -- 0..1
        realized_vol    DECIMAL(8,4) NULL,
        sample_days     INT NULL,
        fetched_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- News sentiment cache (per-ticker)
IF OBJECT_ID('dbo.news_sentiment_cache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.news_sentiment_cache (
        ticker          VARCHAR(12) NOT NULL PRIMARY KEY,
        sentiment_score DECIMAL(8,4) NULL,    -- -1..+1
        headlines       NVARCHAR(MAX) NULL,   -- JSON
        rationale       NVARCHAR(1024) NULL,
        fetched_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- LLM usage (Cat 8.1): per-call token + cost tracking
IF OBJECT_ID('dbo.llm_usage', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.llm_usage (
        usage_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NULL,
        purpose         VARCHAR(32) NOT NULL,   -- strategist|chat|recommendations
        provider        VARCHAR(16) NOT NULL,
        model           VARCHAR(64) NOT NULL,
        prompt_tokens   INT NOT NULL DEFAULT 0,
        completion_tokens INT NOT NULL DEFAULT 0,
        total_tokens    INT NOT NULL DEFAULT 0,
        cost_usd        DECIMAL(10,6) NOT NULL DEFAULT 0,
        cache_hit       BIT NOT NULL DEFAULT 0,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_llm_usage_time ON dbo.llm_usage(created_at DESC);
    CREATE INDEX IX_llm_usage_project ON dbo.llm_usage(project_id, created_at DESC);
END
GO

-- AI recommendations (Cat 10.1)
IF OBJECT_ID('dbo.ai_recommendations', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ai_recommendations (
        rec_id          BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        title           NVARCHAR(256) NOT NULL,
        rationale       NVARCHAR(MAX) NULL,
        suggested_changes NVARCHAR(MAX) NULL,  -- JSON {setting_key: new_value}
        status          VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
        applied_at      DATETIME2 NULL,
        CONSTRAINT FK_ai_recs_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_ai_recs_project_time
        ON dbo.ai_recommendations(project_id, created_at DESC);
END
GO

-- Anomaly events (Cat 10.3)
IF OBJECT_ID('dbo.anomalies', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.anomalies (
        anomaly_id      BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        kind            VARCHAR(64) NOT NULL,
        detected_at     DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        severity        VARCHAR(16) NOT NULL DEFAULT 'warn',
        baseline_value  DECIMAL(18,4) NULL,
        observed_value  DECIMAL(18,4) NULL,
        details         NVARCHAR(MAX) NULL,
        CONSTRAINT FK_anomalies_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_anomalies_project_time
        ON dbo.anomalies(project_id, detected_at DESC);
END
GO

-- Order lifecycle tracker: every order submitted to Alpaca
IF OBJECT_ID('dbo.orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.orders (
        order_id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id         VARCHAR(64) NOT NULL,
        alpaca_order_id    VARCHAR(64) NOT NULL,
        symbol             VARCHAR(64) NOT NULL,
        side               VARCHAR(8) NOT NULL,
        order_type         VARCHAR(16) NOT NULL,
        qty                DECIMAL(18,4) NOT NULL,
        limit_price        DECIMAL(18,4) NULL,
        status             VARCHAR(32) NOT NULL,
        filled_qty         DECIMAL(18,4) NOT NULL DEFAULT 0,
        filled_avg_price   DECIMAL(18,4) NULL,
        submitted_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_polled_at     DATETIME2 NULL,
        terminal           BIT NOT NULL DEFAULT 0,
        related_contract_id BIGINT NULL,
        last_error         NVARCHAR(512) NULL,
        CONSTRAINT FK_orders_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_orders_project_status
        ON dbo.orders(project_id, terminal, status);
    CREATE UNIQUE INDEX UX_orders_alpaca_id
        ON dbo.orders(alpaca_order_id);
END
GO

-- Reconciliation log
IF OBJECT_ID('dbo.reconciliation_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.reconciliation_log (
        recon_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        ran_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        mismatches      INT NOT NULL DEFAULT 0,
        auto_sync       BIT NOT NULL DEFAULT 0,
        details         NVARCHAR(MAX) NULL,
        CONSTRAINT FK_reconciliation_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_recon_project_time
        ON dbo.reconciliation_log(project_id, ran_at DESC);
END
GO

-- DB backups log
IF OBJECT_ID('dbo.backup_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.backup_log (
        backup_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
        started_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        completed_at    DATETIME2 NULL,
        status          VARCHAR(16) NOT NULL DEFAULT 'RUNNING',
        path            NVARCHAR(512) NULL,
        size_bytes      BIGINT NULL,
        error_message   NVARCHAR(512) NULL
    );
    CREATE INDEX IX_backup_log_time
        ON dbo.backup_log(started_at DESC);
END
GO

-- Wheel cycles: one row per ticker-lifetime (first CSP → shares-out)
IF OBJECT_ID('dbo.wheel_cycles', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.wheel_cycles (
        cycle_id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id          VARCHAR(64) NOT NULL,
        ticker              VARCHAR(12) NOT NULL,
        status              VARCHAR(16) NOT NULL DEFAULT 'OPEN',  -- OPEN|CLOSED
        started_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        ended_at            DATETIME2 NULL,
        total_premium       DECIMAL(18,4) NOT NULL DEFAULT 0,
        realized_pnl        DECIMAL(18,4) NOT NULL DEFAULT 0,
        csp_count           INT NOT NULL DEFAULT 0,
        cc_count            INT NOT NULL DEFAULT 0,
        assignment_count    INT NOT NULL DEFAULT 0,
        cost_basis_adjusted DECIMAL(18,4) NULL,
        final_exit_price    DECIMAL(18,4) NULL,
        final_outcome       VARCHAR(32) NULL,  -- EXPIRED_OUT|CALLED_AWAY|SOLD|STOPPED
        CONSTRAINT FK_wheel_cycles_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_wheel_cycles_project_ticker
        ON dbo.wheel_cycles(project_id, ticker, status);
    CREATE INDEX IX_wheel_cycles_status_time
        ON dbo.wheel_cycles(project_id, status, started_at DESC);
END
GO

-- Add adjusted_cost_basis to stock_positions if missing (covered call uses it)
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'adjusted_cost_basis'
                 AND Object_ID = Object_ID(N'dbo.stock_positions'))
BEGIN
    ALTER TABLE dbo.stock_positions ADD adjusted_cost_basis DECIMAL(18,4) NULL;
END
GO

-- Link contracts to their wheel_cycles
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'cycle_id'
                 AND Object_ID = Object_ID(N'dbo.wheel_contracts'))
BEGIN
    ALTER TABLE dbo.wheel_contracts ADD cycle_id BIGINT NULL;
END
GO

-- Notification channels (per-project external notification configs)
IF OBJECT_ID('dbo.notification_channels', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.notification_channels (
        channel_id      BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        channel_type    VARCHAR(16) NOT NULL,    -- discord|email|slack|in_app
        name            NVARCHAR(128) NOT NULL,
        target          NVARCHAR(MAX) NOT NULL,  -- webhook URL or email address
        config          NVARCHAR(MAX) NULL,      -- JSON for extras (SMTP host etc.)
        events_filter   NVARCHAR(MAX) NULL,      -- JSON array of event_types (null = all important)
        enabled         BIT NOT NULL DEFAULT 1,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_sent_at    DATETIME2 NULL,
        last_error      NVARCHAR(512) NULL,
        send_count      INT NOT NULL DEFAULT 0,
        CONSTRAINT FK_notification_channels_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_notification_channels_project
        ON dbo.notification_channels(project_id, enabled);
END
GO

-- Notifications history (audit + in-app feed)
IF OBJECT_ID('dbo.notifications', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.notifications (
        notification_id BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        channel_id      BIGINT NULL,
        title           NVARCHAR(256) NOT NULL,
        body            NVARCHAR(MAX) NULL,
        severity        VARCHAR(16) NOT NULL DEFAULT 'info',  -- info|warn|error|critical
        event_type      VARCHAR(64) NULL,
        payload         NVARCHAR(MAX) NULL,
        status          VARCHAR(16) NOT NULL DEFAULT 'queued', -- queued|sent|failed|in_app
        sent_at         DATETIME2 NULL,
        read_at         DATETIME2 NULL,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        error_message   NVARCHAR(512) NULL,
        CONSTRAINT FK_notifications_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_notifications_project_time
        ON dbo.notifications(project_id, created_at DESC);
    CREATE INDEX IX_notifications_unread
        ON dbo.notifications(project_id, read_at)
        WHERE read_at IS NULL;
END
GO

-- Backtest runs (history of completed backtests)
IF OBJECT_ID('dbo.backtest_runs', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.backtest_runs (
        run_id          BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        name            NVARCHAR(128) NOT NULL,
        from_date       DATE NOT NULL,
        to_date         DATE NOT NULL,
        started_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        completed_at    DATETIME2 NULL,
        status          VARCHAR(16) NOT NULL DEFAULT 'RUNNING',
        params          NVARCHAR(MAX) NULL,    -- JSON
        result          NVARCHAR(MAX) NULL,    -- JSON
        CONSTRAINT FK_backtest_runs_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
END
GO

-- Earnings cache (per-ticker next earnings date)
IF OBJECT_ID('dbo.earnings_cache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.earnings_cache (
        ticker             VARCHAR(12) NOT NULL PRIMARY KEY,
        next_earnings_date DATE NULL,
        fetched_at         DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        source             VARCHAR(32) NOT NULL DEFAULT 'yfinance'
    );
END
GO

-- Add settings_snapshot to wheel_contracts (for attribution) if missing
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'settings_snapshot'
                 AND Object_ID = Object_ID(N'dbo.wheel_contracts'))
BEGIN
    ALTER TABLE dbo.wheel_contracts ADD settings_snapshot NVARCHAR(MAX) NULL;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'underlying_at_entry'
                 AND Object_ID = Object_ID(N'dbo.wheel_contracts'))
BEGIN
    ALTER TABLE dbo.wheel_contracts ADD underlying_at_entry DECIMAL(18,4) NULL;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'quantity'
                 AND Object_ID = Object_ID(N'dbo.wheel_contracts'))
BEGIN
    ALTER TABLE dbo.wheel_contracts ADD quantity INT NOT NULL DEFAULT 1;
END
GO

-- ---------------------------------------------------------------------------
-- AUTH: users + sessions + per-user project ownership
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'users')
BEGIN
    CREATE TABLE dbo.users (
        user_id          UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        email            VARCHAR(320)     NOT NULL UNIQUE,
        password_hash    VARCHAR(255)     NOT NULL,
        totp_secret      VARCHAR(64)      NULL,
        totp_enabled     BIT              NOT NULL DEFAULT 0,
        email_verified   BIT              NOT NULL DEFAULT 0,
        is_admin         BIT              NOT NULL DEFAULT 0,
        is_active        BIT              NOT NULL DEFAULT 1,
        created_at       DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
        last_login_at    DATETIME2        NULL
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'user_sessions')
BEGIN
    CREATE TABLE dbo.user_sessions (
        session_token    UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        user_id          UNIQUEIDENTIFIER NOT NULL,
        created_at       DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
        expires_at       DATETIME2        NOT NULL,
        ip_address       VARCHAR(45)      NULL,
        user_agent       VARCHAR(500)     NULL,
        CONSTRAINT fk_session_user FOREIGN KEY (user_id)
            REFERENCES dbo.users(user_id) ON DELETE CASCADE
    );
    CREATE INDEX ix_user_sessions_user ON dbo.user_sessions(user_id);
END
GO

-- Add user_id to trading_projects (nullable for migration; backfill below)
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'user_id'
                 AND Object_ID = Object_ID(N'dbo.trading_projects'))
BEGIN
    ALTER TABLE dbo.trading_projects ADD user_id UNIQUEIDENTIFIER NULL;
END
GO

-- Broker selection (Phase 1: 'alpaca' | 'etrade'). Defaults to 'alpaca'
-- for backward compatibility with existing rows.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'broker_type'
                 AND Object_ID = Object_ID(N'dbo.trading_projects'))
BEGIN
    ALTER TABLE dbo.trading_projects
        ADD broker_type VARCHAR(16) NOT NULL DEFAULT 'alpaca';
END
GO

-- ETrade-specific credentials. Stored encrypted via Fernet.
-- Consumer key/secret are app-level developer creds; access token is
-- per-user issued after OAuth dance and expires daily at midnight ET.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE Name = N'etrade_consumer_key'
                 AND Object_ID = Object_ID(N'dbo.trading_projects'))
BEGIN
    ALTER TABLE dbo.trading_projects ADD
        etrade_consumer_key       NVARCHAR(MAX) NULL,
        etrade_consumer_secret    NVARCHAR(MAX) NULL,
        etrade_access_token       NVARCHAR(MAX) NULL,
        etrade_access_token_secret NVARCHAR(MAX) NULL,
        etrade_account_id_key     VARCHAR(64)   NULL,
        etrade_environment        VARCHAR(16)   NULL,
        etrade_token_renewed_at   DATETIME2     NULL;
END
GO

-- Per-user preferences (e.g. default broker for new projects).
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'user_preferences')
BEGIN
    CREATE TABLE dbo.user_preferences (
        user_id  UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        default_broker VARCHAR(16) NOT NULL DEFAULT 'alpaca',
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_user_prefs FOREIGN KEY (user_id)
            REFERENCES dbo.users(user_id) ON DELETE CASCADE
    );
END
GO

-- Market Outlook cache: stores per-ticker / per-horizon quant + LLM forecasts.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'market_outlook_cache')
BEGIN
    CREATE TABLE dbo.market_outlook_cache (
        ticker         VARCHAR(12)  NOT NULL,
        horizon_days   INT          NOT NULL,
        quant_json     NVARCHAR(MAX) NULL,
        llm_text       NVARCHAR(MAX) NULL,
        confidence     VARCHAR(16)  NULL,
        direction      VARCHAR(16)  NULL,
        generated_at   DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT pk_market_outlook PRIMARY KEY (ticker, horizon_days)
    );
END
GO

-- ---------------------------------------------------------------------------
-- 8. Seed default global settings (all UI-editable)
-- ---------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- 9. Day Trading Tables (Phase 2)
-- ---------------------------------------------------------------------------

-- Intraday trading signals (RSI/MACD)
IF OBJECT_ID('dbo.intraday_signals', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.intraday_signals (
        signal_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        ticker          VARCHAR(12) NOT NULL,
        signal_type     VARCHAR(20) NOT NULL,  -- BUY|SELL|NEUTRAL
        signal_value    DECIMAL(8,4) NOT NULL,
        rsi             DECIMAL(8,4) NULL,
        macd_line       DECIMAL(12,6) NULL,
        macd_signal     DECIMAL(12,6) NULL,
        macd_histogram  DECIMAL(12,6) NULL,
        vwap            DECIMAL(18,4) NULL,
        underlying_price DECIMAL(18,4) NULL,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        expires_at      DATETIME2 NOT NULL,
        CONSTRAINT FK_intraday_signals_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_intraday_signals_project_ticker
        ON dbo.intraday_signals(project_id, ticker, created_at DESC);
END
GO

-- Bracket (OCO) orders
IF OBJECT_ID('dbo.bracket_orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.bracket_orders (
        bracket_id              BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id              VARCHAR(64) NOT NULL,
        parent_order_id         VARCHAR(64) NOT NULL,
        take_profit_order_id    VARCHAR(64) NULL,
        stop_loss_order_id      VARCHAR(64) NULL,
        symbol                  VARCHAR(64) NOT NULL,
        parent_qty              DECIMAL(18,4) NOT NULL,
        take_profit_price       DECIMAL(18,4) NULL,
        stop_loss_price         DECIMAL(18,4) NULL,
        status                  VARCHAR(20) NOT NULL DEFAULT 'OPEN',
        created_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        closed_at               DATETIME2 NULL,
        exit_reason             VARCHAR(32) NULL,
        CONSTRAINT FK_bracket_orders_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_bracket_orders_project_status
        ON dbo.bracket_orders(project_id, status);
END
GO

-- Day trade log (PDT tracking)
IF OBJECT_ID('dbo.day_trade_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.day_trade_log (
        trade_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        symbol          VARCHAR(64) NOT NULL,
        open_order_id   VARCHAR(64) NOT NULL,
        close_order_id  VARCHAR(64) NOT NULL,
        trade_date      DATETIME2 NOT NULL,
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_day_trade_log_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_day_trade_log_project_date
        ON dbo.day_trade_log(project_id, trade_date);
END
GO

-- ---------------------------------------------------------------------------
-- 10. Long-Term Investing Tables (Phase 3)
-- ---------------------------------------------------------------------------

-- Dividend events
IF OBJECT_ID('dbo.dividend_events', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dividend_events (
        event_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        ticker          VARCHAR(12) NOT NULL,
        ex_date         DATE NOT NULL,
        record_date     DATE NULL,
        pay_date        DATE NULL,
        amount          DECIMAL(18,6) NOT NULL,
        shares_held     INT NOT NULL,
        total_amount    DECIMAL(18,4) NOT NULL,
        status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
        created_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_dividend_events_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_dividend_events_project_ticker
        ON dbo.dividend_events(project_id, ticker, ex_date);
END
GO

-- DCA schedules
IF OBJECT_ID('dbo.dca_schedules', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dca_schedules (
        schedule_id             BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id              VARCHAR(64) NOT NULL,
        ticker                  VARCHAR(12) NOT NULL,
        frequency               VARCHAR(20) NOT NULL DEFAULT 'weekly',
        amount_dollars          DECIMAL(18,2) NOT NULL,
        next_execution_date     DATE NOT NULL,
        last_execution_date     DATE NULL,
        enabled                 BIT NOT NULL DEFAULT 1,
        total_invested          DECIMAL(18,2) NOT NULL DEFAULT 0,
        total_shares            DECIMAL(18,6) NOT NULL DEFAULT 0,
        execution_count         INT NOT NULL DEFAULT 0,
        created_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_dca_schedules_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_dca_schedules_project_enabled
        ON dbo.dca_schedules(project_id, enabled, next_execution_date);
END
GO

-- Target allocations for rebalancing
IF OBJECT_ID('dbo.target_allocations', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.target_allocations (
        allocation_id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id              VARCHAR(64) NOT NULL,
        ticker                  VARCHAR(12) NOT NULL,
        target_pct              DECIMAL(8,4) NOT NULL,
        rebalance_threshold_pct DECIMAL(8,4) NOT NULL DEFAULT 0.05,
        created_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at              DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_target_allocations_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE,
        CONSTRAINT UQ_target_allocations UNIQUE (project_id, ticker)
    );
    CREATE INDEX IX_target_allocations_project
        ON dbo.target_allocations(project_id);
END
GO

-- ---------------------------------------------------------------------------
-- 11. Advanced Options Tables (Phase 4)
-- ---------------------------------------------------------------------------

-- Multi-leg orders (iron condors, spreads, etc.)
IF OBJECT_ID('dbo.multi_leg_orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.multi_leg_orders (
        order_id        BIGINT IDENTITY(1,1) PRIMARY KEY,
        project_id      VARCHAR(64) NOT NULL,
        strategy_type   VARCHAR(32) NOT NULL,
        underlying      VARCHAR(12) NOT NULL,
        status          VARCHAR(20) NOT NULL DEFAULT 'OPEN',
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
        max_profit      DECIMAL(18,4) NULL,
        expiration      DATE NULL,
        opened_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        closed_at       DATETIME2 NULL,
        realized_pnl    DECIMAL(18,4) NULL,
        CONSTRAINT FK_multi_leg_orders_project FOREIGN KEY (project_id)
            REFERENCES dbo.trading_projects(project_id) ON DELETE CASCADE
    );
    CREATE INDEX IX_multi_leg_orders_project_status
        ON dbo.multi_leg_orders(project_id, status);
    CREATE INDEX IX_multi_leg_orders_strategy
        ON dbo.multi_leg_orders(project_id, strategy_type, status);
END
GO

-- ---------------------------------------------------------------------------
-- 12. Seed default global settings (all UI-editable)
-- ---------------------------------------------------------------------------
MERGE dbo.app_settings AS tgt
USING (VALUES
    ('llm_provider',             'anthropic',                       'string', 'llm',      'Which LLM to use: anthropic | google',                               0),
    ('anthropic_api_key',        '',                                'secret', 'llm',      'Anthropic API key for Claude',                                       1),
    ('anthropic_model',          'claude-sonnet-4-6',               'string', 'llm',      'Claude model id used by the Strategist agent',                       0),
    ('google_api_key',           '',                                'secret', 'llm',      'Google AI Studio API key for Gemini',                                1),
    ('google_model',             'gemini-2.5-flash',                'string', 'llm',      'Gemini model id (gemini-2.5-flash is the free-tier default)',        0),
    ('anthropic_temperature',    '0.2',                             'float',  'llm',      'LLM temperature for strategy decisions',                             0),
    ('anthropic_max_tokens',     '2048',                            'int',    'llm',      'Maximum tokens per Claude completion',                               0),
    ('loop_interval_seconds',    '60',                              'int',    'runtime',  'Seconds between full scan->execute cycles per tenant',               0),
    ('market_hours_only',        'true',                            'bool',   'runtime',  'Only run cycles during US market hours',                             0),
    ('max_concurrent_tenants',   '8',                               'int',    'runtime',  'Maximum tenants executing in parallel',                              0),
    ('log_level',                'INFO',                            'string', 'runtime',  'Application log level',                                              0),
    ('daily_digest_enabled',     'false',                           'bool',   'notifications', 'Send a daily summary digest each morning',                       0),
    ('daily_digest_hour_utc',    '13',                              'int',    'notifications', 'Hour (UTC) to send daily digest (13 UTC = 9 AM ET)',             0),
    ('backup_enabled',           'true',                            'bool',   'reliability',   'Automatically back up the SQL Server database each night',       0),
    ('backup_hour_utc',          '7',                               'int',    'reliability',   'Hour (UTC) the daily backup runs',                               0),
    ('backup_dir',               'C:\\trader_backups',              'string', 'reliability',   'Directory the SQL Server service can write backups to',          0),
    ('backup_retention_days',    '14',                              'int',    'reliability',   'Delete .bak files older than this many days',                    0),
    ('reconcile_interval_min',   '15',                              'int',    'reliability',   'Reconciliation cycle interval (0 disables)',                     0),
    ('reconcile_auto_sync',      'false',                           'bool',   'reliability',   'When a DB row is missing from Alpaca, automatically mark closed', 0),
    ('order_poll_interval_sec',  '30',                              'int',    'reliability',   'Seconds between order status polls (0 disables)',                0)
) AS src(setting_key, setting_value, value_type, category, description, is_secret)
ON tgt.setting_key = src.setting_key
WHEN NOT MATCHED THEN
    INSERT (setting_key, setting_value, value_type, category, description, is_secret)
    VALUES (src.setting_key, src.setting_value, src.value_type, src.category, src.description, src.is_secret);
GO
