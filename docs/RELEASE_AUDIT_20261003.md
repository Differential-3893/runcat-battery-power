# Consolidated release audit — 2026-10-03

This delivery includes the previously unapplied runtime-preservation patch and
the remaining documentation/CI corrections. It supersedes the separate
`RunCat_runtime_preservation_20261003` delivery; do not layer that older package
on top of this one.

## Scope

The producer itself is byte-identical to the installed-source baseline. Card
rows, telemetry/calculations, account/credit display semantics and polling cadence
are unchanged. Installation settings follow explicit override → recorded value →
fresh default through real shell entrypoints. Added manual helpers use the saved
runtime even with custom paths; they are one-shot commands, not services.

The README, runtime contract and maintenance/manual examples are aligned. CI uses
`actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` (official `v7.0.1`,
verified on 2026-10-03), `contents: read`, `persist-credentials: false`, and a
10-minute job timeout. Both suites run on Linux and macOS. The Codex workflow
moves from v4 to that verified v7 release; battery already used the same v7 SHA.
Hosted runner software/OS images are still maintained by GitHub, not frozen by
this action pin. No automatic dependency-update bot is installed.

## Evidence and limits

The source and delivery suites were exercised in the delivery's Linux/Python
3.13.5 environment; per-run logs/counts are in the delivery verification folder.
Tests include real shell entrypoints, pipes, locks and temporary filesystem work,
with synthetic account/battery data and fake or mocked native services. The new
contract tests check manual-runtime selection, current documentation commands,
local documentation targets and CI pin/permission rules. Neither test counts nor
SHA pinning prove absence of all defects.

No native Mac login, battery telemetry or GUI service was exercised here. The
combined apply command performs live verification on the user's Mac; UI rendering,
a real Codex turn and a full polling cycle still need observation there. Future
upstream API/OS changes are not covered by these finite tests.

## Completion criterion

The listed failures must have regression coverage, all offline checks must pass,
and native apply must report success for each project. Publication is separate:
two independent commits, no force push, preservation of unrelated remote work,
and no runtime account/sensor files in uploads. CI completion after publication
must be checked independently; a successful push is not a claim of passing CI.

A new issue should name the current revision, triggering input and user/security
impact. Optional refactoring is not a failed release, but concrete new evidence
must still be evaluated rather than suppressed.
