#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
합성 번들로 분석기를 돌려 판정 결과를 확인한다.

  python3 tests/run_tests.py            # 전체 시나리오 실행, 기대값 검사
  python3 tests/run_tests.py --dump DIR # 시나리오별 판정 목록(JSON)과 HTML 을 DIR 에 저장
"""
import json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import make_bundle
import es_disk_render as R

def run(name, work):
    b = make_bundle.build(name, os.path.join(work, name))
    res = R.analyze(b)
    R.render(res, os.path.join(work, name + ".html"))
    return res

def summary(res):
    return [[f.sev, f.dim, f.owner, f.title] for f in res["findings"]]

if __name__ == "__main__":
    dump = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--dump" else None
    work = dump or tempfile.mkdtemp()
    out = {}
    for name in make_bundle.SCEN:
        res = run(name, work)
        out[name] = {"verdict": res["verdict"][1], "platform": res.get("platform"), "findings": summary(res)}
    if dump:
        with open(os.path.join(dump, "summary.json"), "w") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(os.path.join(dump, "summary.json"))
    else:
        import expectations
        fails = expectations.check(out)
        for f in fails:
            print("FAIL", f)
        print("{} scenarios, {} failures".format(len(out), len(fails)))
        sys.exit(1 if fails else 0)
