# Frame selection eval — 30 scenarios

- accuracy: **26/30 (86.7%)**
- SUT: `nous.cognitive.frames.FrameEngine.select`
- 6 default frames seeded under `frames-eval-agent`.

## Per-frame

| frame | n | correct | accuracy |
|---|---:|---:|---:|
| decision | 5 | 4 | 80% |
| task | 5 | 4 | 80% |
| question | 4 | 4 | 100% |
| debug | 6 | 5 | 83% |
| conversation | 5 | 4 | 80% |
| creative | 5 | 5 | 100% |

## Failures

| expected | actual | input |
|---|---|---|
| decision | question | `What's the trade-off here?` |
| task | debug | `Fix the failing CI pipeline.` |
| debug | task | `Why did the build fail this morning?` |
| conversation | question | `Hi Nous, how's it going?` |

## Confusion matrix

| expected \ actual | decision | task | question | debug | conversation | creative |
|---|---|---|---|---|---|---|
| decision | 4 | 0 | 1 | 0 | 0 | 0 |
| task | 0 | 4 | 0 | 1 | 0 | 0 |
| question | 0 | 0 | 4 | 0 | 0 | 0 |
| debug | 0 | 1 | 0 | 5 | 0 | 0 |
| conversation | 0 | 0 | 1 | 0 | 4 | 0 |
| creative | 0 | 0 | 0 | 0 | 0 | 5 |