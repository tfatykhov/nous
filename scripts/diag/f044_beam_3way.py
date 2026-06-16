import json, sys, glob
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
def agg_arm(armdir):
    per_cat={}; alls=[]
    for f in sorted(glob.glob(f"{armdir}/conv*.json")):
        d=json.load(open(f,encoding="utf-8"))
        for cat,items in d.items():
            for it in items:
                s=it.get("final_score") if cat=="event_ordering" and "final_score" in it else it.get("llm_judge_score")
                if s is not None:
                    per_cat.setdefault(cat,[]).append(float(s)); alls.append(float(s))
    pc={k:sum(v)/len(v) for k,v in per_cat.items()}
    return (sum(alls)/len(alls) if alls else 0.0, len(alls), pc)
base="reports/beam/_f044ab"
o1,n1,c1=agg_arm(f"{base}/arm1_off")
o2,n2,c2=agg_arm(f"{base}/arm2_onself")
o3,n3,c3=agg_arm(f"{base}/arm3_oncontent")
cats=sorted(set(c1)|set(c2)|set(c3))
print(f"{'category':<26}{'OFF':>8}{'ON-self':>9}{'ON-cont':>9}{'cont-OFF':>10}")
print("-"*62)
for c in cats:
    a,b,d=c1.get(c,0),c2.get(c,0),c3.get(c,0)
    print(f"{c:<26}{a:>8.3f}{b:>9.3f}{d:>9.3f}{d-a:>+10.3f}")
print("-"*62)
print(f"{'OVERALL':<26}{o1:>8.4f}{o2:>9.4f}{o3:>9.4f}{o3-o1:>+10.4f}")
print(f"\nn per arm: OFF={n1} ON-self={n2} ON-content={n3}")
print(f"CONTRASTS:")
print(f"  Arm2-Arm1 (best-case effect)      = {o2-o1:+.4f}")
print(f"  Arm3-Arm1 (generalization effect) = {o3-o1:+.4f}")
print(f"  Arm2-Arm3 (best-case inflation)   = {o2-o3:+.4f}")
