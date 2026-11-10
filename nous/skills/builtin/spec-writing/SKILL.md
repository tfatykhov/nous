---
name: spec-writing-verify
description: Writing a spec, design note, plan, proposal or review about code or data. Each claim cites its evidence (file:line and caller, or query and result); unchecked claims are labelled; a review subtask runs before delivery.
---
1. Before a claim about code: open the file, cite file:line and who calls it (grep the callers). The code is under /app/nous, the feature docs under /app/docs/features (INDEX.md) and /app/docs/reference.
2. Before a claim about data: run the query, cite it and its result (count, ids, time). Numbers come from a query or log line run this turn, never from memory.
3. A claim you could not check reads "unverified: <claim> (would verify by <how>)". Never present a guess as a fact.
4. If the text is longer than one page: before delivering, spawn a review subtask with the draft and "check every file:line and query against the code and data; list what does not hold", then fix or label what it finds.
5. End with an Evidence list: files read, queries run.
