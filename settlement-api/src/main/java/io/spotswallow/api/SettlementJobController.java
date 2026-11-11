package io.spotswallow.api;

import java.time.LocalDate;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/settlement-jobs")
public class SettlementJobController {
    private final SettlementJobService service;

    public SettlementJobController(SettlementJobService service) {
        this.service = service;
    }

    public record SettlementRequest(String merchantId, LocalDate settlementDate) {}

    /** 202: 새로 접수됨. 200: 이미 접수된 요청 (멱등). */
    @PostMapping
    public ResponseEntity<Map<String, Object>> accept(@RequestBody SettlementRequest request) {
        if (request.merchantId() == null || request.merchantId().isBlank() || request.settlementDate() == null) {
            return ResponseEntity.badRequest().body(Map.of("error", "merchantId and settlementDate are required"));
        }
        var result = service.accept(request.merchantId(), request.settlementDate());
        HttpStatus status = result.created() ? HttpStatus.ACCEPTED : HttpStatus.OK;
        return ResponseEntity.status(status).body(Map.of("jobId", result.jobId(), "created", result.created()));
    }

    @GetMapping("/{jobId}")
    public ResponseEntity<Map<String, Object>> get(@PathVariable String jobId) {
        return service.find(jobId).map(ResponseEntity::ok).orElse(ResponseEntity.notFound().build());
    }
}
