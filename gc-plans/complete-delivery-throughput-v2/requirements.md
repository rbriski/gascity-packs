---
plan_slug: complete-delivery-throughput-v2
phase: requirements
rig: gascity-packs
rig_root: /home/nvidia/gascity/city/rigs/gascity-packs
artifact_root: /home/nvidia/gascity/city/rigs/gascity-packs/gc-plans
status: approved
created_at: 2026-08-03T14:28:19Z
updated_at: 2026-08-03T14:28:19Z
---

# Requirements: Complete Delivery Throughput v2

## Problem Statement

The first Complete Delivery pack train spent more than 70 hours without
finishing. S5 and S6 alone consumed 61 hours 12 minutes. The dominant delays
were unbounded exact-head re-review, provider waiting, overlapping repairs,
and unaccounted coordination time rather than initial implementation or
protected merge time.

The delivery workflow currently makes CodeRabbit a required completion
provider, permits twelve external-review attempts, and inherits a ten-iteration
implementation/review loop. Every repair changes the candidate SHA and can
restart the entire external-review sequence. This is incompatible with fast,
predictable feature delivery.

## Solution

Make Complete Delivery own a bounded, provider-independent path from one
frozen candidate through internal review, deterministic gates, protected merge,
deployment, and verification. Use Gas City's existing Formula, molecule, bead,
hook, Witness, convoy, and refinery primitives as the durable control plane.
Reports remain observational and never become workflow authority.

## User Stories

### As a feature owner, I want delivery to continue across restarts

- One durable workflow root owns the run until verified completion or an
  explicit terminal blocker.
- A Mayor or worker restart recovers work from the hook and native Formula
  state rather than chat memory.
- A convoy reports aggregate progress but is never treated as the executor.

### As a maintainer, I want review to converge predictably

- Internal agent review is the default review authority.
- CodeRabbit is off by default and cannot block the critical path.
- Review evidence is tied to one canonical full candidate SHA.
- At most two review/repair iterations are permitted.
- A third iteration fails closed and requires architectural escalation.

### As an operator, I want stalls and rework to be visible

- Every Formula attempt remains represented by native bead state, timestamps,
  attempt number, outcome, and dependencies.
- Every external command has a finite timeout.
- Provider unavailability cannot create an indefinite wait.
- The wall-clock report can classify at least 95 percent of elapsed time.

### As a rig owner, I want the same behavior everywhere

- The policy ships in the versioned Complete Delivery pack, not copied rig by
  rig.
- A new rig receives the safe defaults when it installs the released pack.
- Rig-specific overrides may strengthen gates but must explicitly opt into any
  external provider.

## Out Of Scope

- Building a new scheduler, watchdog, convoy implementation, or review service.
- Broad city-wide model-routing rollout before a canary succeeds.
- Rewriting the existing lifecycle implementation into another language during
  this remediation.
- Creating another standalone HTML report.

## Other Notes

- The approved target is 16 wall-clock hours for the first high-assurance pack,
  with a 24-hour architectural stop.
- Standard future features target 6-10 hours deployed; high-risk features
  target 12-18 hours.
- The existing Complete Delivery, S5/S6 wall-clock, and model-routing reports
  are the evidence baseline and should receive after-action updates only after
  implementation is verified.
