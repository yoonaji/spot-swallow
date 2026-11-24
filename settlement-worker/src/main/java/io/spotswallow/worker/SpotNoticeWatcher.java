package io.spotswallow.worker;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.listener.AbstractMessageListenerContainer;
import org.springframework.amqp.rabbit.listener.RabbitListenerEndpointRegistry;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 스팟 회수 경고(2분 전 중단 알림)를 받으면 새 작업은 받지 않고, 처리 중인 작업만 끝낸다 (drain).
 *
 * 경고 없이 죽으면 처리 중이던 작업은 Ack 가 없어 다른 워커가 처음부터 다시 계산한다(정합성은 그대로).
 * 경고를 받으면 소비를 멈추고 처리 중인 1건(prefetch=1)을 커밋·Ack 한 뒤 쉬므로, 다시 계산하는 작업이 없다.
 * 경고가 오지 않거나 늦는 경우도 있으므로 이 기능은 최적화이고, 정합성은 여전히 수동 Ack 와 멱등성이 지킨다.
 *
 * 경고를 읽는 곳 (swallow.notice.source):
 *   none  끔 (기본값)
 *   file  파일이 생기면 경고로 본다. 로컬 실험에서 주입기가 docker exec 로 만든다.
 *   imds  EC2 인스턴스 메타데이터(IMDSv2) spot/instance-action. 404 면 경고 없음. (EC2 에서는 아직 시험 안 함)
 */
@Component
public class SpotNoticeWatcher {
    private static final Logger log = LoggerFactory.getLogger(SpotNoticeWatcher.class);
    private static final String IMDS = "http://169.254.169.254/latest";

    private final RabbitListenerEndpointRegistry registry;
    private final String source;
    private final Path file;
    private final long drainTimeoutMs;
    private final AtomicBoolean draining = new AtomicBoolean(false);
    private final Counter notices;
    private final HttpClient http;

    public SpotNoticeWatcher(RabbitListenerEndpointRegistry registry, MeterRegistry meters,
                             @Value("${swallow.notice.source:none}") String source,
                             @Value("${swallow.notice.file:/tmp/spot-notice}") String file,
                             @Value("${swallow.notice.drain-timeout-ms:110000}") long drainTimeoutMs) {
        this.registry = registry;
        this.source = source.toLowerCase();
        this.file = Path.of(file);
        this.drainTimeoutMs = drainTimeoutMs;
        this.notices = Counter.builder("swallow.worker.spot_notices").register(meters);
        Gauge.builder("swallow.worker.draining", draining, d -> d.get() ? 1 : 0).register(meters);
        this.http = "imds".equals(this.source)
                ? HttpClient.newBuilder().connectTimeout(Duration.ofMillis(500)).build() : null;
        // 경고 파일은 지우지 않는다. 부팅 중(Java 시작 전)에 경고가 오면 시작하자마자 마무리 상태로 들어가야 한다.
        // 실제 스팟 인스턴스는 회수된 뒤 다시 시작되지 않으므로 지난번 파일이 남는 일은 없다.
    }

    @Scheduled(fixedDelayString = "${swallow.notice.poll-ms:1000}")
    public void poll() {
        if (draining.get() || "none".equals(source)) {
            return;
        }
        if (noticePresent()) {
            drain();
        }
    }

    boolean noticePresent() {
        return switch (source) {
            case "file" -> Files.exists(file);
            case "imds" -> imdsNotice();
            default -> false;
        };
    }

    /** 소비를 멈추고 처리 중인 작업이 끝날 때까지 기다린다. 끝나면 쉬면서 실제 회수를 기다린다. */
    void drain() {
        if (!draining.compareAndSet(false, true)) {
            return;
        }
        notices.increment();
        long t0 = System.nanoTime();
        log.warn("SPOT NOTICE: stop taking new jobs, finishing in-flight work (timeout {} ms)", drainTimeoutMs);
        for (var c : registry.getListenerContainers()) {
            if (c instanceof AbstractMessageListenerContainer a) {
                a.setShutdownTimeout(drainTimeoutMs);
            }
            c.stop();   // 소비 취소 → 처리 중인 메시지가 커밋·Ack 될 때까지 기다린다
        }
        log.warn("SPOT DRAINED in {} ms, idle until reclaimed", (System.nanoTime() - t0) / 1_000_000);
    }

    boolean isDraining() {
        return draining.get();
    }

    private boolean imdsNotice() {
        try {
            String token = http.send(HttpRequest.newBuilder(URI.create(IMDS + "/api/token"))
                    .timeout(Duration.ofMillis(500))
                    .header("X-aws-ec2-metadata-token-ttl-seconds", "60")
                    .PUT(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString()).body();
            HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(IMDS + "/meta-data/spot/instance-action"))
                    .timeout(Duration.ofMillis(500))
                    .header("X-aws-ec2-metadata-token", token).GET().build(), HttpResponse.BodyHandlers.ofString());
            return r.statusCode() == 200;
        } catch (IOException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }
}
