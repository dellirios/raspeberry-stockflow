# StockFlow Python Implementation Plan

> **For agentic workers:** Execute inline with superpowers:executing-plans, without remote operations.

**Goal:** Port the local 2.1.0 agent to Python for Raspberry Pi 1 B+; retain a portable core for a later Windows adapter.

**Architecture:** Standard-library Python daemon, native ZPL renderers, durable print/event journals, CUPS and libusb Linux adapter. Reuse the existing Python QR reader with installation-relative paths. Independent heartbeat/event delivery during physical waits.

**Tech Stack:** Python >=3.11, unittest, CUPS/ipptool, system libusb; optional evdev and MySQL connector for the reader only.

**Spec:** User's current local port request; source `../stockflow` version 2.1.0 and its approved layout/feedback documentation.

## Global Constraints

- Edit only this new local directory; do not use SSH or change production/original Node agent.
- Target ARMv6 32-bit, 512 MB RAM, Raspbian 13 / Python 3.13.5.
- Preserve ZPL byte-for-byte against synthetic Node fixtures, including QR payload and approved enlarged symbol.
- Never copy production credentials, journals, database dumps, archives or product data.
- Persist intent before physical submission; never replay an uncertain submitted job.
- Windows hardware integration and Raspberry physical tests are deferred explicitly.

## Review Focus

- Timeout/crash after submission must block automatic replay.
- Broken feedback and divergent/global counters must never confirm a physical print.
- Bearer tokens must never follow redirects or reach another host.
- Calibration may suspend unsent blocked work, never interrupt submitted work.
- Simulated tests must never connect to production or send physical prints.

## Tasks

- [x] Layout: write golden comparison tests, observe failure, port renderers/settings/date/weight, pass tests.
- [x] Feedback: write parser/recovery/physical state tests, observe failure, implement durable monitor and CUPS adapter.
- [x] Protocol: test URL restrictions, event acknowledgment, deduplication and calibration; implement daemon, independent heartbeat/outbox, health/preview CLI.
- [x] Reader/deployment: reuse reader with relative paths, optional requirements and systemd examples; document installation and unsupported Windows hardware.
- [x] Verify all tests and compilation locally, generate synthetic preview; review failure paths and document remaining hardware checks.

## Execution ledger

- Source inspected: app/router, printer service, renderers, geometry, feedback, outbox, heartbeat, calibration, reader and USB helper.
- Decision: proceed with the requested code now; no additional approval cycle. New empty project provides isolation.
- Decision: Linux transport has no fallback after an ambiguous send; safety against duplicate labels takes precedence over reproducing unsafe legacy fallback behavior.
- Decision: no new external dependencies for the print daemon; optional reader dependencies remain separate.
- Verified: 31 local unittest tests passed, including a complete daemon cycle using simulated printer/API and loopback HTTP only.
- Verified: all 13 production Python files parse with Python 3.11 grammar; local compilation succeeded on Python 3.15.2. Python 3.13.5/ARM execution is deferred.
- Verified: Zebra/Elgin synthetic ZPL previews generated; source Node Git working tree remains clean and Windows destination remains empty.
- Reviewed differences documented: no physical fallback after uncertain send, private loopback HTTP default, restricted fetch route, explicit quantity/payload limits, no automatic history purge or new PNG renderer.
- Final independent read-only review found no concrete important correctness, replay, concurrency or shutdown blocker. Hardware/runtime validation on the Raspberry remains pending, as requested.
