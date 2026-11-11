package io.spotswallow.worker;

import java.util.concurrent.ThreadLocalRandom;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 장애 주입. 지정한 지점에서 확률적으로 프로세스를 즉시 종료한다 (kill -9 와 같은 효과, 종료 훅 없음).
 * 스팟 인스턴스가 경고 없이 사라지는 상황을 재현해 무결성 구조를 검증하는 데 쓴다.
 */
@Component
public class ChaosPolicy {
    private static final Logger log = LoggerFactory.getLogger(ChaosPolicy.class);

    public enum Point { BEFORE_COMMIT, AFTER_COMMIT_BEFORE_ACK }

    private final Point point;
    private final double probability;

    public ChaosPolicy(@Value("${swallow.chaos.crash-point:NONE}") String point,
                       @Value("${swallow.chaos.crash-probability:0}") double probability) {
        this.point = "NONE".equalsIgnoreCase(point) ? null : Point.valueOf(point.toUpperCase());
        this.probability = probability;
    }

    public void maybeCrash(Point at, String jobId) {
        if (at == point && probability > 0 && ThreadLocalRandom.current().nextDouble() < probability) {
            log.error("CHAOS: halting worker at {} while processing {}", at, jobId);
            Runtime.getRuntime().halt(137);
        }
    }
}
