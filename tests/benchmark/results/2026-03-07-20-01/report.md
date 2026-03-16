# Context Benchmark Report

**Date:** 2026-03-07 20:25 UTC


## LONG

| Config | Recall | Halluc. | Honest Unc. | Avg Score | In Tokens | Out Tokens |
|--------|--------|---------|-------------|-----------|-----------|------------|
| new-700k | 56% | 22% | 0% | 0.67 | 2,615,757 | 65,275 |

### Questions (long)

| Question | Dist | new-700k |
|----------|------|---|
| What is the exact SECRET_KEY value from middleware.py?... | 27 | ✅ 1.0 |
| List all 4 roles from the Role enum in permissions.py.... | 27 | ✅ 1.0 |
| What is the MAX_POOL_SIZE in database.py?... | 27 | ✅ 1.0 |
| What is the CONNECTION_RETRY_DELAY value in database.py?... | 28 | ? 0.5 |
| What is the MAX_CONCURRENT_TASKS value in scheduler.py?... | 26 | ? 0.5 |
| What bcrypt_rounds value is configured in AppConfig?... | 29 | ✅ 1.0 |
| Show me the exact get_connection context manager implementation from database.py... | 31 | ❌ 0.0 |
| Show the complete Role enum with all values from permissions.py.... | 33 | ✅ 1.0 |
| What is the exact invalidate_pattern implementation in cache.py?... | 31 | ❌ 0.0 |