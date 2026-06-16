import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
def agg(path):
    d=json.load(open(path,encoding="utf-8"))
    per_cat={}; alls=[]
    for cat,items in d.items():
        cs=[]
        for it in items:
            s=it.get("final_score") if cat=="event_ordering" and "final_score" in it else it.get("llm_judge_score")
            if s is not None: cs.append(float(s)); alls.append(float(s))
        if cs: per_cat[cat]=sum(cs)/len(cs)
    return (sum(alls)/len(alls) if alls else 0.0), per_cat
off_o,off=agg("reports/beam/_f044ab/off-eval.json")
on_o,on=agg("reports/beam/_f044ab/on-eval.json")
print(f"{'category':<28}{'OFF':>8}{'ON':>8}{'delta':>9}")
print("-"*53)
for c in sorted(set(off)|set(on)):
    o=off.get(c,0); n=on.get(c,0); d=n-o
    mark = ("  UP" if d>0.001 else "  DOWN" if d<-0.001 else "")
    print(f"{c:<28}{o:>8.3f}{n:>8.3f}{d:>+9.3f}{mark}")
print("-"*53)
print(f"{'OVERALL':<28}{off_o:>8.4f}{on_o:>8.4f}{on_o-off_o:>+9.4f}")
