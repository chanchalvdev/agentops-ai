# ADR-003: Use pgvector for Retrieval

Status: Accepted. Date: 2026-01-20. Deciders: Platform Architecture group.

## Context

The knowledge assistant needs semantic search over roughly 2 million chunks across 40 tenants.
Tenant isolation is mandatory, and the team already operates PostgreSQL.

## Options Considered

1. pgvector inside the existing PostgreSQL cluster.
2. A managed vector database (Pinecone).
3. Self-hosted OpenSearch with k-NN.

## Decision

We chose pgvector with HNSW indexes. Vectors live next to document metadata, so tenant filters,
joins, and transactions use ordinary SQL, and there is no second system to keep consistent.
Full-text search uses PostgreSQL tsvector, which enables hybrid retrieval in a single query plan.

## Consequences

Positive: one backup and access-control model, transactional ingestion, no extra vendor.
Negative: HNSW index builds are memory intensive; at more than 50 million vectors we expect to
revisit this decision. Filtered vector search needs iterative index scans (pgvector 0.8 or later)
to avoid returning too few results for small tenants.
