-- 마감 등급: 급한 작업(URGENT)은 큐에서 먼저 꺼낸다. 마감 시각은 접수 시각 + 등급별 목표 시간.
ALTER TABLE settlement_jobs ADD COLUMN priority    VARCHAR(8)  NOT NULL DEFAULT 'NORMAL'
    CHECK (priority IN ('URGENT', 'NORMAL'));
ALTER TABLE settlement_jobs ADD COLUMN deadline_at TIMESTAMPTZ;
ALTER TABLE outbox          ADD COLUMN priority    SMALLINT    NOT NULL DEFAULT 0;

-- 긴 작업의 중간 저장. 회수되면 다음 워커가 처음부터가 아니라 저장된 거래 다음부터 계산한다.
-- (last_tx_id, tx_count, gross, fee) 는 "tx_id <= last_tx_id 인 거래의 합계"이고, 입력 거래는 바뀌지 않으므로
-- 어느 워커가 쓰든 같은 지점이면 같은 값이다. 저장은 앞으로만 간다(tx_count 가 늘 때만 덮어쓴다).
-- 최종 결과는 여전히 settlement_results 의 PK 로 한 번만 확정되므로, 이 표는 정합성이 아니라 속도를 위한 것이다.
CREATE TABLE job_checkpoints (
    job_id      VARCHAR(64)    PRIMARY KEY REFERENCES settlement_jobs (job_id),
    last_tx_id  BIGINT         NOT NULL,
    tx_count    INT            NOT NULL,
    gross       NUMERIC(18, 2) NOT NULL,
    fee         NUMERIC(18, 2) NOT NULL,
    worker_id   VARCHAR(64)    NOT NULL,
    updated_at  TIMESTAMPTZ    NOT NULL DEFAULT now()
);
