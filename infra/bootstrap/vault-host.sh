#!/bin/bash
# 측정용 Vault + Postgres 호스트 (안정 영역 역할). tools/measure/security_overhead.py 가 user-data 로 넣는다.
# __WATCHDOG_MIN__, __N_SECRET_IDS__ 는 드라이버가 채운다.
#
# 하는 일
#   1. Postgres 15: DB swallow, 최소 권한 그룹 롤 swallow_worker (결과 테이블 SELECT/INSERT 만)
#   2. Vault: 자체 서명 TLS(사설 IP SAN), file 저장소, 1-of-1 unseal
#      - database 엔진: 워커가 요청할 때마다 swallow_worker 에 속한 1시간짜리 DB 계정을 새로 만든다
#      - approle: 워커 정책은 database/creds/worker 읽기만. secret_id 는 1회용
#   3. 응답 래핑한 1회용 secret_id 를 미리 N 개 만들어 콘솔에 남긴다.
#      실제 운영에서는 제어기(안정 영역)가 워커를 띄우기 직전에 하나씩 발급해 user-data 로 넘긴다.
#      측정 계정은 이 호스트의 8200 포트에 밖에서 접근할 수 없어서(보안 그룹 변경 권한 없음) 콘솔로 전달한다.
#      래핑 토큰은 한 번만 풀 수 있으므로, 누가 먼저 풀었는지 워커가 알아챌 수 있다(unwrap 실패).
mark() { echo "SWALLOW_MARK $1 $(date +%s%3N)" > /dev/console; }
say() { echo "$*" > /dev/console; }

shutdown -h +__WATCHDOG_MIN__ >/dev/null 2>&1 || true
mark host_boot

dnf install -y -q postgresql15-server openssl jq dnf-plugins-core >/dev/null 2>&1
dnf config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo >/dev/null 2>&1
dnf install -y -q vault >/dev/null 2>&1 || { say "SWALLOW_VAULT error vault-install"; exit 1; }
mark packages

# ---- Postgres
postgresql-setup --initdb >/dev/null 2>&1
PGDATA=/var/lib/pgsql/data
echo "listen_addresses = '*'" >> $PGDATA/postgresql.conf
sed -i '1i host all all 127.0.0.1/32 scram-sha-256\nhost all all 172.31.0.0/16 scram-sha-256' $PGDATA/pg_hba.conf
systemctl enable --now postgresql >/dev/null 2>&1
PGPW=$(openssl rand -hex 16)
sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<SQL || say "SWALLOW_VAULT error postgres"
ALTER USER postgres PASSWORD '$PGPW';
CREATE DATABASE swallow;
\c swallow
CREATE TABLE settlement_result (job_id text PRIMARY KEY, amount numeric NOT NULL);
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE ROLE swallow_worker NOLOGIN;
GRANT CONNECT ON DATABASE swallow TO swallow_worker;
GRANT USAGE ON SCHEMA public TO swallow_worker;
GRANT SELECT, INSERT ON settlement_result TO swallow_worker;
SQL
mark postgres_ready

# ---- Vault
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)
mkdir -p /etc/vault.d/tls /opt/vault/data
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$IP" \
  -addext "subjectAltName=IP:$IP,IP:127.0.0.1" \
  -keyout /etc/vault.d/tls/key.pem -out /etc/vault.d/tls/cert.pem >/dev/null 2>&1
cat > /etc/vault.d/vault.hcl <<EOF
storage "file" { path = "/opt/vault/data" }
listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "/etc/vault.d/tls/cert.pem"
  tls_key_file  = "/etc/vault.d/tls/key.pem"
}
api_addr      = "https://$IP:8200"
disable_mlock = true
EOF
chown -R vault:vault /etc/vault.d /opt/vault
systemctl start vault
export VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/etc/vault.d/tls/cert.pem
for i in $(seq 1 60); do vault status >/dev/null 2>&1; [ $? -eq 2 ] && break; sleep 1; done

INIT=$(vault operator init -key-shares=1 -key-threshold=1 -format=json)
vault operator unseal "$(echo "$INIT" | jq -r '.unseal_keys_b64[0]')" >/dev/null
export VAULT_TOKEN=$(echo "$INIT" | jq -r .root_token)
for i in $(seq 1 30); do vault status >/dev/null 2>&1 && break; sleep 1; done

vault auth enable approle >/dev/null
vault secrets enable database >/dev/null
vault write database/config/swallow plugin_name=postgresql-database-plugin allowed_roles=worker \
  connection_url="postgresql://{{username}}:{{password}}@127.0.0.1:5432/swallow" \
  username=postgres password="$PGPW" >/dev/null || say "SWALLOW_VAULT error db-config"
vault write database/roles/worker db_name=swallow default_ttl=1h max_ttl=4h \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE swallow_worker;" \
  >/dev/null || say "SWALLOW_VAULT error db-role"
vault policy write worker - >/dev/null <<EOF
path "database/creds/worker" { capabilities = ["read"] }
EOF
vault write auth/approle/role/worker token_policies=worker token_ttl=15m token_num_uses=3 \
  secret_id_num_uses=1 secret_id_ttl=4h >/dev/null

# 동작 확인: 발급한 계정으로 실제 로그인되는지, 권한이 결과 테이블로 제한되는지
CRED=$(vault read -format=json database/creds/worker)
U=$(echo "$CRED" | jq -r .data.username); P=$(echo "$CRED" | jq -r .data.password)
if PGPASSWORD="$P" psql -h 127.0.0.1 -U "$U" -d swallow -qtAc "INSERT INTO settlement_result VALUES ('selftest', 1)" >/dev/null 2>&1 \
   && ! PGPASSWORD="$P" psql -h 127.0.0.1 -U "$U" -d swallow -qtAc "CREATE TABLE t(x int)" >/dev/null 2>&1; then
  say "SWALLOW_VAULT selftest ok"
else
  say "SWALLOW_VAULT selftest failed"
fi

say "SWALLOW_VAULT addr https://$IP:8200"
say "SWALLOW_VAULT role_id $(vault read -field=role_id auth/approle/role/worker/role-id)"
# 콘솔은 긴 줄을 자르므로 인증서를 64자씩 나눠 남기고, 받는 쪽이 해시로 확인한다
base64 -w64 /etc/vault.d/tls/cert.pem | while read -r part; do say "SWALLOW_VAULT ca_part $part"; done
say "SWALLOW_VAULT ca_sha256 $(base64 -w0 /etc/vault.d/tls/cert.pem | sha256sum | cut -d' ' -f1)"
for i in $(seq 1 __N_SECRET_IDS__); do
  say "SWALLOW_VAULT wrapped $(vault write -f -wrap-ttl=4h -field=wrapping_token auth/approle/role/worker/secret-id)"
done
unset VAULT_TOKEN INIT
mark vault_ready
