# Runbook: Payments Database Failover

Applies to: ledger-service PostgreSQL cluster. Severity: SEV-1 when authorization is impacted.

## Detection

The alert `ledger_primary_unreachable` fires when the primary fails health checks for 30 seconds.
Replication lag above 10 seconds triggers `ledger_replication_lag_high` as a warning.

## Procedure

1. Page the on-call database engineer and open an incident channel named inc-payments-db.
2. Confirm the primary is actually down from two separate hosts before promoting anything.
3. Freeze deployments for all payments services.
4. Promote the standby with `patronictl failover --candidate ledger-standby-1`.
5. Verify writes succeed using the synthetic payment check `pay-canary`.
6. Update the incident channel every 15 minutes until resolved.

## Escalation

If failover does not complete within 10 minutes, escalate to the Payments Core engineering
manager. If customer-facing authorization errors exceed 2% for more than 5 minutes, notify the
incident commander and the communications lead.

## After the Incident

Write a postmortem within 5 business days. Re-enable deployments only after replication to a new
standby is healthy.
