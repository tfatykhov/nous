import asyncio, asyncpg, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DSN = "postgresql://nous:nous_dev_password@192.168.1.141:5432/nous"
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
