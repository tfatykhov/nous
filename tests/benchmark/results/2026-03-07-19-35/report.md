# Context Benchmark Report

**Date:** 2026-03-07 19:42 UTC


## MEDIUM

| Config | Recall | Halluc. | Honest Unc. | Avg Score | In Tokens | Out Tokens |
|--------|--------|---------|-------------|-----------|-----------|------------|
| new-700k | 100% | 0% | 0% | 1.00 | 318,523 | 16,591 |

### Questions (medium)

| Question | Dist | new-700k |
|----------|------|---|
| What is the SECRET_KEY value in middleware.py?... | 12 | ✅ 1.0 |
| What class is defined in database.py and what is its MAX_POOL_SIZE?... | 11 | ✅ 1.0 |
| What is the STATEMENT_TIMEOUT value in database.py?... | 12 | ✅ 1.0 |
| What is the cache key prefix defined in cache.py?... | 10 | ✅ 1.0 |
| What is the TASK_TIMEOUT_SECONDS value in scheduler.py?... | 10 | ✅ 1.0 |
| What is the default port in AppConfig?... | 13 | ✅ 1.0 |
| Show me the exact implementation of the hash_password method from middleware.py.... | 18 | ✅ 1.0 |
| List all the DEFAULT_PERMISSIONS entries from permissions.py with their exact re... | 18 | ✅ 1.0 |