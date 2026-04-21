# Documentation Index

Development notes, design documents, and implementation details for jaato-client-telegram.

## Design

| Document | Description |
|----------|-------------|
| [design-prompt.md](design/design-prompt.md) | Original design prompt that guided the client's architecture and feature set |
| [event-flow-diagram.md](design/event-flow-diagram.md) | Visual diagram of how events flow through the system — from Telegram to jaato server and back |
| [rendering-pipeline.md](design/rendering-pipeline.md) | How agent responses are rendered into Telegram messages, including markdown, code blocks, and expandable content |

## Features

| Document | Description |
|----------|-------------|
| [abuse-protection.md](features/abuse-protection.md) | Rate limiting and abuse protection system to prevent misuse |
| [access-request-workflow.md](features/access-request-workflow.md) | How users request access and the approval/denial flow |
| [expandable-content.md](features/expandable-content.md) | Collapsible/expandable sections for long tool outputs in Telegram |
| [file-sharing.md](features/file-sharing.md) | File upload/download between users and the agent via Telegram |
| [file-watching.md](features/file-watching.md) | Watching workspace files for changes and notifying users |
| [file-watching-sharing-summary.md](features/file-watching-sharing-summary.md) | Combined summary of file watching and sharing implementation |
| [permission-approval-ui.md](features/permission-approval-ui) | Inline keyboard UI for approving or denying agent permission requests |
| [presentation-context.md](features/presentation-context.md) | How presentation context is integrated into agent responses |
| [workspace-isolation.md](features/workspace-isolation.md) | Per-user workspace isolation to keep sessions separate |

## Bug Fixes

| Document | Description |
|----------|-------------|
| [connection-recovery.md](fixes/connection-recovery.md) | Fix for recovering dropped IPC connections to the jaato server |
| [file-mention-detection.md](fixes/file-mention-detection.md) | Fix for detecting file mentions in agent responses |
| [ipc-recovery.md](fixes/ipc-recovery.md) | Fix for IPC transport reconnection after failures |

## Implementation Notes

| Document | Description |
|----------|-------------|
| [telemetry.md](implementation/telemetry.md) | Telemetry system — metrics collection, session tracking, and reporting |
| [peer-review-migration.md](implementation/peer-review-migration.md) | Peer review findings and migration assessment for the codebase |
| [implementation-review.md](implementation/implementation-review.md) | Review of file sharing and file watching implementations |
| [phase1-mvp-summary.md](implementation/phase1-mvp-summary.md) | Summary of Phase 1 MVP — what was built and how |
| [presentation-content-summary.md](implementation/presentation-content-summary.md) | Summary of presentation context and expandable content implementation |
| [sdk-author-summary.md](implementation/sdk-author-summary.md) | Rendering pipeline details written for SDK authors consuming this client |

## Project

| Document | Description |
|----------|-------------|
| [improvements.md](project/improvements.md) | User experience improvements planned or implemented |

---

*Project-level docs kept at repo root: [README.md](../README.md), [CONTRIBUTING.md](../CONTRIBUTING.md), [CHANGELOG.md](../CHANGELOG.md), [ROADMAP.md](../ROADMAP.md), [TESTING.md](../TESTING.md), [WHITELIST.md](../WHITELIST.md)*
