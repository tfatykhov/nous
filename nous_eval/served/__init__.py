"""Served-precision eval: what Nous puts in front of the model, judged for relevance.

Two entry points share one library: ``recall`` (the recall_deep tool) and ``preturn`` (the
pre-turn context build). Each run is one arm (a named set of Settings overrides on a base) on a
fresh clone of a prod-shaped database; ``compare`` pairs two runs. See README.md in this folder.
Developer-only, like the rest of nous_eval: the prod image copies ``nous/`` only.
"""
