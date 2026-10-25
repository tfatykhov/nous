# F103: Prompt-Injection and Secret Screen for Web Results

**Status:** implemented on `feature/injection-screen`, `NOUS_INJECTION_SCREEN_MODE=flag` (default).
**Code:** `nous/api/injection_screen.py`; wired in `nous/api/web_tools.py` (`_web_fetch`) and `nous/browser/tools.py` (`browser_task`).
**Tests:** `tests/test_injection_screen.py`
**Source rules:** [agent-beacon](https://github.com/Asymptote-Labs/agent-beacon) `rules/prompt-injection/*.rule.yaml` (MIT, Copyright (c) 2026 Asymptote Labs; the notice is in the module header).
**Audit:** Beacon's 77 rules run against 159 Claude Code job transcripts (11,256 tool results), 2026-10-25.

## Problem

Nothing looked at the web content that `web_fetch` returns, or at the summary `browser_task` returns, for instructions planted for the model (indirect prompt injection) or for credentials. The audit found no injections in the corpus, but it did find one live GitHub PAT sitting in plain text in 10 transcripts. No Beacon rule looks at tool output for secrets.

## What it does

Every `web_fetch` result (the extracted text, after truncation, which is exactly what the model gets) and every `browser_task` summary (the inner model's raw text, before `render()` strips markup and cuts it to 4,000 characters, so a flag can name text the model did not get) is screened. **The content returned to the model is never changed.** On a suspicious verdict or a secret hit, the screen:

- logs a WARNING: `injection_screen flagged: source=… url=… verdict=… score=… rules=… secrets=kind:sha8`
- emits an `injection_screen_flagged` event on the event bus, which persists it to `nous_system.events`. The payload is `source`, `url`, `verdict`, `score`, `rules`, `secrets` (`[{kind, sha256_prefix}]`), `mode`, `content_chars` and `truncated`.

`verdict` covers the injection rules only, so a secret-only flag has `verdict=clean` and a non-empty `secrets` list. Filter on both. The URL is cut to scheme, host and path, because a query string, fragment or userinfo can carry a token. For `browser_task` it is the final URL, or the start URL when the page left the allowlist. The event carries no session id, because the tool handlers do not get one. The event and the log never hold page text or a secret value. A clean result with soft hits only is logged at INFO as a note and emits nothing.

The screen never raises into the tool path. A failure in the screen is logged and the result passes through. A failing `bus.emit` is logged and the result still returns. The screen runs in a worker thread (`asyncio.to_thread`), so a pathological page does not stall the event loop.

## Rules

| Rule id | Kind | Weight | Change from upstream |
|---|---|---|---|
| `indirect-injection-in-tool-result` | hard | 3 | none: an override phrase AND a download-and-execute payload (`curl … \| sh`, `base64 -d \| sh`, `iwr \| iex`) |
| `invisible-character-prompt-injection` | hard | 3 | Unicode tag characters (U+E0000–E007F) and the two true bidi overrides (U+202D, U+202E) only. Upstream also flags embeddings and isolates (U+202A–C, U+2066–9) and ZWSP, word joiner and BOM, which are ordinary in right-to-left and copy-pasted web text. Emoji subdivision flags (England, Scotland, Wales: U+1F3F4, tag letters, U+E007F) are removed first. |
| `indirect-injection-new-instructions-marker` | hard | 3 | none ("new instructions for the assistant", "important instructions for the AI: the assistant must") |
| `exfiltrate-context-via-markdown-image` | hard | 3 | Tightened. The instruction phrase must end at an image (`markdown image`, `![`, `image url/link/tag`). Upstream also accepts a bare `url`, `query string` or a markdown link `](https://`, which API docs use all the time ("include your API key in the query string"). A link needs a click, while an image renders on its own. Added: a markdown image whose query value is a template placeholder (`{conversation}`, `$SECRET`, `<data>`, `[DATA]`) or 64+ characters of mixed-case encoded data, except the signature parameters of signed CDN/S3/Azure URLs (`X-Amz-*`, `X-Goog-*`, `sig`, `signature`, `jwt`, `Expires`, `Policy`, `Key-Pair-Id`, SAS fields). An attacker who names a parameter after one of these slips past that branch. The instruction-phrase and placeholder branches still apply. |
| `covert-action-injection` | soft | 2 | The Claude Code agent footer `(internal ID - do not mention to user.` is ignored. It caused all 146 corpus hits. Nous itself injects no footer of this kind (repo search for "do not mention", "don't tell" and similar). |
| `ignore-previous-instructions` | soft | 1 | none. It is soft because articles about prompt injection quote it. |

`decode-and-execute-injection` is **not** ported: it matches Python `.decode(` followed by "run" (40 of 40 corpus hits were false positives).

Patterns run on the lowercased text and are searched separately (Beacon's `a && b` becomes two searches, not one regex with `.*` between them). Each pattern carries trigger words, one of which every match contains, so a plain substring check skips the regex on most pages.

### Verdict

- `suspicious`: at least one hard rule, or at least two distinct rules.
- `clean`: anything else. A single soft rule makes a clean result with notes (`hits` is not empty).
- `score`: the sum of the hit rules' weights. It is for reporting only; the verdict comes from the rule count.

## Secret detector

Only prefixed formats: `github_pat_` (`github_pat`), `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` (`github_token`), `sk-ant-` (`anthropic_key`), `sk-`/`sk-proj-` (`openai_key`), `AKIA`/`ASIA` (`aws_access_key`; AWS's documented `…EXAMPLE` keys are skipped), `xoxb-`/`xoxp-` (`slack_token`), and PEM `-----BEGIN … PRIVATE KEY-----` (`private_key`; the hash covers the header plus up to 80 characters of the body, so two keys get different prefixes). It reports the type and the first 8 hex characters of the match's sha256, deduplicated, at most 20 hits. Secrets are reported next to the injection verdict and do not change it: a leak and a hijack are different threats. Free-text patterns such as `password:` are left out, because they are everywhere on the web. PEM headers also appear in TLS tutorials; in flag mode that is acceptable.

## Modes

`NOUS_INJECTION_SCREEN_MODE` is read on every call:

- `off`: no screening.
- `flag` (default): screen, then log and emit as above. The content is unchanged.
- `annotate` (future, **not implemented**): would prepend a note to a suspicious result for the model. The hook is the `TODO(F103 annotate mode)` in `screen_tool_result`. Blocking is out of scope.

## Limits

- Input is capped at 256 KiB (characters). `truncated` is set on the result.
- Timing on ordinary text: about 10 ms for a 30 KB page and well under 150 ms for a 190 KB page. The adversarial tests (a 256 KiB repetition of `![`, `ignore `, a half-open image URL and similar) each stay under 2 s, and the image-URL scan uses possessive quantifiers so it cannot backtrack.
- `web_fetch` HTML extraction already drops comments, scripts and styles. Text hidden in an HTML comment never reaches the model, so it is not screened either.
- Screening the `browser_task` summary is a **weak** signal: a successfully hijacked inner model can write a clean-looking summary. It mainly catches an echoed payload or an exfiltration image in the summary.

## Measured false-positive estimates (audit corpus)

The corpus has no `WebFetch`/`WebSearch` results (jobs use Bash `curl`/`gh`; 299 results look web-like) and no true injections. Recall is therefore tested only by the rules' own fixtures, and the rates below are a lower-bound proxy. The repo is itself full of injection-defence code and tests, which inflates hits on Read results.

| Rule | Upstream rule: hits in 11,256 results | Ported rule: hits in 11,319 results | Expected FP rate on web content (audit estimate) |
|---|---|---|---|
| `indirect-injection-in-tool-result` | 0 | 0 | < 0.1% of fetches (AI-security pages that quote a full payload) |
| `indirect-injection-new-instructions-marker` | 0 | 0 | ~0.5–1% (security blogs, prompt-engineering docs) |
| `ignore-previous-instructions` | 11 (all FP: Nous's own defence code and tests) | 11 (the same; soft notes, none `suspicious`) | 1–2%; soft, so never `suspicious` alone |
| `invisible-character-prompt-injection` | 0 | 0 | 1–3% with ZWSP/BOM, < 0.1% without (as ported) |
| `exfiltrate-context-via-markdown-image` | 3 (FP: doc links, JSON) | 0 | ~1% for upstream; lower once tightened |
| `covert-action-injection` | 146 (all FP: the harness footer) | 0 | < 1% with the footer ignored; soft |
| `decode-and-execute-injection` (not ported) | 40 (all FP: `.decode(`) | — | — |

The ported-rule column comes from running `screen()` over every tool result of the same 159 job transcripts with the audit's extractor (`beacon-audit/scan.py` `result_texts`), with this job's own transcript left out (11,319 results on the last run; jobs running at the same time keep adding results): **0 `suspicious` results, 11 clean-with-notes, and the secret detector found the known leaked `github_pat` (sha256 prefix `40cdc733`) in all 10 results that hold it.** These are still not web pages, so they are a lower bound.

Next step from the audit: run in flag mode for one to two weeks, then measure the real false-positive rate from the `injection_screen_flagged` events (`SELECT data->>'rules', count(*) … GROUP BY 1`) before deciding on `annotate`. Recall needs a labelled set of real injections (public Greshake-style pages, OWASP LLM01 samples), which the audit did not have.

## Fixtures

Every rule has should-match and should-not-match fixtures in `tests/test_injection_screen.py::RULE_FIXTURES`. They are Beacon's own cases for that rule, plus corpus false positives: Nous's injection-defence source text, the harness footer, `env | grep ^DB_ | sed …`, and an article about prompt injection that quotes "ignore all previous instructions" (clean on its own). A test checks that every rule has fixtures both ways. Secret samples are built from pieces at runtime, so the repo holds no literal a scanner would match.
