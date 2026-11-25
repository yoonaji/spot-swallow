#!/bin/sh
# 스팟 워커 전용 DB 계정. 처음 DB 를 만들 때 한 번만 실행된다 (docker-entrypoint-initdb.d).
# 권한은 Flyway 마이그레이션 V3 이 그룹 역할 swallow_worker 에 준다. 여기서는 로그인 계정만 만든다.
# 스팟 = 믿을 수 없는 서버이므로, 워커가 털려도 확정된 결과를 바꾸거나 지울 수 없어야 한다.
set -e
psql -v ON_ERROR_STOP=1 -v pw="${WORKER_DB_PASSWORD:-swallow_worker}" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE swallow_worker NOLOGIN;
CREATE ROLE swallow_worker_app LOGIN PASSWORD :'pw' IN ROLE swallow_worker;
SQL
