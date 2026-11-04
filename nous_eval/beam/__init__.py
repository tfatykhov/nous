"""BEAM benchmark harness (PR-M): ingest a BEAM conversation into a Nous agent
through the production write path, answer its probing questions through the
pre-turn context build or the recall_deep tool, and score with BEAM's own judge.

Run as ``python -m nous_eval.beam {ingest,answer,score} ...`` (see ``run.py``).
The corpus lives in the upstream checkout (``tools/beam``, gitignored); every
output goes to an ``--out-dir`` outside version control.

Modules:
    corpus   -- reads ``chats/<bucket>/<id>/chat.json`` + ``probing_questions.json``
    arms     -- Settings for a run: scratch DB, per-conversation agent, NOUS_* overrides
    harness  -- component wiring (mirrors nous/main.py), ingest, the two answer modes
    scoring  -- port of BEAM's evaluator (src/evaluation/compute_metrics.py)
    run      -- the CLI
"""
