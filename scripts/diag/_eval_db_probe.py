import asyncio, asyncpg
DSN = "postgresql://nous:nous_eval@localhost:5433/nous_eval_prod"
async def m():
    try:
        c = await asyncpg.connect(DSN)
    except Exception as e:
        print("EVAL DB CONNECT FAIL:", repr(e)); return
    for q, l in [
        ("SELECT count(*) FROM heart.facts WHERE agent_id='nous-default'", "facts"),
        ("SELECT count(*) FROM brain.graph_edges WHERE agent_id='nous-default'", "edges"),
        ("SELECT count(*) FROM brain.graph_edges WHERE agent_id='nous-default' AND consolidation_state='consolidated'", "consolidated"),
    ]:
        try:
            print(f"  {l}:", await c.fetchval(q))
        except Exception as e:
            print(f"  {l}: ERR {e}")
    await c.close()
asyncio.run(m())
