# Payments Platform Architecture v1

Status: Superseded by v2. Owner: Payments Core team. Last reviewed: 2025-03-14.

## Overview

The payments platform is a single Java 17 monolith ("paycore") deployed to three availability
zones behind an application load balancer. All payment state lives in one PostgreSQL 14 primary
with two synchronous read replicas. Card authorization, capture, refunds, and ledger posting run
inside the same process and share one database transaction per request.

## Performance Requirements

- Card authorization p99 latency must stay below 300 ms at the API edge.
- Peak sustained throughput target is 1,200 transactions per second (TPS).
- Batch settlement runs nightly at 02:00 UTC and must finish within 90 minutes.

## Availability and Recovery

- Availability target: 99.9% monthly for the authorization API.
- Recovery Point Objective (RPO): 15 minutes, achieved with WAL archiving every 5 minutes and
  hourly base backups.
- Recovery Time Objective (RTO): 60 minutes using manual promotion of a replica.

## Data Retention

Transaction records are retained for 7 years to satisfy audit requirements. Card numbers are
tokenized by the external vault; paycore never stores a primary account number (PAN).

## Known Limitations

Deployments require a full restart of the monolith, causing roughly 40 seconds of degraded
capacity per zone. Schema migrations lock the ledger table and are only allowed in the Sunday
maintenance window. A single slow refund query has twice caused authorization timeouts.
