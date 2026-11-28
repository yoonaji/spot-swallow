package io.spotswallow.api;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 감사 체인의 끝 해시를 DB 밖에 남긴다 (앵커).
 *
 * 체인 안의 검사(brokenLinks)는 DB 최고 관리자가 체인 전체를 처음부터 다시 계산해 덮으면 통과한다.
 * 그래서 주기적으로 (seq, hash) 를 DB 가 아닌 곳에 추가만 하는 파일로 적어 두고, 대사 때 그 해시가 아직 그대로인지 본다.
 * 과거의 어느 줄을 고치면 그 뒤 모든 해시가 바뀌므로, 고친 줄 뒤에 앵커가 하나라도 있으면 드러난다.
 *
 * 로컬에서는 별도 Docker 볼륨의 파일이다. 운영에서는 DB 관리자가 손댈 수 없는 저장소
 * (예: S3 Object Lock 처럼 한 번 쓰면 지울 수 없는 저장소)에 두어야 의미가 있다.
 * 한계: 마지막 앵커 이후의 변조는 다음 앵커가 변조된 해시를 적으므로 잡지 못한다. 앵커 주기가 그 구간이다.
 */
@Component
public class AuditAnchor {
    private static final Logger log = LoggerFactory.getLogger(AuditAnchor.class);
    private static final Pattern LINE = Pattern.compile("\"seq\":(\\d+),\"hash\":\"([0-9a-f]{64})\"");

    private final JdbcTemplate jdbc;
    private final Path file;

    public AuditAnchor(JdbcTemplate jdbc, @Value("${swallow.audit.anchor-file}") String file) {
        this.jdbc = jdbc;
        this.file = Path.of(file);
    }

    record Anchor(long seq, String hash) {}

    /** 체인 끝이 지난 앵커보다 앞으로 갔으면 한 줄 추가한다. 추가한 앵커를 돌려준다 (없으면 null). */
    @Scheduled(fixedDelayString = "${swallow.audit.anchor-ms}", initialDelayString = "${swallow.audit.anchor-ms}")
    public synchronized Anchor anchorNow() {
        try {
            List<Anchor> head = jdbc.query("SELECT seq, hash FROM result_audit ORDER BY seq DESC LIMIT 1",
                    (rs, i) -> new Anchor(rs.getLong(1), rs.getString(2)));
            if (head.isEmpty()) {
                return null;
            }
            Anchor a = head.get(0);
            List<Anchor> existing = read();
            if (!existing.isEmpty() && existing.get(existing.size() - 1).seq() >= a.seq()) {
                return null;
            }
            Files.createDirectories(file.toAbsolutePath().getParent());
            String line = "{\"seq\":" + a.seq() + ",\"hash\":\"" + a.hash() + "\",\"at\":\"" + Instant.now() + "\"}\n";
            Files.writeString(file, line, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
            return a;
        } catch (Exception e) {
            log.warn("audit anchor failed: {}", e.toString());
            return null;
        }
    }

    /** 앵커마다 DB 의 같은 seq 해시가 그대로인지 본다. */
    public Map<String, Object> verify() {
        Map<String, Object> out = new LinkedHashMap<>();
        List<Anchor> anchors;
        try {
            anchors = read();
        } catch (IOException e) {
            out.put("anchors", 0);
            out.put("anchorMismatch", 0);
            out.put("error", e.toString());
            return out;
        }
        long mismatch = 0;
        for (Anchor a : anchors) {
            List<String> h = jdbc.queryForList("SELECT hash FROM result_audit WHERE seq = ?", String.class, a.seq());
            if (h.isEmpty() || !h.get(0).trim().equals(a.hash())) {
                mismatch++;
            }
        }
        out.put("anchors", anchors.size());
        out.put("lastAnchoredSeq", anchors.isEmpty() ? null : anchors.get(anchors.size() - 1).seq());
        out.put("anchorMismatch", mismatch);
        return out;
    }

    private List<Anchor> read() throws IOException {
        List<Anchor> list = new ArrayList<>();
        if (!Files.exists(file)) {
            return list;
        }
        for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
            Matcher m = LINE.matcher(line);
            if (m.find()) {
                list.add(new Anchor(Long.parseLong(m.group(1)), m.group(2)));
            }
        }
        return list;
    }
}
