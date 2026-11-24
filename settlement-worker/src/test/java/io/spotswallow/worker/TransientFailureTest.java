package io.spotswallow.worker;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.sql.SQLException;

import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.transaction.CannotCreateTransactionException;
import org.springframework.transaction.TransactionSystemException;

class TransientFailureTest {

    @Test
    void dbRestartFailuresAreRetried() {
        // postgres-restart 행에서 실제로 DLQ 로 잘못 보낸 예외
        assertTrue(SettlementWorker.isTransientDbFailure(new TransactionSystemException("JDBC rollback failed")));
        assertTrue(SettlementWorker.isTransientDbFailure(new CannotCreateTransactionException("no connection")));
        assertTrue(SettlementWorker.isTransientDbFailure(new QueryTimeoutException("timeout")));
        assertTrue(SettlementWorker.isTransientDbFailure(
                new RuntimeException("wrapped", new SQLException("terminating connection", "57P01"))));
        assertTrue(SettlementWorker.isTransientDbFailure(
                new IllegalStateException(new SQLException("connection refused", "08001"))));
    }

    @Test
    void realPoisonIsNotRetried() {
        assertFalse(SettlementWorker.isTransientDbFailure(new IllegalArgumentException("job not found: X")));
        assertFalse(SettlementWorker.isTransientDbFailure(new DataIntegrityViolationException("check violated")));
        assertFalse(SettlementWorker.isTransientDbFailure(
                new RuntimeException(new SQLException("syntax error", "42601"))));
    }
}
