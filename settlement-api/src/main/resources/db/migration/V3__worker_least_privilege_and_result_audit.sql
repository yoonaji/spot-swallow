-- 스팟 워커는 언제 꺼질지 모르고, 믿을 수도 없는 서버다. 두 가지를 더한다.
--
-- 1) 최소 권한: 워커 계정은 확정된 결과를 "추가"만 할 수 있다. 바꾸거나 지울 수 없다.
--    (GameDay 1회차: 결과 1건을 DB 에서 직접 1원 바꿀 수 있었고, 대사로 사후에 잡기만 했다.)
-- 2) 결과 감사 체인: 결과가 추가·변경·삭제될 때마다 DB 트리거가 해시 체인에 한 줄을 남긴다.
--    누가(DB 계정, 접속 주소) 언제 무엇으로 바꿨는지 남고, 트리거를 끄고 몰래 바꾸면 체인과 결과가 어긋나 대사에서 드러난다.

-- ---------------------------------------------------------------- 1) 워커 최소 권한
-- 로그인 계정(swallow_worker_app)은 DB 를 처음 만들 때 infra/postgres/init 에서 만든다. 여기서는 그룹 역할에 권한만 준다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'swallow_worker') THEN
        CREATE ROLE swallow_worker NOLOGIN;
    END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM swallow_worker;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM swallow_worker;
GRANT USAGE ON SCHEMA public TO swallow_worker;

GRANT SELECT ON transactions TO swallow_worker;                              -- 입력 거래 읽기
GRANT SELECT ON settlement_jobs TO swallow_worker;                           -- 작업 읽기
GRANT UPDATE (status, completed_at) ON settlement_jobs TO swallow_worker;    -- 완료 표시만
GRANT INSERT ON settlement_results TO swallow_worker;                        -- 결과는 추가만 (UPDATE·DELETE 없음)
GRANT SELECT (job_id) ON settlement_results TO swallow_worker;               -- ON CONFLICT (job_id) 검사에 필요한 칸만
GRANT INSERT ON job_events TO swallow_worker;                                -- 이력도 추가만
GRANT USAGE ON SEQUENCE job_events_event_id_seq TO swallow_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON job_checkpoints TO swallow_worker;   -- 중간 저장 (속도용, 정합성과 무관)

-- ---------------------------------------------------------------- 2) 결과 감사 체인
CREATE TABLE result_audit (
    seq         BIGSERIAL    PRIMARY KEY,
    job_id      VARCHAR(64)  NOT NULL,
    action      VARCHAR(8)   NOT NULL,     -- INSERT | UPDATE | DELETE
    payload     TEXT         NOT NULL,     -- 이 시점의 결과 값 (DELETE 면 'DELETED')
    db_user     TEXT         NOT NULL,     -- 접속한 DB 계정 (session_user)
    client_addr TEXT,
    recorded_at TIMESTAMPTZ  NOT NULL,
    prev_hash   CHAR(64)     NOT NULL,
    hash        CHAR(64)     NOT NULL
);
CREATE INDEX idx_result_audit_job ON result_audit (job_id, seq);
REVOKE ALL ON result_audit FROM PUBLIC;

-- 결과 한 줄을 고정된 문자열로 만든다 (세션 설정 DateStyle 등에 흔들리지 않게 형식을 직접 정한다).
CREATE FUNCTION result_payload(r settlement_results) RETURNS TEXT
    LANGUAGE sql STABLE AS $$
    SELECT concat_ws('|', r.job_id, r.merchant_id, to_char(r.settlement_date, 'YYYY-MM-DD'), r.tx_count,
                     r.gross_amount, r.fee_amount, r.net_amount, r.worker_id)
$$;

-- 앞 줄 해시 + 이번 줄 내용 → SHA-256. 대사가 같은 함수로 다시 계산해 비교한다.
CREATE FUNCTION result_audit_hash(prev CHAR(64), s BIGINT, jid TEXT, act TEXT, p TEXT, who TEXT, at TIMESTAMPTZ)
    RETURNS CHAR(64) LANGUAGE sql STABLE AS $$
    SELECT encode(sha256(convert_to(concat_ws('|', prev, s, jid, act, p, who, extract(epoch FROM at)), 'UTF8')), 'hex')
$$;

CREATE FUNCTION result_audit_record(jid TEXT, act TEXT, p TEXT) RETURNS VOID
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    prev CHAR(64);
    s    BIGINT;
    at   TIMESTAMPTZ := clock_timestamp();
BEGIN
    -- 체인은 한 줄로 이어져야 하므로 기록은 한 번에 하나씩 (확정 트랜잭션이 짧아서 대기는 짧다)
    PERFORM pg_advisory_xact_lock(727101);
    SELECT hash INTO prev FROM result_audit ORDER BY seq DESC LIMIT 1;
    prev := coalesce(prev, repeat('0', 64));
    s := nextval(pg_get_serial_sequence('result_audit', 'seq'));
    INSERT INTO result_audit (seq, job_id, action, payload, db_user, client_addr, recorded_at, prev_hash, hash)
    VALUES (s, jid, act, p, session_user, inet_client_addr()::text, at, prev,
            result_audit_hash(prev, s, jid, act, p, session_user, at));
END $$;
-- 워커가 이 함수를 직접 불러 가짜 기록을 남기지 못하게 한다 (트리거를 통해서만 불린다)
REVOKE ALL ON FUNCTION result_audit_record(TEXT, TEXT, TEXT) FROM PUBLIC;

CREATE FUNCTION result_audit_trigger() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM result_audit_record(OLD.job_id, TG_OP, 'DELETED');
    ELSE
        PERFORM result_audit_record(NEW.job_id, TG_OP, result_payload(NEW));
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION result_audit_trigger() FROM PUBLIC;

CREATE TRIGGER trg_settlement_results_audit
    AFTER INSERT OR UPDATE OR DELETE ON settlement_results
    FOR EACH ROW EXECUTE FUNCTION result_audit_trigger();

-- 감사 기록은 추가만 된다. 소유자도 고치거나 지울 수 없다 (트리거를 끄면 가능하지만, 그러면 체인이 깨져 대사에서 드러난다).
CREATE FUNCTION result_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'result_audit is append-only';
END $$;
CREATE TRIGGER trg_result_audit_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON result_audit
    FOR EACH STATEMENT EXECUTE FUNCTION result_audit_append_only();

-- 이미 있던 결과도 체인에 올린다 (기존 DB 에 적용할 때)
DO $$
DECLARE r settlement_results;
BEGIN
    FOR r IN SELECT * FROM settlement_results ORDER BY completed_at, job_id LOOP
        PERFORM result_audit_record(r.job_id, 'INSERT', result_payload(r));
    END LOOP;
END $$;
