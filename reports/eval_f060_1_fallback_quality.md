# F060.1 fallback quality probe — `nous-prod-snapshot`

- Sampled: **10** real stuck-open episodes (transcript NULL, summary >= 20 chars)
- Summarized: 8, errors/skipped: 2
- Guard fired (any suspects): **5/8** (62%)
- Above threshold (>2 suspects): **0/8** (0%)
- Avg suspect entities per output: **0.8**
- Avg candidate_facts emitted: **1.9**

| ep_id | input chars | output chars | facts | suspects | suspect samples |
|---|---:|---:|---:|---:|---|
| 6cc10862... | 200 | 1389 | 3 | 0 | — |
| d2344031... | 56 | 1030 | 1 | 1 | user asked about |
| a983450f... | 121 | 1294 | 2 | 1 | https://github.com/tfatykhov/nous-longmemeval, |
| 122bee36... | 200 | 1268 | 2 | 0 | — |
| 20fd2889... | 200 | 1168 | 2 | 1 | writing style corrections |
| 9aea49e2... | 119 | 1064 | 2 | 0 | — |
| 60b34456... | 120 | 1235 | 2 | 1 | directed acyclic graph |
| 3fc54928... | 58 | 968 | 1 | 2 | directed acyclic graph, user asks if |

## Sample inputs and outputs

### 6cc10862...

**Input** (200 chars):
> check this
ous's scheduled tick (every 30 min per --tick-interval-seconds=1800) wasn't firing reliably. CT has a self-alarm that fires
  when no /snapshot calls have come in for 5400s (90 min = ≥3 con

**Output title:** Nous Scheduled Tick Reliability Issue Investigation
**Output summary:** The transcript fragment describes a reliability problem with Nous's scheduled tick mechanism. The tick interval is configured at 30 minutes (--tick-interval-seconds=1800) but was not firing reliably. A self-alarm exists in the system (referred to as CT) that triggers when no /snapshot calls have been received for 5400 seconds (90 minutes), which corresponds to 3 or more consecutive missed tick int

### d2344031...

**Input** (56 chars):
> what is this parameter is for for nous - NOUS_MAX_TOKENS

**Output title:** User Asked About NOUS_MAX_TOKENS Parameter Purpose
**Output summary:** The user asked a single question about the purpose of the NOUS_MAX_TOKENS parameter in the Nous cognitive agent framework. The transcript contains only the question itself — no answer or explanation was provided within the recorded exchange. The question was directed at understanding what this environment variable or configuration parameter controls within the Nous system.

**Guard suspects:**
- `user asked about`

### a983450f...

**Input** (121 chars):
> ok i created repository - https://github.com/tfatykhov/nous-longMemEval go ahead create initial structure/ specs  for now

**Output title:** Initial Structure and Specs Created for nous-longMemEval Repository
**Output summary:** The user created a GitHub repository at https://github.com/tfatykhov/nous-longMemEval and requested the assistant to create an initial structure and specifications for it. The repository appears to be related to long-term memory evaluation for the Nous cognitive agent framework. The assistant was tasked with scaffolding the initial project structure and specs for this repository. The transcript in

**Guard suspects:**
- `https://github.com/tfatykhov/nous-longmemeval,`

### 122bee36...

**Input** (200 chars):
> Run this with run_python and return ONLY the raw JSON output no commentary:

import asyncio, json
from sqlalchemy import text
from nous.storage.database import get_session
async def q():
    async wit

**Output title:** Incomplete Python Script Execution via run_python
**Output summary:** The user requested execution of a Python script using run_python, instructing the agent to return only raw JSON output with no commentary. The script was intended to query a database using SQLAlchemy's async session from nous.storage.database, with an async function defined but the transcript cuts off mid-definition — the function body and any await statements are incomplete. The script imports as

### 20fd2889...

**Input** (200 chars):
> It is good progress  with nous tiny hippo v2 but few things. 1 do not use things like heart and brain
 This is scientific article. 2. Nous was deployed 4 weeks ago. We do not have thousands of facts. 

**Output title:** Nous Tiny Hippo V2 Writing Style Corrections
**Output summary:** User provided feedback on the Nous Tiny Hippo V2 content, identifying two specific issues to correct. First, metaphorical or informal language such as 'heart' and 'brain' should not be used, as the content is a scientific article requiring precise, formal language. Second, a factual overclaim was flagged: Nous was deployed only 4 weeks ago and therefore does not have thousands of facts accumulated

**Guard suspects:**
- `writing style corrections`

### 9aea49e2...

**Input** (119 chars):
> can you add actual plan to git as well? also numbering is not correct. last feature was f038 so this one should be f039

**Output title:** Add plan to git and fix feature numbering to f039
**Output summary:** The user made two requests: (1) add the actual plan file to git, and (2) correct the feature numbering. The user specified that the last feature was f038, so the current feature should be numbered f039, not whatever number was previously assigned. The transcript does not confirm whether the assistant completed these actions — it only records the user's instructions. The requests are clear directiv

### 60b34456...

**Input** (120 chars):
> using current dag is it possible to create a loop kind of process where it can repeat same tasks until condition is met?

**Output title:** User Asks About DAG Loop Capability
**Output summary:** The user asked whether the current DAG (Directed Acyclic Graph) implementation supports creating loop-like processes that repeat the same tasks until a condition is met. The transcript contains only the user's question — no response or answer from the assistant is present. It is unclear whether the DAG supports conditional looping, as no answer was provided in the transcript.

**Guard suspects:**
- `directed acyclic graph`

### 3fc54928...

**Input** (58 chars):
> now we can use dag for a complex research is this correct?

**Output title:** User Asks If DAG Supports Complex Research
**Output summary:** The user posed a single confirmatory question: whether DAG (Directed Acyclic Graph) can now be used for complex research. The transcript contains only this question with no assistant response, no elaboration, and no additional context. It is unclear what system, framework, or prior work the user is referencing when they say 'now we can use.' No answer, confirmation, or clarification was provided w

**Guard suspects:**
- `directed acyclic graph`
- `user asks if`
