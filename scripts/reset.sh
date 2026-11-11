#!/usr/bin/env bash
# 실험 사이에 상태를 비운다: 컨테이너와 DB·큐 데이터를 모두 지우고 다시 띄운다.
# 사용: scripts/reset.sh [워커 수]   (환경 변수 CHAOS_CRASH_POINT 등은 그대로 compose 에 전달된다)
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down -v --remove-orphans
docker compose up -d --build --scale settlement-worker="${1:-3}"
