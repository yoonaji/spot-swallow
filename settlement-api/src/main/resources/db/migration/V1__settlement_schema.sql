-- 정산 입력 데이터: 한 번 적재되면 바뀌지 않는다 (불변 입력).
CREATE TABLE transactions (
    tx_id           BIGSERIAL      PRIMARY KEY,
    merchant_id     VARCHAR(32)    NOT NULL,
    settlement_date DATE           NOT NULL,
    amount          NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    fee_rate        NUMERIC(6, 4)  NOT NULL CHECK (fee_rate >= 0 AND fee_rate < 1),
    occurred_at     TIMESTAMPTZ    NOT NULL
);
CREATE INDEX idx_transactions_merchant_date ON transactions (merchant_id, settlement_date);

-- 정산 작업: 가맹점 x 정산일 하나가 작업 하나다.
-- (merchant_id, settlement_date) UNIQUE 로 같은 정산 요청이 두 번 접수되는 것을 막는다 (API 멱등성).
CREATE TABLE settlement_jobs (
    job_id          VARCHAR(64)  PRIMARY KEY,
    merchant_id     VARCHAR(32)  NOT NULL,
    settlement_date DATE         NOT NULL,
    status          VARCHAR(16)  NOT NULL,          -- ACCEPTED -> COMPLETED
    accepted_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    CONSTRAINT uq_settlement_jobs_merchant_date UNIQUE (merchant_id, settlement_date)
);
CREATE INDEX idx_settlement_jobs_status ON settlement_jobs (status, accepted_at);

-- 트랜잭셔널 아웃박스: 작업 저장과 같은 트랜잭션으로 기록되고, 발행기가 RabbitMQ 로 보낸다.
CREATE TABLE outbox (
    outbox_id       BIGSERIAL    PRIMARY KEY,
    job_id          VARCHAR(64)  NOT NULL REFERENCES settlement_jobs (job_id),
    payload         TEXT         NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'PENDING',   -- PENDING -> PUBLISHED
    attempts        INT          NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX idx_outbox_pending ON outbox (outbox_id) WHERE status = 'PENDING';

-- 정산 결과: job_id PRIMARY KEY 가 멱등성의 핵심이다.
-- 같은 메시지가 두 번 처리되어도 결과는 한 번만 확정된다.
CREATE TABLE settlement_results (
    job_id          VARCHAR(64)    PRIMARY KEY REFERENCES settlement_jobs (job_id),
    merchant_id     VARCHAR(32)    NOT NULL,
    settlement_date DATE           NOT NULL,
    tx_count        INT            NOT NULL,
    gross_amount    NUMERIC(18, 2) NOT NULL,
    fee_amount      NUMERIC(18, 2) NOT NULL,
    net_amount      NUMERIC(18, 2) NOT NULL,
    worker_id       VARCHAR(64)    NOT NULL,
    completed_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_settlement_results_net CHECK (net_amount = gross_amount - fee_amount)
);

-- 처리 이력 (감사 로그): 누가 언제 무엇을 했는지. 중복 처리 시도도 여기 남는다.
CREATE TABLE job_events (
    event_id        BIGSERIAL    PRIMARY KEY,
    job_id          VARCHAR(64)  NOT NULL,
    event_type      VARCHAR(32)  NOT NULL,  -- ACCEPTED, PUBLISHED, COMPLETED, DUPLICATE_SKIPPED, DEAD_LETTERED
    actor           VARCHAR(64)  NOT NULL,
    detail          TEXT,
    occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_job_events_job ON job_events (job_id);
