FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY target/app.jar app.jar
ENTRYPOINT ["java", "-XX:+UseSerialGC", "-Xss512k", "-jar", "app.jar"]
