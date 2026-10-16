import asyncio, asyncpg, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DSN = os.environ["PROD_DSN"]
async def main():
    c = await asyncpg.connect(DSN)
    try:
        rows = await c.fetch("""SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='nous_system' AND table_name='context_log' ORDER BY ordinal_position""")
        print("=== nous_system.context_log columns ===")
        for r in rows:
            print(f"  {r['column_name']:<28} {r['data_type']}")
    finally:
        await c.close()
asyncio.run(main())
