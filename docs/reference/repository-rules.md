# Repository rules

The default branch is protected by a ruleset (`scripts/ops/main-ruleset.json`), by `.github/CODEOWNERS` and by the `Protected paths` check. Together they keep the agent's own GitHub account from changing the code that defines its scores, or the owner's signals, without the owner.

## What the ruleset enforces

- The default branch cannot be deleted or force-pushed.
- Every change arrives by pull request. An ordinary pull request needs no approval, so the agent can merge its own on green CI.
- A pull request that touches a path in `.github/CODEOWNERS` needs the code owner's review. A new push dismisses an earlier approval.
- `Tests`, `DB roles` and `Protected paths` must pass, and must be reported by GitHub Actions (`integration_id` 15368). A commit status that another account posts through the API under the same name does not count.
- The repository admin role may bypass all of it. That is how the owner merges a pull request of their own that touches a protected path (`gh pr merge <n> --admin --squash`): an author cannot approve their own pull request.

## The agent's account

The agent pushes and opens pull requests as a machine user with the Write role on each repository it works on, using a classic token with the `repo` scope only. Without the `workflow` scope, GitHub refuses a push that changes `.github/workflows/`. The repository variable `NOUS_PR_LOGINS` names that login, and `Protected paths` fails any pull request it opens that touches a protected path. While the variable is empty only the commit-email rule is active, and the check's log says so.

## Applying or changing it

```bash
gh api -X POST repos/tfatykhov/nous/rulesets --input scripts/ops/main-ruleset.json        # first time
gh api repos/tfatykhov/nous/rulesets --jq '.[] | {id, name, enforcement}'
gh api -X PUT repos/tfatykhov/nous/rulesets/<id> --input scripts/ops/main-ruleset.json    # later changes
gh variable set NOUS_PR_LOGINS --body "<agent login>" -R tfatykhov/nous
```

A check added to the ruleset must run on every pull request to the default branch (no path filter and no job-level `if:`), or every pull request waits for it forever; `tests/test_ci_workflow.py` checks this. `Lint & Format` is advisory and `Android` is path-filtered, so neither is required.
