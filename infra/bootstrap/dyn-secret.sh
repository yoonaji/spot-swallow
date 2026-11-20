# 스팟 워커 보안 부트스트랩: Vault 에서 수명 짧은 DB 계정을 받아 워커 환경 파일로 넘긴다.
# ec2_tready.py 가 JDK 설치 후·앱 기동 전에 끼워 넣고, 앞뒤 마크로 bootstrap_s 를 잰다.
# __VAULT_ADDR__, __VAULT_CA_B64__, __ROLE_ID__, __WRAPPED__ 는 실행마다 security_overhead.py 가 채운다.
#
#   1. 래핑 토큰을 풀어 1회용 secret_id 를 얻는다 (이미 누가 풀었다면 실패 → 워커를 띄우지 않는다)
#   2. AppRole 로그인 → 15분짜리 토큰 (database/creds/worker 읽기만 가능)
#   3. DB 계정 발급 → /run/swallow/worker.env (0600). 워커는 이 파일로 DB_USER/DB_PASSWORD 를 읽는다
# 이미지에는 비밀이 하나도 들어 있지 않다. 인바운드 포트도 열지 않는다(기본 보안 그룹, 외부 접근 없음).
VA="__VAULT_ADDR__"
install -d -m 700 /run/swallow
echo "__VAULT_CA_B64__" | base64 -d > /run/swallow/vault-ca.pem
VC="curl -sSf --max-time 10 --cacert /run/swallow/vault-ca.pem"
exec 2>/dev/console   # 실패 원인을 콘솔에 남긴다
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
SID=$($VC -X POST -H "X-Vault-Token: __WRAPPED__" "$VA/v1/sys/wrapping/unwrap" | json 'd["data"]["secret_id"]')
TOK=$($VC -X POST -d "{\"role_id\":\"__ROLE_ID__\",\"secret_id\":\"$SID\"}" "$VA/v1/auth/approle/login" | json 'd["auth"]["client_token"]')
umask 077
$VC -H "X-Vault-Token: $TOK" "$VA/v1/database/creds/worker" \
  | json '"DB_USER=" + d["data"]["username"] + "\nDB_PASSWORD=" + d["data"]["password"]' > /run/swallow/worker.env
unset SID TOK
grep -q '^DB_PASSWORD=.' /run/swallow/worker.env && mark secret_ok
