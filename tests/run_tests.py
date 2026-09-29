#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
합성 번들로 분석기를 돌려 판정 결과를 확인한다.

  python3 tests/run_tests.py            # 전체 시나리오 실행, 기대값 검사
  python3 tests/run_tests.py --dump DIR # 시나리오별 판정 목록(JSON)과 HTML 을 DIR 에 저장
"""
import json, os, re, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import make_bundle
import es_disk_render as R

def run(name, work):
    b = make_bundle.build(name, os.path.join(work, name))
    res = R.analyze(b)
    R.render(res, os.path.join(work, name + ".html"))
    res["_shell"], res["_shell_text"] = shell_verdict(b)
    return res

def shell_verdict(bundle):
    """es_disk_summary.sh(bash+awk)의 판정 문구와 전체 출력. 셸 요약과 HTML 판정이 같아야 한다."""
    sh = os.path.join(os.path.dirname(HERE), "es_disk_summary.sh")
    out = subprocess.run(["bash", sh, bundle], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True).stdout
    m = re.search(r"^판정: (.*)$", out, re.M)
    return (m.group(1) if m else "(판정 없음) " + out[-200:]), out

def summary(res):
    return [[f.sev, f.dim, f.owner, f.title] for f in res["findings"]]

if __name__ == "__main__":
    dump = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--dump" else None
    work = dump or tempfile.mkdtemp()
    out = {}
    for name in make_bundle.SCEN:
        res = run(name, work)
        out[name] = {"verdict": res["verdict"][1], "platform": res.get("platform"), "findings": summary(res),
                     "shell_verdict": res["_shell"], "shell_text": res["_shell_text"]}
    if dump:
        with open(os.path.join(dump, "summary.json"), "w") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(os.path.join(dump, "summary.json"))
    else:
        import expectations
        fails = expectations.check(out)
        # 출력 형식 변형: 같은 값이 같은 숫자로 읽혀야 한다 (Python 파서)
        for a, b in ((make_bundle.EBS_START, make_bundle.EBS_END), (make_bundle.EBS_V2_START, make_bundle.EBS_V2_END),
                     (make_bundle.EBS_V3_START, make_bundle.EBS_V3_END)):
            x, y = R.parse_ebs_stats(a).get("nvme1n1", {}), R.parse_ebs_stats(b).get("nvme1n1", {})
            got = {k: y.get(k, 0) - x.get(k, 0) for k in ("vol_iops", "vol_tp", "inst_iops", "inst_tp")}
            if got != {"vol_iops": 30000000, "vol_tp": 0, "inst_iops": 0, "inst_tp": 300000}:
                fails.append("EBS 통계 형식 변형 해석 오류: {}".format(got))
        # 셸 요약도 같은 초 단위 값을 내야 한다
        for name in ("aws_ebs_throttle", "aws_ebs_v2", "aws_ebs_v3"):
            if "한도 초과 30.0초" not in out[name].get("shell_text", ""):
                fails.append("{}: 셸 요약에 EBS 한도 초과 30.0초가 없음".format(name))
        # 매체 판정 근거: HTML 이 추정이 아니라고 하면 셸도 추정이면 안 된다
        for name, r in out.items():
            html_guess = any("추정으로 판정" in f[3] for f in r["findings"])
            m_ = re.search(r"^판정 기준 .*$", r.get("shell_text", ""), re.M)
            if m_ and ("(추정)" in m_.group(0)) != html_guess:
                fails.append("{}: 매체 추정 여부가 다름 (셸 '{}', HTML 추정 {})".format(name, m_.group(0), html_guess))
        for name, r in out.items():
            if r["shell_verdict"] != r["verdict"]:
                fails.append("{}: 셸 요약 판정 '{}' ≠ HTML '{}'".format(name, r["shell_verdict"], r["verdict"]))
        for f in fails:
            print("FAIL", f)
        print("{} scenarios, {} failures".format(len(out), len(fails)))
        sys.exit(1 if fails else 0)
