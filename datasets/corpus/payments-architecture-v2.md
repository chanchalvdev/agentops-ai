# Payments Platform Architecture v2

Status: Approved. Owner: Payments Core team. Approved: 2026-02-02.

## Overview

v2 splits paycore into four services: authorization-service, capture-service, refund-service, and
ledger-service. Services communicate through Kafka topics with at-least-once delivery; every
consumer is idempotent and deduplicates on the payment_id plus event sequence number. Each service
owns its own PostgreSQL 16 database. The ledger is the system of record.

## Performance Requirements

- Card authorization p99 latency must stay below 200 ms at the API edge.
- Peak sustained throughput target is 3,000 transactions per second (TPS).
- Settlement becomes continuous (micro-batches every 5 minutes) instead of a nightly batch.

## Availability and Recovery

- Availability target: 99.95% monthly for the authorization API.
- Recovery Point Objective (RPO): 5 minutes, using continuous WAL streaming to a standby region.
- Recovery Time Objective (RTO): 15 minutes with automated failover.

## Data Retention

Transaction records are retained for 5 years in the primary store, then moved to cold storage.
Tokenization through the external vault is unchanged.

## Migration Plan

The migration is planned in three phases over 9 months. Phase 1 (months 1-3) extracts the refund
service. Phase 2 (months 4-6) extracts capture and introduces Kafka. Phase 3 (months 7-9)
extracts authorization and the ledger. Estimated engineering effort is 14 engineers for 9 months.
The dual-write period for the ledger is limited to 30 days with daily reconciliation reports.

## Open Risks

The 5-year retention conflicts with the 7-year audit requirement recorded in v1 and must be
confirmed with the compliance team before Phase 3. Kafka adds operational load; the platform team
has not yet committed on-call coverage for the new clusters.
