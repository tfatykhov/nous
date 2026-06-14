import asyncio, asyncpg, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DSN = "postgresql://nous:nous_dev_password@192.168.1.141:5432/nous"

Q_SUMMARY = """
SELECT
  count(*) AS rows,
  count(*) FILTER (WHERE cache_read IS NOT NULL) AS with_token_data,
  count(*) FILTER (WHERE cache_read > 0) AS with_cache_hit,
  coalesce(sum(cache_read),0) AS tot_cache_read,
  coalesce(sum(cache_creation),0) AS tot_cache_created,
  coalesce(sum(input_tokens_actual),0) AS tot_input_noncached,
  min(timestamp)::date AS first_day,
  max(timestamp)::date AS last_day
FROM nous_system.context_log;
"""
Q_BY_TYPE = """
SELECT call_type,
  count(*) AS n,
  count(*) FILTER (WHERE cache_read > 0) AS hits,
  coalesce(sum(cache_read),0) AS cr,
  coalesce(sum(cache_creation),0) AS cc,
  coalesce(sum(input_tokens_actual),0) AS inp
FROM nous_system.context_log
WHERE cache_read IS NOT NULL
GROUP BY call_type ORDER BY n DESC;
"""
Q_RECENT = """
SELECT call_type, frame_id, session_id, turn_number,
  input_tokens_actual AS inp, cache_read AS cr, cache_creation AS cc
FROM nous_system.context_log
WHERE cache_read IS NOT NULL
ORDER BY timestamp DESC LIMIT 30;
"""
async def main():
    c = await asyncpg.connect(DSN)
    try:
        r = await c.fetchrow(Q_SUMMARY)
        print("=== context_log SUMMARY ===")
        for k,v in r.items(): print(f"  {k}: {v}")
        cr=r["tot_cache_read"]; cc=r["tot_cache_created"]; inp=r["tot_input_noncached"]
        ti = cr+cc+inp
        if ti:
            print(f"  -> total_input(cr+cc+inp) = {ti}")
            print(f"  -> overall hit_rate (cache_read/total_input) = {cr/ti*100:.1f}%")
            print(f"  -> cache_creation share = {cc/ti*100:.1f}%   noncached share = {inp/ti*100:.1f}%")
        print("\n=== BY call_type (rows with token data) ===")
        for row in await c.fetch(Q_BY_TYPE):
            t=row["cr"]+row["cc"]+row["inp"]; hr=row["cr"]/t*100 if t else 0
            print(f"  {row['call_type']:<22} n={row['n']:<5} hits={row['hits']:<5} hit={hr:5.1f}%  cr={row['cr']:<9} cc={row['cc']:<8} inp={row['inp']}")
        print("\n=== RECENT 30 (newest first) ===")
        for row in await c.fetch(Q_RECENT):
            t=(row["cr"] or 0)+(row["cc"] or 0)+(row["inp"] or 0); hr=(row["cr"] or 0)/t*100 if t else 0
            print(f"  {row['call_type']:<18} {(row['frame_id'] or '-'):<11} {row['session_id'][:8]} t{row['turn_number']:<3} inp={row['inp']:<6} cr={row['cr']:<7} cc={row['cc']:<6} hit={hr:5.1f}%")
    finally:
        await c.close()
asyncio.run(main())
