# History and findings

What was wrong, what the evidence was, and what changed. Every figure here came from an
activity log or an artefact export, not from estimation.

## Where it started

The workflow produced test cases, but each story took two full executions, hit a
600-second timeout, and burned six `RemoteDisconnected` LLM failures on the way.

| | Before | After the fixes |
|---|---:|---:|
| LLM connection failures per run | 6 | **0** |
| 600 s timeouts | 1 | **0** |
| Generator output | 109,214 chars | 48–64 KB |
| Reviewer output | ~104,000 chars | ~3,000 |
| Variables bound | no | yes |
| Rework loop | never fired usefully | fires and counts |

## The findings, in order

### 1. The reviewer was transcribing the whole table

Its prompt said *"add the testcases along with the original output"*, so it re-emitted the
generator's 100 KB table. Its own verdict was 183 characters; the other 103,000 were a copy.
That completion is what the gateway kept severing.

**Fixed:** the reviewer now returns the JSON verdict only. Failures moved off the reviewer
entirely — proof being that reviewer sections of later logs contain zero failures while it
still ingests a ~109 KB input.

### 2. Placeholders had no braces

Agent 3's `### INPUT DATA` read `Test scenariojson:tsInputJson_string_true` — no braces, so
never substituted. Confirmed side by side in one log: agent 367 with braces received the
real ADO JSON; agent 3 without braces received the literal string.

**Fixed:** braces added. Verified by zero unbound literals in the next run.

### 3. The loop had no brake

Nothing counted rounds, and with the threshold at 97 against a rubric whose top band starts
at 90, sound output scored 92 and re-queued forever.

**Fixed:** tool 76 takes a `round_no`, stamps the next value itself, and stops at 3.
Verified: `round_no` went 0 → 1 across two logs, the 1 having been stamped by the tool.

### 4. The threshold was unreachable

97 in the tool, while the reviewer's own rubric said 90–100 means *"all four basics pass —
the default when nothing is broken"*. Four consecutive runs returned **exactly 97**, the
number its own prompt named as the pass mark.

**Fixed:** 90 in both places.

### 5. Hard gates fired on false positives

Checks 6, 7 and 8 were written as prose judgements. The reviewer reported a NAS path as a
"schema document name", claimed `per the AC` appeared when it occurs zero times, and called
8 test cases across 4 scenarios a breach of "no more than 2 per scenario". Verified against
the data: that run had **zero** angle brackets, **zero** `kb_` strings, **zero** meta-label
strings, exactly 2 cases per scenario and 10 steps each. Every gate actually passed.

**Fixed:** literal string lists, count-then-compare, and an evidence rule — quote it or it
passes.

### 6. The angle-bracket ban was stricter than the human standard

The manual test cases use `<ISA13>` 21 times and `<InterchangeID>` 42 times, in exactly the
SQL positions the generator was penalised for.

**Fixed:** runtime test data in angle brackets is allowed; unresolved design values are not.

### 7. The whole workflow executes twice per trigger

Not duplicate logging — the two tool calls in one log carried **different** `tsInputJson`,
meaning two different scenario sets from two different runs. Two "Reviewer started" events
against two tool calls, one each, so the reviewer was not looping.

**Not fixed.** `maxIter: 2` bounds the reviewer as a precaution, but the evidence points at
the trigger firing twice, which is outside these files.

## Comparison against the manual gold standard

Human-authored test cases for the same story (WI 640764), for reference:

| | Manual | Generated (best run) |
|---|---:|---:|
| Test cases | 21 | 8 |
| Steps each | 23 | 10 |
| Total rows | 483 | 80 |
| Columns | 11 | 13 |
| ACs covered | all 7 + 3 regression | 4 scenarios |
| Trading partner named | 67 times | **0** |
| SQL target | `EDIStageArchive.dbo.*` | FACETS `CMC_*` |

The manual model is roughly **3 test cases per acceptance criterion** — positive, edge,
negative — with a fixed 23-step skeleton: prepare file (1–4), stage and archive (5–8), TM UI
(9–12), date assertions (13–15), SQL (16–22), capture audit evidence (23). All 21 share one
7-point environment precondition.

Current settings move toward this: 3 cases per scenario, 15–20 steps, trading partner
required, column semantics aligned. Coverage is still the gap — 4 scenarios cannot represent
16 acceptance criteria and DoD items.

The manual workbook is not copied into this repo to avoid a second source of truth. It was
provided as `TestCases_WI640764_834_AR_PASSE 1 1.xlsx`.

## Two credentials found in the activity logs

An Azure DevOps PAT in plaintext and an AAVA bearer JWT (named user, issued 2026-08-04,
expiring 2026-12-31) both appear in full in exported activity logs, because they are passed
as agent input variables and prompt context is logged.

**Not fixed.** The proper fix is `AVASecret.getValue(...)` inside the tool with the argument
as a dev-only fallback. Until then, treat exported logs as containing live secrets.
