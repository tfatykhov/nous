import asyncio, asyncpg, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DSN = "postgresql://nous:nous_dev_password@192.168.1.141:5432/nous"
Q = """
WITH ordered AS (
  SELECT session_id, timestamp, call_type, frame_id, turn_number,
         input_tokens_actual AS inp, cache_read AS cr, cache_creation AS cc,
         lag(timestamp) OVER (PARTITION BY session_id ORDER BY timestamp) AS prev_ts,
         lag(frame_id)  OVER (PARTITION BY session_id ORDER BY timestamp) AS prev_frame,
         lag(turn_number) OVER (PARTITION BY session_id ORDER BY timestamp) AS prev_turn
  FROM nous_system.context_log WHERE cache_read IS NOT NULL
)
SELECT * FROM ordered
"""
async def main():
    c = await asyncpg.connect(DSN)
    try:
        rows = await c.fetch(Q)
        # within-window (<=5min) calls that have a prev call
        warm = [r for r in rows if r["prev_ts"] is not None and (r["timestamp"]-r["prev_ts"]).total_seconds()<=300]
        full = [r for r in warm if (r["cr"] or 0)==0 and (r["cc"] or 0)>0]
        fc_full = [r for r in full if r["prev_frame"]!=r["frame_id"]]
        sf_full = [r for r in full if r["prev_frame"]==r["frame_id"]]
        total_cc = sum((r["cc"] or 0) for r in rows)
        warm_cc = sum((r["cc"] or 0) for r in warm)
        fc_full_cc = sum((r["cc"] or 0) for r in fc_full)
        sf_full_cc = sum((r["cc"] or 0) for r in sf_full)
        print(f"TOTAL cache_creation (all logged): {total_cc:,}")
        print(f"warm (<=5min, prev exists) calls: {len(warm)}  cc={warm_cc:,}")
        print(f"  full-prefix busts within warm window: {len(full)}  cc={sum((r['cc'] or 0) for r in full):,}")
        print(f"    - FRAME-CHANGED busts: {len(fc_full)}  cc={fc_full_cc:,}  <-- AVOIDABLE via stable tools")
        print(f"    - same-frame busts:    {len(sf_full)}  cc={sf_full_cc:,}")
        print(f"  => avoidable share of TOTAL cc (frame-change busts): {fc_full_cc/total_cc*100:.1f}%")
        # If those were cache reads instead of creates: creation ~1.25x base, read ~0.1x base.
        # Savings factor ~ (1.25-0.1)/1.25 = 92% of those tokens' cost avoided, plus they'd read at 0.1x.
        print(f"  => ~{fc_full_cc:,} tokens/month re-created at 1.25x that could be 0.1x reads")
        # same-frame busts: are these explained by learn events (static/semi drift)?
        print("\n=== same-frame warm full busts (cause = static/semi drift, not tools) ===")
        for r in sf_full:
            gap=(r["timestamp"]-r["prev_ts"]).total_seconds()
            print(f"  {r['session_id'][:8]} t{r['prev_turn']}->t{r['turn_number']} {r['frame_id']:<10} gap={gap:.0f}s cc={r['cc']}")
        # Frame-change rate across turn boundaries (first call of each turn)
        # Approximate: count distinct (session,turn) and how often frame differs from prev turn's frame
    finally:
        await c.close()
asyncio.run(main())
