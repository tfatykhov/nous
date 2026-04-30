# Frame selection eval — 30 scenarios

- accuracy: **25/30 (83.3%)**
- SUT: `nous.cognitive.frames.FrameEngine.select`
- 6 default frames seeded under `frames-eval-agent`.

## Per-frame

| frame | n | correct | accuracy |
|---|---:|---:|---:|
| decision | 5 | 5 | 100% |
| task | 5 | 4 | 80% |
| question | 4 | 4 | 100% |
| debug | 6 | 4 | 67% |
| conversation | 5 | 4 | 80% |
| creative | 5 | 4 | 80% |

## Failures

| expected | actual | input |
|---|---|---|
| task | debug | `Fix the failing CI pipeline.` |
| debug | task | `Why did the build fail this morning?` |
| debug | conversation | `The compaction step is broken.` |
| conversation | question | `How are you doing today?` |
| creative | question | `What if we redesigned from scratch?` |

## Confusion matrix

| expected \ actual | decision | task | question | debug | conversation | creative |
|---|---|---|---|---|---|---|
| decision | 5 | 0 | 0 | 0 | 0 | 0 |
| task | 0 | 4 | 0 | 1 | 0 | 0 |
| question | 0 | 0 | 4 | 0 | 0 | 0 |
| debug | 0 | 1 | 0 | 4 | 1 | 0 |
| conversation | 0 | 0 | 1 | 0 | 4 | 0 |
| creative | 0 | 0 | 1 | 0 | 0 | 4 |