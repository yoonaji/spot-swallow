package io.spotswallow.api;

import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.Declarables;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 큐 구성. API 와 워커가 같은 구성을 선언한다 (선언은 멱등).
 * - settlement.jobs: durable 큐. 워커가 Ack 하기 전까지 메시지를 보관한다.
 * - settlement.jobs.dlq: 처리할 수 없는 메시지(독성 메시지)를 격리하는 큐.
 */
@Configuration
public class RabbitTopology {
    public static final String EXCHANGE = "settlement";
    public static final String JOBS_QUEUE = "settlement.jobs";
    public static final String JOBS_ROUTING_KEY = "settlement.jobs";
    public static final String DLX = "settlement.dlx";
    public static final String DLQ = "settlement.jobs.dlq";

    @Bean
    public Declarables settlementTopology() {
        DirectExchange exchange = new DirectExchange(EXCHANGE, true, false);
        DirectExchange dlx = new DirectExchange(DLX, true, false);
        Queue jobs = QueueBuilder.durable(JOBS_QUEUE)
                .deadLetterExchange(DLX)
                .deadLetterRoutingKey(DLQ)
                .build();
        Queue dlq = QueueBuilder.durable(DLQ).build();
        Binding jobsBinding = BindingBuilder.bind(jobs).to(exchange).with(JOBS_ROUTING_KEY);
        Binding dlqBinding = BindingBuilder.bind(dlq).to(dlx).with(DLQ);
        return new Declarables(exchange, dlx, jobs, dlq, jobsBinding, dlqBinding);
    }
}
