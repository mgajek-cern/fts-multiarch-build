# Rucio Transfer Orchestration

## Overview

High-level overview of how Rucio daemons coordinate transfer execution,
state reconciliation, and eventual consistency across distributed storage systems.

## Transfer Lifecycle

```mermaid
sequenceDiagram
    autonumber

    participant User
    participant RucioDB as Rucio DB
    participant Judge as Judge Evaluator
    participant Conveyor as Conveyor Submitter
    participant FTS
    participant Src as Source Storage
    participant Dst as Destination Storage
    participant Poller as Conveyor Poller
    participant Finisher as Conveyor Finisher

    User->>RucioDB: Create replication rule

    Note over RucioDB: Metadata is committed (state = NEW)

    Judge->>RucioDB: Poll for new rules
    Judge->>RucioDB: Create transfer request

    Conveyor->>RucioDB: Poll queued transfers
    Conveyor->>FTS: Submit transfer job

    FTS->>Src: Read file
    FTS->>Dst: Write file

    Note over FTS,Dst: Execution is external and time-decoupled

    Poller->>FTS: Poll transfer status
    FTS-->>Poller: FINISHED

    Poller->>RucioDB: Update transfer state

    Finisher->>RucioDB: Resolve replicas
    Finisher->>RucioDB: Mark file AVAILABLE

    Note over User,RucioDB: System converges over time (eventual consistency window)
```

## Eventual Consistency Model

Rucio is a **decoupled, multi-worker, state-driven workflow system**.

Independent daemons coordinate through shared database state and external systems (FTS, storage), producing **eventual consistency through staged progression**, rather than immediate end-to-end transaction completion.

Key property:

> Work is propagated via persistent state transitions, not synchronous execution chains.

## Operational Implications

- Temporary state divergence is expected (DB ≠ storage reality)
- Polling intervals directly affect convergence latency
- Failures are handled via retry + reprocessing loops
- Recovery is daemon-driven, not request-driven
- Concurrent execution introduces non-deterministic timing but deterministic final state (in healthy conditions)
