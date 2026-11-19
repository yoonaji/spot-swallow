FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY target/app.jar app.jar
# BOOT_DELAY_S: 로컬 실험에서 클라우드 인스턴스 확보·부팅 시간을 흉내 낸다 (제어기가 분포에서 뽑아 넣는다).
# 0 이면 바로 시작한다. exec 로 java 를 PID 1 로 만들어 SIGTERM 을 그대로 받게 한다 (graceful 종료).
ENTRYPOINT ["sh", "-c", "sleep ${BOOT_DELAY_S:-0}; exec java -XX:+UseSerialGC -Xss512k -jar app.jar"]
