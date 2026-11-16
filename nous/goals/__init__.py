"""F114 goals: a goal the owner approves is worked on in background sessions, each a continuation chain.

Behind ``NOUS_GOALS_ENABLED`` (default off). Outside this package only the goal routes import it
(``nous/api/goal_routes.py`` and their mount in ``nous/api/rest.py``).
"""
