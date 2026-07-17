# Provenance and Design Lineage

This repository is a new Python implementation authored for the same project owner. Its design lineage is explicitly acknowledged.

The conceptual starting point is the analysis and redesign work in:

- `independent-user-memory/docs/design-thinking-and-redesign.md`
- `independent-user-memory/docs/evolvable-memory-architecture.md`

Those documents describe evidence-grounded memory, immutable revisions, bitemporal reasoning, recall traces, outcome attribution, contextual utility, and guarded policy evolution. This repository turns those ideas into a Python-native architecture.

Implementation rules:

1. No Java source tree, class hierarchy, persistence entity, controller, or mapper is mechanically translated.
2. Python domain concepts are modeled from behavior and invariants, not one-to-one class correspondence.
3. Design concepts and their lineage remain documented instead of being represented as unrelated clean-room work.
4. Third-party code must be attributed and licensed independently before inclusion.

The repository uses AGPL-3.0-only, consistent with the originating project. This file documents provenance; it is not a substitute for the license terms.

Test-only third-party dependency:

- `axe-core` 4.11.4, maintained by Deque Systems and distributed under MPL-2.0, is exact-pinned through `package-lock.json` for local Chromium accessibility audits. It is installed into ignored `node_modules/`, is not copied into the application image or Python distribution, and does not become a runtime frontend dependency.
