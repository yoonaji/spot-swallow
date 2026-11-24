package io.spotswallow.worker;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.amqp.rabbit.listener.RabbitListenerEndpointRegistry;

class SpotNoticeWatcherTest {

    @TempDir
    Path dir;

    private SpotNoticeWatcher watcher(String source, Path file) {
        return new SpotNoticeWatcher(new RabbitListenerEndpointRegistry(), new SimpleMeterRegistry(),
                source, file.toString(), 1000);
    }

    @Test
    void noticeArrivingDuringBootIsHonoured() throws Exception {
        Path f = dir.resolve("spot-notice");
        Files.createFile(f);   // Java 가 뜨기 전에 경고가 왔다
        SpotNoticeWatcher w = watcher("file", f);
        w.poll();
        assertTrue(w.isDraining());
    }

    @Test
    void noticeFileStartsDrainOnce() throws Exception {
        Path f = dir.resolve("spot-notice");
        SpotNoticeWatcher w = watcher("file", f);
        w.poll();
        assertFalse(w.isDraining());
        Files.createFile(f);
        w.poll();
        assertTrue(w.isDraining());
        w.poll();   // 두 번째 확인은 아무것도 하지 않는다
        assertTrue(w.isDraining());
    }

    @Test
    void disabledSourceNeverDrains() throws Exception {
        Path f = dir.resolve("spot-notice");
        SpotNoticeWatcher w = watcher("none", f);
        Files.createFile(f);
        w.poll();
        assertFalse(w.isDraining());
    }
}
