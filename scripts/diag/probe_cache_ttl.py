import asyncio, asyncpg, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DSN = "postgresql://nous:nous_dev_password@192.168.1.141:5432/nous"

# For every call, compute gap since previous call in same session, and classify
Q = """
WITH ordered AS (
  SELECT session_id, timestamp, call_type, frame_id, turn_number,
         input_tokens_actual AS inp, cache_read AS cr, cache_creation AS cc,
         lag(timestamp) OVER (PARTITION BY session_id ORDER BY timestamp) AS prev_ts,
         lag(frame_id)  OVER (PARTITION BY session_id ORDER BY timestamp) AS prev_frame
  FROM nous_system.context_log
  WHERE cache_read IS NOT NULL
)
SELECT * FROM ordered
"""
async def main():
    c = await asyncpg.connect(DSN)
    try:
        rows = await c.fetch(Q)
        # Focus on "full creation" calls: cr==0 and cc>0
        full_create = [r for r in rows if (r["cr"] or 0)==0 and (r["cc"] or 0)>0]
        with_prev = [r for r in full_create if r["prev_ts"] is not None]
        print(f"total token rows: {len(rows)}")
        print(f"full-creation calls (cr=0, cc>0): {len(full_create)}")
        print(f"  ...of which have a previous call in session: {len(with_prev)}")
        gt5=lt5=0; frame_changed=0
        gaps=[]
        for r in with_prev:
            gap=(r["timestamp"]-r["prev_ts"]).total_seconds()
            gaps.append(gap)
            if gap>300: gt5+=1
            else: lt5+=1
            if r["prev_frame"]!=r["frame_id"]: frame_changed+=1
        print(f"\nFull-creation calls WITH prev call in same session:")
        print(f"  gap > 5min (TTL expiry, expected): {gt5}")
        print(f"  gap <=5min (cache still warm -> REAL bust): {lt5}")
        if lt5:
            print(f"  ...of those <=5min busts, frame changed vs prev call: {frame_changed}")
        # Show the <=5min busts in detail (the suspicious ones)
        susp=[r for r in with_prev if (r["timestamp"]-r["prev_ts"]).total_seconds()<=300]
        print(f"\n=== Suspicious <=5min full-creation busts (first 20) ===")
        for r in susp[:20]:
            gap=(r["timestamp"]-r["prev_ts"]).total_seconds()
            fc = "FRAME-CHG" if r["prev_frame"]!=r["frame_id"] else "same-frame"
            print(f"  {r['session_id'][:8]} t{r['turn_number']:<3} {r['call_type']:<8} {(r['frame_id'] or '-'):<11} gap={gap:6.0f}s cc={r['cc']:<7} prevframe={r['prev_frame']} {fc}")
    finally:
        await c.close()
asyncio.run(main())
