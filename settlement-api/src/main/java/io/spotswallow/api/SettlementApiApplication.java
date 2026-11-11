package io.spotswallow.api;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class SettlementApiApplication {
    public static void main(String[] args) {
        SpringApplication.run(SettlementApiApplication.class, args);
    }
}
