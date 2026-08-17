# Where every instruction came from

Provenance for the three sub agent prompts. This file exists because "is that rule yours or
ours?" has been asked several times, and the answer was not written down anywhere.

Four sources feed these prompts:

| Tag | Source | Status |
|---|---|---|
| **P** | `../agents/agent2_…txt`, `agent3_…txt`, `agent559_…txt` — production, workflows 161/163 | in production |
| **JD** | `../agents/testcase_jd.txt`, `reviwer_jd.txt` — console copies added 2026-08-17 | pre column fix |
| **186** | `../aava_endpoint_outputs/workflow_186_…/agent_614…`, `agent_560…` | **APPROVED**, different use case |
| **O** | written for the orchestrator, no upstream source | — |

---

## Agent 01 — scenario generator

| Rule | Source |
|---|---|
| ROLE, INPUT, INSTRUCTIONS 1–9, STRICT RULES, OUTPUT FORMAT, SAMPLE | **P** — carried verbatim, confirmed byte identical to 186's agent 560 |
| `SCENARIO VOLUME DISCIPLINE` / `{{maxscenarios}}` | **O** — neither P nor 186 caps scenario count. Added because each scenario is a parallel execution and the count drives cost |
| Five KBs named with ids (562, 567, 568, 569, 570) | **186** + **P** — verified identical by id in both exports |

## Agent 02 — test case generator

| Rule | Source |
|---|---|
| INSTRUCTIONS 1–10, rules 4, 4a, 5, 5a, 6, 6a, 6b, PROCESS STEPS 1–12 incl. 9a, placeholder resolution, error handling | **P** — carried verbatim |
| FEEDBACK HANDLING 1–4 | **P**, retargeted from `reworknotes` to `{{regenerate}}` |
| `OUTPUT VOLUME DISCIPLINE` | **P** + **O** — P/JD hardcode the numbers, this parameterises them as `limits.*` |
| `QUALITY RULES` — atomicity, traceability, preconditions, measurable results, type balance | **P** |
| `REQUIRED CLASSIFICATIONS` heading + rule 7a column semantics | **O** — renamed from P's `REQUIRED TEST CASE TYPES`, which listed Positive/Negative/Edge under a heading naming the column that must hold Functional/Regression. **Semantics identified by the user** from the manual test cases; P and JD both still carry the swapped values |
| "Generate only functional test cases" | **JD** + **186** |
| `CONSISTENCY AND UNIQUENESS GATES` — semantic dedup, uniqueness signature, step depth consistency, minimum depth, edge carries more, baseline step preservation, structural validity | **186** — general quality rules, adopted as written |
| Discriminating attribute isolation, state × attribute matrix | **186** rules 6c/6d, **generalised**. 186 says "Aid Category"; this says "a business attribute that takes several distinct values" and keeps Aid Category 198/199 as the worked example, so it applies to stories with no aid categories |
| Eligibility lookup by Member SSN (NM109) + the `CMC_SBEL_ELIG_ENT` / `CMC_MEME_MEMBER` query | **186** — concrete schema knowledge absent from P |
| Which SQL Server: `crt_69` for FACETS validation, `crt_72` for launch/connection and all non FACETS | **186** — user confirmed 2026-08-17 that 186 is correct here; P and JD know only `crt_72` |
| TM UI `https://edifecstmenr-crt.caresource.corp:8443/tm/logon/logon.jsp` | **186** — user confirmed 2026-08-17. P and JD carry `edifecstm-crt`, which is stale |
| Angle bracket policy (runtime data allowed) | **P** relaxed by **O** — JD and 186 both forbid all angle brackets and use `[TEST DATA: …]`. **Open decision, not yet adopted** |
| `OUTPUT FORMAT` nested JSON | **O** — P/JD/186 all emit a table. Measured 43% less output; the tool expands it back to the same 13 columns |

## Agent 03 — reviewer

| Rule | Source |
|---|---|
| Role, input description, checks 1–4, scoring bands, "no deduct" clauses, gaps/feedback rules | **P** (agent 559) |
| Checks 5–9 as literal string tests and counts, EVIDENCE RULE | **P** rewritten by **O** — P stated them as prose judgements and the reviewer invented violations, pinning every score at 85 |
| Per test case `scores[]` instead of one batch confidence | **O** — enables per test case repair |
| Checks 10–12: semantic duplicates, step depth consistency, edge negative path | **186** — the generator side of these is 186's; a gate the generator must pass needs a checker |
| Tool call, `pat_token`, `roundNo`, scenariojson verbatim | **P**, **removed** — the tool owns the loop |

---

## Deliberately NOT adopted from 186

| 186 has | Why not |
|---|---|
| 11 columns, no `ScenarioId` / `AcceptanceCriteriaRef` | User confirmed the 13 column contract twice; `CLAUDE.md` invariant 3 |
| "Generate as many test cases as needed", no caps | Impossible under the client's 240s ACA ceiling |
| No reviewer, no self healing | The orchestrator exists to have one |
| Text to Excel agent 394 + tool 53 | Not adopted yet, but it is the right answer for delivering the table — already built and approved |
