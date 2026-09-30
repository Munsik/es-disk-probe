#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run the analyzer and the shell summary on synthetic bundles, in both report languages.

  python3 tests/run_tests.py            # all scenarios, all checks
  python3 tests/run_tests.py --dump DIR # also write <scenario>.ko.html, <scenario>.en.html and summary.json to DIR

Checks:
  - expectations.py (finding ids, owners, verdict class) in ko and en
  - ko and en produce the same findings (severity, id, owner) and the same verdict
  - the shell summary (bash + awk) gives the same verdict as the HTML report, in both languages
  - i18n catalogs (tests/i18n_check.py) and the writing rules on user docs
  - parsers keep reading known output variants (EBS stats, cluster settings, per-index stats)
"""
import json, os, re, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import make_bundle
import es_disk_render as R
import i18n_check

LANGS = ("ko", "en")
DOCS = ("README.md", "README.ko.md", "GUARDLINE.md", "GUARDLINE.ko.md", "docs/UPDATE_NOTES.md")


def shell(bundle, lang):
    sh = os.path.join(ROOT, "es_disk_summary.sh")
    return subprocess.run(["bash", sh, bundle, "--lang", lang], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True).stdout


def line_after(text, template):
    """the value part of the line that starts with template's text before {1}"""
    prefix = template.split("{1}")[0]
    for l in text.split("\n"):
        if l.startswith(prefix):
            return l[len(prefix):]
    return None


def main():
    dump = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--dump" else None
    work = dump or tempfile.mkdtemp()
    cats = {l: R.load_catalog(l) for l in LANGS}
    guess_ids = {k for k, v in cats["ko"].items() if k.startswith("r.") and "추정으로 판정" in v}
    out = {l: {} for l in LANGS}
    bundles = {name: make_bundle.build(name, os.path.join(work, name)) for name in make_bundle.SCEN}
    for lang in LANGS:
        R.set_lang(lang)
        for name, b in bundles.items():
            res = R.analyze(b)
            R.render(res, os.path.join(work, "{}.{}.html".format(name, lang)))
            html = open(os.path.join(work, "{}.{}.html".format(name, lang)), encoding="utf-8").read()
            text = shell(b, lang)
            if re.search(r"(?<![\w./])(s\.[a-z_]+\.|r\.\d{4}\b)", text):
                print("FAIL {} [{}]: raw catalog key in the shell summary".format(name, lang)); sys.exit(1)
            if any(d in html + text for d in i18n_check.DASHES) or (lang == "en" and i18n_check.HANGUL.search(html + text)):
                print("FAIL {} [{}]: em/en dash or Hangul in output".format(name, lang)); sys.exit(1)
            out[lang][name] = {
                "platform": res.get("platform"), "verdict_class": res["verdict"][0], "verdict": res["verdict"][1],
                "findings": [{"sev": f.sev, "id": f.id, "owner_id": f.owner_id, "title": str(f.title)} for f in res["findings"]],
                "shell_verdict": line_after(text, cats[lang]["s.verdict"]), "shell_text": text}
    if dump:
        with open(os.path.join(dump, "summary.json"), "w") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(os.path.join(dump, "summary.json"))
        return

    import expectations
    fails = []
    for lang in LANGS:
        fails += expectations.check(out[lang], lang)
    for name in bundles:
        a, b = out["ko"][name], out["en"][name]
        key = lambda r: sorted((f["sev"], f["id"] or "", f["owner_id"] or "") for f in r["findings"])
        if key(a) != key(b):
            fails.append("{}: ko and en findings differ".format(name))
        if a["verdict_class"] != b["verdict_class"]:
            fails.append("{}: ko and en verdicts differ".format(name))
        for lang in LANGS:
            r = out[lang][name]
            if r["shell_verdict"] != r["verdict"]:
                fails.append("{} [{}]: shell verdict '{}' != HTML '{}'".format(name, lang, r["shell_verdict"], r["verdict"]))
            # media basis: the shell marks it as estimated exactly when the HTML report has the "estimated" finding
            basis = line_after(r["shell_text"], cats[lang]["s.basis"]) or ""
            if (cats[lang]["s.basis.guess"] in basis) != any(f["id"] in guess_ids for f in r["findings"]):
                fails.append("{} [{}]: media estimate differs between shell and HTML".format(name, lang))

    # parsers: output variants must give the same numbers
    for a, b in ((make_bundle.EBS_START, make_bundle.EBS_END), (make_bundle.EBS_V2_START, make_bundle.EBS_V2_END),
                 (make_bundle.EBS_V3_START, make_bundle.EBS_V3_END)):
        x, y = R.parse_ebs_stats(a).get("nvme1n1", {}), R.parse_ebs_stats(b).get("nvme1n1", {})
        got = {k: y.get(k, 0) - x.get(k, 0) for k in ("vol_iops", "vol_tp", "inst_iops", "inst_tp")}
        if got != {"vol_iops": 30000000, "vol_tp": 0, "inst_iops": 0, "inst_tp": 300000}:
            fails.append("EBS stats variant parsed wrong: {}".format(got))
    for name in ("aws_ebs_throttle", "aws_ebs_v2", "aws_ebs_v3"):
        for lang in LANGS:
            t = out[lang][name]["shell_text"]
            if not any("30.0" in l and "nvme1n1" in l for l in t.split("\n")):
                fails.append("{} [{}]: shell summary lacks the 30.0 s EBS limit line".format(name, lang))
    # _cluster/settings: nested (current collector) and flat (old bundles) must read the same.
    # A real ES returns {} for flat_settings=true with filter_path (seen on ES 8.19)
    nested = {"defaults": {"cluster": {"routing": {"allocation": {"disk": {"watermark": {
        "high": "90%", "high.max_headroom": "150gb"}}}}}}, "persistent": {}}
    flat = {"defaults": {"cluster.routing.allocation.disk.watermark.high": "90%",
                         "cluster.routing.allocation.disk.watermark.high.max_headroom": "150gb"}}
    for cs_ in (nested, flat):
        lim_, _ = R.wm_high(R.flat_settings(cs_), 4 * 1024 ** 4)
        if not lim_ or abs(lim_ - 96.34) > 0.1:
            fails.append("cluster settings parsed wrong ({}): {}".format("nested" if cs_ is nested else "flat", lim_))
    # per-index stats: a real ES nests them under nodes.<id>.indices.indices.<index>; the old shape must still read
    for shape in ("nested", "flat"):
        d_ = tempfile.mkdtemp()
        for fn, n_ in (("es_idx_start.json", 1000), ("es_idx_end.json", 91000)):
            ix = {"loadtest": {"indexing": {"index_total": n_, "index_time_in_millis": n_ // 10},
                               "merges": {"total_size_in_bytes": n_ * 100}, "store": {"size_in_bytes": n_ * 600},
                               "segments": {"count": 12}, "search": {"query_total": 0}, "refresh": {"total": 5}}}
            with open(os.path.join(d_, fn), "w") as fh:
                json.dump({"nodes": {"n1": {"indices": {"indices": ix} if shape == "nested" else ix}}}, fh)
        if not R.analyze_local_indices(d_, None):
            fails.append("per-index stats not parsed ({} shape)".format(shape))

    # cluster-only report (es_cluster_probe.sh bundle): same findings in both languages, skewed node found
    cdir = make_bundle.build_cluster(os.path.join(work, "cluster"))
    cl = {}
    for lang in LANGS:
        R.set_lang(lang)
        F = []
        R.analyze_cluster(cdir, lambda *a: F.append(R.Finding(*a)), R.LAT_TH["allflash"])
        cl[lang] = sorted((f.sev, f.id) for f in F)
        R.render_cluster_only(cdir, os.path.join(work, "cluster.{}.html".format(lang)))
        if not any(f.id == "r.0043" and "es-hot-02" in str(f.title) for f in F):
            fails.append("cluster [{}]: skewed node es-hot-02 not reported".format(lang))
    if cl["ko"] != cl["en"]:
        fails.append("cluster: ko and en findings differ")

    # i18n catalogs and writing rules on user docs
    ko, _ = i18n_check.load(os.path.join(ROOT, "i18n", "ko.txt"))
    en, _ = i18n_check.load(os.path.join(ROOT, "i18n", "en.txt"))
    fails += ["i18n: " + e for e in i18n_check.check(ko, en)]
    for doc in DOCS:
        p = os.path.join(ROOT, doc)
        if not os.path.isfile(p):
            continue
        text = open(p, encoding="utf-8").read()
        low = text.lower()
        if any(d in text for d in i18n_check.DASHES):
            fails.append("{}: em/en dash".format(doc))
        korean = ".ko." in doc
        banned = i18n_check.BANNED_KO if korean else i18n_check.BANNED_EN
        if not korean and i18n_check.HANGUL.search(text.replace("한국어", "")):
            fails.append("{}: Hangul in an English doc".format(doc))
        for w in banned:
            if w in low:
                fails.append("{}: avoid '{}'".format(doc, w))

    for f in fails:
        print("FAIL", f)
    print("{} scenarios x {} languages, {} failures".format(len(bundles), len(LANGS), len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
