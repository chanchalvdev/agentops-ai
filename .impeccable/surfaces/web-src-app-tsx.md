---
version: 1
slug: "web-src-app-tsx"
primary_target: "web/src/App.tsx"
related_targets: ["web/src"]
---

# Surface brief: AgentOps AI web console (web/)

Scope: the whole web UI (Chat, Documents, Workspace, Approvals, Executions, Evaluations & Monitoring). Visitor mode: **Operate**.

Audience and job: platform engineers operating the agent (ask, approve, inspect traces, watch evals and drift), and the owner demoing it on a shared screen. Every screen must make evidence legible: source, trace, measurement.

Constraints: pure client of the public API; React 18 + TS strict + Vite 5 + Tailwind 3; keep every route, API call and test working. Desktop first; must not break at narrower widths.

Memorable moment: watching a run's custody chain sign itself, step by step, and countersigning an approval.

## Direction contract

THESIS: Every answer is evidence. Runs are case-numbered records whose custody chain (step, signer, time) is signed at each handoff and sealed at the end; approvals are a human countersignature. Refuses the category default: a neutral chat thread with a side trace panel and indigo accents.

OWN-WORLD: Cool bond-white ground (#F2F3EF), carbon ink (#172033), a manila nav rail (#D9B46A) as the committed shell colour, stamp-violet (#5B3FA0) for seals, signatures and focus, seal-red (#B4232F) only for critical/refused, record-green (#2F7D4F) for verified. Public Sans for all UI type, JetBrains Mono (tabular) for IDs and figures. 1.5px ink rules instead of boxes and shadows; square corners; stamps for sealed states.

STORY: The visitor sees what the agent did, in order, by whom, how long it took, and what it cites, then trusts or rejects it with one action.

FIRST VIEWPORT: Left manila rail: wordmark, six nav items with counts (approval badge in violet), connection key at the foot. Main: case header (case ID chip + question + mode/time), the custody chain as one horizontal strip of signed handoffs ending in a violet seal, then a two-column work area: answer with inline exhibit chips and figures row on the left; numbered exhibits on the right, focused one at full ink with a violet offset, others dimmed. The ask box sits at the foot of the work area.

FORM: Custody Record (forensic chain-of-custody tags), position 5 on the grounded list; seed key 4d90a70a. Raises: type carries hierarchy (Metro); focused exhibit full ink, rest dimmed (Streaming wall); sequential IDs as navigation (Saville); tabular figures at full density (Ikeda). Signature move: the custody chain, reused as the Executions timeline and the approval countersignature.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
