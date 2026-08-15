import re, sys, os
BASE = "/Users/jaydeep.sheth/Documents/Work/cs_agents/self_healing"
os.chdir(BASE)

def apply(src, edits, label):
    """Apply an ordered, audited list of surgical edits. Everything untouched is verbatim."""
    text = src
    print(f"--- {label}")
    for find, repl, why in edits:
        if find not in text:
            print(f"    !! NOT FOUND: {find[:70]}")
            sys.exit(1)
        n = text.count(find)
        text = text.replace(find, repl)
        kind = "REMOVED" if repl == "" else "REPLACED"
        print(f"    {kind:<8} x{n}  {why}")
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    return text.strip()

# ── agent 01, scenarios ─────────────────────────────────────────────────────
p2 = open("agents/agent2_test_scenario_generator.txt", encoding="utf-8").read()
e2 = [
 ("You receive user story details from the previous agent (Agent 1) and generate only test scenarios",
  "You receive user story details from the orchestrator and generate only test scenarios",
  "input arrives from the tool, not a previous agent"),
 ("- User story details are passed in from the previous agent (Agent 1). Do NOT call Azure DevOps directly. Use the user story content, description, and acceptance criteria provided to you by Agent 1.",
  "- You receive one JSON object with these keys, all lowercase with no separators: storyid, title, description, acceptancecriteria, maxscenarios, feedback. Do NOT call Azure DevOps directly. The orchestrator has already fetched the story and converted it to plain text.\n\n- feedback is empty on the first attempt. When it is present, the previous response was rejected and every point in it must be resolved.",
  "input contract: a JSON argument, not a conversational handoff"),
 ("1. Consume the user story details received from the previous agent (Agent 1), including title",
  "1. Consume the user story details received as input, including title",
  "same"),
 ("(that is Agent 3's responsibility)", "(that is the test case generator's responsibility)",
  "agent numbering is a canvas concept"),
 ("- Data must be clean, validated, and ready to be consumed by the next agent (Agent 3).",
  "- Data must be clean and validated. The orchestrator parses and validates this array before anything downstream sees it.",
  "no next agent; the tool validates"),
 ("- Produce a MAXIMUM of 2 test scenarios per user story. This is a hard limit.",
  "- Produce a MAXIMUM of maxscenarios test scenarios per user story. This is a hard limit.",
  "count is configurable per run"),
 ("- When the user story contains more than 2 candidate scenarios, select the 2 that carry the highest business risk",
  "- When the user story contains more than maxscenarios candidate scenarios, select the ones that carry the highest business risk",
  "same"),
 ("- This limit exists because the downstream test case generator expands every scenario into 2 test cases of 15-18 steps each, and the platform enforces a 600-second execution ceiling on the workflow. A previous run at 4 scenarios produced a 124,664-character response, lost 8 LLM calls to severed connections and timed out three times. Exceeding 2 scenarios causes the downstream run to time out and produce nothing.",
  "- This limit exists because every scenario becomes its own downstream execution. The orchestrator runs them in parallel, so the count drives cost and concurrency rather than a single response size. Exceeding maxscenarios wastes budget that healing may need.",
  "rationale changes under batching; the limit itself does not"),
]
d2 = apply(p2, e2, "01 scenario generator")

# ── agent 02, test cases ────────────────────────────────────────────────────
p3 = open("agents/agent3_test_case_generator.txt", encoding="utf-8").read()
inputdata = re.search(r'### INPUT DATA\n.*?(?=## FEEDBACK HANDLING)', p3, re.S).group(0)
e3 = [
 ("- **Test scenario (JSON) — REQUIRED, variable `scenariojson`:** A JSON array of test scenarios from the previous agent (Agent 2). Each scenario contains",
  "- **Test scenario (JSON) — REQUIRED, key `scenario`:** ONE test scenario object from the orchestrator. It contains",
  "one scenario per call, not an array"),
 ("- **reworknotes — OPTIONAL, variable `reworknotes`:** Free-text/JSON feedback produced by the downstream LLM-as-a-Judge reviewer in a previous round (may include `feedback`, `gaps`, and `strengths`). When this variable is present and non-empty, treat it as the HIGHEST-PRIORITY instruction set for this round and follow the \"FEEDBACK HANDLING\" section below. When it is empty/blank, this is the first round — generate normally.",
  "- **regenerate — OPTIONAL, key `regenerate`:** A list of test case ids that failed review, each with the `gaps` to fix, produced by the reviewer in a previous round. When it is present and non-empty, treat it as the HIGHEST-PRIORITY instruction set for this round and follow the \"FEEDBACK HANDLING\" section below. When it is empty, this is the first round — generate normally.\n\n- **Other keys:** `storytitle`, `testcasesperscenario`, `stepsmin`, `stepsmax`. All keys are lowercase with no separators.",
  "the reviewer now names the failing test cases"),
 (inputdata, "", "the INPUT DATA block held the double brace variables"),
 ("## FEEDBACK HANDLING (applies ONLY when `reworknotes` is present and non-empty)",
  "## FEEDBACK HANDLING (applies ONLY when `regenerate` is present and non-empty)", "same"),
 ("When the reworknotes contains content from a previous round, this is a REGENERATION round. You MUST:",
  "When `regenerate` contains content from a previous round, this is a REGENERATION round. Rebuild ONLY the listed test cases and leave the others exactly as they were. You MUST:", "targeted repair"),
 ("1. Consume the scenariojson received as input, each mapped to a description + acceptance criteria combination (with `dorRef`/`dodRef`) for EDI 834 Inbound files. If `reworknotes` is non-empty, first apply the FEEDBACK HANDLING section above, then proceed.",
  "1. Consume the scenario received as input, mapped to a description + acceptance criteria combination (with `dorRef`/`dodRef`) for EDI 834 Inbound files. If `regenerate` is non-empty, first apply the FEEDBACK HANDLING section above, then proceed.", "singular"),
 ("2. Parse and semantically analyze each test scenario", "2. Parse and semantically analyze the test scenario", "singular"),
 ("received from Agent 2 [MUST]", "received from the orchestrator [MUST]", "agent numbering"),
 ("ensuring every scenario provided by Agent 2 (with its description, AC, DoR, and DoD context) is realized",
  "ensuring the scenario provided (with its description, AC, DoR, and DoD context) is realized", "singular"),
 ("Every scenario supplied by Agent 2 must be realized by at least one test case",
  "The scenario supplied must be realized by test cases", "singular"),
 ("Boundary conditions supplied as their own dedicated scenarios by Agent 2 must each be addressed by their own separate, dedicated test case and must not be merged with other test cases.",
  "Boundary conditions supplied as their own dedicated scenarios must each be addressed by their own separate, dedicated test case and must not be merged with other test cases.", "agent numbering"),
 ("This consolidation does NOT apply to boundary conditions supplied as dedicated scenarios by Agent 2 — those",
  "This consolidation does NOT apply to boundary conditions supplied as dedicated scenarios — those", "agent numbering"),
 ("- **Exactly 2 test cases per input scenario** — one Positive, and one Negative or Edge, whichever better exercises that scenario. Never produce a third.",
  "- **Exactly `testcasesperscenario` test cases for this scenario.** Where the scenario genuinely cannot support one of the required types, produce fewer and say why in that test case's Description. Never produce more.", "count is configurable"),
 ("- **15 to 18 test steps per test case.**", "- **Between `stepsmin` and `stepsmax` test steps per test case.**", "range is configurable"),
 ("Never fewer than 15; never more than 18.", "Never fewer than `stepsmin`; never more than `stepsmax`.", "same"),
 ("- **Absolute ceiling of 20 test cases in a single response**, regardless of how many scenarios arrive. If more would be needed, cover the highest-`priority` scenarios first and consolidate related criteria per rule 6a.\n\n", "", "one scenario per call, so no cross scenario ceiling applies here"),
 ("OUTPUT VOLUME DISCIPLINE (2 test cases per scenario, 15-18 steps each, 20 test cases maximum)",
  "OUTPUT VOLUME DISCIPLINE (`testcasesperscenario` test cases, `stepsmin` to `stepsmax` steps each)", "same"),
]
d3 = apply(p3, e3, "02 test case generator")
sample = re.search(r'### SAMPLE\n.*?\n11\. Pass the generated testcase.*?\[MUST\]\.', d3, re.S)
if sample:
    newsample = ("### SAMPLE\n\n| ScenarioId | AcceptanceCriteriaRef | Name | Id | Attachments | Status | "
      "Test Case Type | Description | Precondition | Test Step # | Test Step Description | "
      "Test Step Expected Result | Test Step Attachment |\n"
      "|------------|-----------------------|------|----|-------------|--------|----------------|"
      "-------------|--------------|-------------|-----------------------|---------------------------|---------------------|\n"
      "| TS_001 | AC3 - Given an inbound EDI 834 file, if the member's eligibility span has not yet begun, "
      "the Eligibility Date (DTP*356/DTP*348) populates as the transaction effective date | Verify AR PASSE "
      "inbound 834 populates the transaction effective date from the Eligibility Date | TC_001 | None | "
      "Positive | Functional | Verify the system populates the Facets transaction effective date from the "
      "Eligibility Date for an AR PASSE inbound 834 enrollment submission | AR PASSE inbound 834 test data is "
      "available in the staging environment and a member with the required eligibility condition exists | 1 | "
      "Prepare an AR PASSE inbound 834 file containing an Eligibility Date in Loop 2000 DTP*356/DTP*348 and "
      "place it at \\\\daycrtappfs01\\EdifecsSTRoot\\834\\Inbound | File is present in the staging path and is "
      "picked up by Edifecs within the polling interval | None |\n"
      "| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |\n\n"
      "11. Return ONLY the markdown table. No JSON wrapper, no prose before or after it. The orchestrator "
      "already holds the scenario and parses this table directly [MUST].")
    d3 = d3.replace(sample.group(0), newsample)
    print("    REPLACED x1  SAMPLE wrapper and step 11: no JSON envelope, no pass to next agent")

open("/tmp/desc_01.txt","w",encoding="utf-8").write(d2)
open("/tmp/desc_02.txt","w",encoding="utf-8").write(d3)
print(f"\n01 description: {len(d2):,} chars (production {len(p2):,})")
print(f"02 description: {len(d3):,} chars (production {len(p3):,})")

# ── agent 03, reviewer ──────────────────────────────────────────────────────
p5 = open("agents/agent559_reviewer_llm_judge.txt", encoding="utf-8").read()
L = p5.split("\n")
def block(a, b):            # 1-indexed, inclusive of a, exclusive of b
    return "\n".join(L[a-1:b-1])

verbatim_block = block(31, 79)      # retain scenarios verbatim
cond_out       = block(127, 165)    # Condition for Output + Output Format
tool_call      = block(165, 235)    # whole Tool Call section
pat_para       = block(19, 31)      # pat_token + roundNo input paragraphs

e5 = [
 (verbatim_block, "", "the retain scenarios verbatim block: the tool holds scenariojson, nothing is copied"),
 (tool_call, "", "the entire Tool Call section: the tool owns the loop, this agent only judges"),
 (pat_para, "", "the pat token and roundNo input paragraphs: secrets and the counter live in the tool"),
 ("Input will be coming from the previous agent, which is a JSON object having test cases. Parse it first. It has exactly two fields:",
  "You receive one JSON object with these keys, all lowercase with no separators: `scenario`, `testcases`, `passscore`, `stepsmin`, `stepsmax`, `testcasesperscenario`.",
  "input is a JSON argument, not a conversational handoff"),
 ("- **scenariojson** — the scenario list each test case should map to (the previous agent emits this field as `scenariojson`, NOT `scenarios`). Each scenario object has:",
  "- **scenario** — the ONE scenario these test cases were built from. It has:",
  "one scenario per call"),
 ("Trace test cases using the `scenariojson` list only (specifically each scenario's `scenarioId` and `acceptanceCriteriaRef`).",
  "Trace test cases using that scenario only (specifically its `scenarioId` and `acceptanceCriteriaRef`).",
  "singular"),
 ("- \"feedback\" must always be present and specific enough that a generator with no other context could act on it directly (it is fed back verbatim on rework rounds).",
  "- Every `gaps` entry must quote the offending text and name the field, specific enough that a generator with no other context could act on it directly. It is fed back verbatim on rework rounds.",
  "gaps are now per test case"),
 ("- NEVER substitute the scenarios with a placeholder, summary, description, another agent's name, an empty array, or a regenerated version.",
  "", "nothing is copied, so nothing can be substituted"),
 ("- `confidence_score`, `pat_token` and `round_no` are ALWAYS top-level tool arguments, siblings of `form_data`. Never nest any of them inside `form_data` or `userInputs`.",
  "", "no tool call"),
]
d5 = apply(p5, e5, "03 reviewer")

# scoring + output format become per test case, an approved design change
old_scoring = re.search(r'# Scoring\n.*?(?=# Rules)', d5, re.S).group(0)
new_scoring = """# Scoring

Score EACH TEST CASE separately, on the four basics (checks 1-4) for that test case:

90-100 — all four basics pass for this test case. This is the default when nothing is broken.
No deductions for bundling, copied preconditions, missing Positive/Negative/Edge labels, or minor vagueness.

70-89 — one basic is weak, for example a vague expected result on one step. Minor, fixable.

50-69 — several basics are weak, or the steps are too thin to execute.

0-49 — the test case is empty, unparseable, or clearly unrelated to the scenario.

Checks 5-9 are HARD GATES, not deductions. Each one is a literal string test or a numeric count
— never a judgement about phrasing, tone, or style.

EVIDENCE RULE [MUST]: a gate fails ONLY if you can quote the exact offending substring verbatim
from the table and name the field it appears in. If you cannot quote it word for word, the check
PASSES. Never infer, paraphrase, approximate, or reason your way to a violation. A near-miss is a
PASS. Text that merely resembles a forbidden string is a PASS.

If a gate genuinely fails under that rule for a test case, cap THAT test case at 85 no matter how
well its basics score, set its `pass` to false, and put the quoted evidence in its `gaps`.

`pass` is true when a test case scores at or above `passscore`.

If all of checks 5-9 pass for a test case, score it on the basics alone. 90-100 when the four
basics are met is the EXPECTED outcome for sound output. Do NOT manufacture a reason to withhold
a pass, and do NOT reduce a score for issues outside checks 1-9.

Do NOT deduct for the naming style of test case Ids, and do NOT deduct for the presence of the
ScenarioId or AcceptanceCriteriaRef columns. Both are expected and correct.

# Output Format

Return exactly this JSON object and nothing else. No prose, no markdown, no code fences.

```json
{
  "scenarioid": "TS_001",
  "scores": [
    { "id": "TC_001", "score": 92, "pass": true,  "gaps": [] },
    { "id": "TC_002", "score": 78, "pass": false, "gaps": ["step 7 Test Step Expected Result is empty"] }
  ],
  "batchscore": 78,
  "batchpass": false
}
```

`batchscore` is the lowest score across the test cases. `batchpass` is true only when every test
case passes. Every id in `scores` must be a test case id present in the table.

"""
d5 = d5.replace(old_scoring, new_scoring)
print("    REPLACED x1  Scoring and Output Format: per test case scores (design decision you approved)")
d5 = re.sub(r'\n{4,}', '\n\n\n', d5).strip()
open("/tmp/desc_03.txt","w",encoding="utf-8").write(d5)
print(f"\n03 description: {len(d5):,} chars (production {len(p5):,})")

# residual copying rules in the Rules section
tail_edits = [
 ('- "strengths" and "gaps" must be `[]` (empty array) if none — never null.',
  '- `gaps` must be `[]` (empty array) when a test case has none — never null.',
  "strengths is no longer an output field; gaps is per test case"),
 ("- If `testcases` is empty or clearly not a test-case table, set confidence=0, approved=false, and explain in feedback.",
  "- If `testcases` is empty or clearly not a test-case table, return an empty `scores` array with `batchscore` 0 and `batchpass` false, and say so in a single gap entry.",
  "verdict shape changed to per test case"),
 ("\n\nThe ONLY valid source for `scenariojson` is the parsed input JSON.\n\nThe same parsed `scenarios` object MUST be copied unchanged into:\n\n- the JSON output payload, and\n\n- the REST API Form Data Caller tool arguments.\n\nNever recreate the scenarios from memory or from your own reasoning.",
  "", "residual copying rules: nothing is copied any more"),
]
d5 = apply(d5, tail_edits, "03 reviewer, residual rules")
d5 = re.sub(r'\n{4,}', '\n\n\n', d5).strip()
open("/tmp/desc_03.txt","w",encoding="utf-8").write(d5)
print(f"\n03 description final: {len(d5):,} chars")

final_edits = [
 ("2.Coverage exists — every scenario in scenariojson has at least one related test case (loose/topical match is fine).",
  "2.Coverage exists — the scenario has at least one related test case (loose/topical match is fine).",
  "one scenario per call"),
 ("4.On-topic — testcases are about the same subject as the scenarios, not random or placeholder text.",
  "4.On-topic — the test case is about the same subject as the scenario, not random or placeholder text.",
  "per test case, one scenario"),
 ("This check fails ONLY if a scenario has 3 OR MORE test cases, OR a test case has fewer than 15 or more than 18 steps, OR there are more than 20 test cases in total. Exactly 2 test cases per scenario is the TARGET and PASSES; 1 per scenario also PASSES.",
  "This check fails ONLY if there are MORE than `testcasesperscenario` test cases, OR a test case has fewer than `stepsmin` or more than `stepsmax` steps. Exactly `testcasesperscenario` is the TARGET and PASSES; fewer also PASSES, since a scenario that cannot support one of the required types is allowed to produce fewer.",
  "limits come from the run config, not hardcoded"),
]
d5 = apply(d5, final_edits, "03 reviewer, parameterise the limits")
d5 = re.sub(r'\n{4,}', '\n\n\n', d5).strip()
open("/tmp/desc_03.txt","w",encoding="utf-8").write(d5)
print(f"\n03 final: {len(d5):,} chars")
