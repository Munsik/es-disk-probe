#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Catalog checks for i18n/ko.txt and i18n/en.txt (also used by run_tests.py).

  python3 tests/i18n_check.py                      # check the full catalogs
  python3 tests/i18n_check.py --chunk en_part.txt  # check a partial English file against ko.txt

Checks:
  - same keys in ko and en
  - same placeholders: Python format fields ({}, {0}, {name}, {:.1f}) and shell slots ({1}..{5})
  - same HTML tags (by tag name and count)
  - leading and trailing spaces kept (they glue fragments together in code)
  - no Hangul in en, no em dash or en dash anywhere, no phrases listed in docs/STYLE.md
"""
import os, re, string, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

HANGUL = re.compile('[가-힣ㄱ-ㆎ]')
DASHES = ("—", "–")
BANNED_EN = ["it's worth noting", "it is worth noting", "crucial", "robust", "seamless", "leverage", "delve",
             "comprehensive", "furthermore", "moreover", "utilize", "in order to", "plays a key role"]
BANNED_KO = ["하는 것이 중요", "살펴보겠", "다양한", "효과적으로", "를 통해", "을 통해"]
TAG = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b')


def load(path):
    from es_disk_render import _unesc
    cat, order = {}, []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            k, sep, v = line.partition(" = ")
            v = v.strip()
            if not sep or len(v) < 2 or v[0] != '"' or v[-1] != '"':
                raise ValueError("{}:{}: bad line: {}".format(path, n, line[:80]))
            cat[k.strip()] = _unesc(v[1:-1])
            order.append(k.strip())
    return cat, order


def fields(key, s):
    """placeholder signature of a string"""
    if key.startswith(("s.", "c.", "p.")):
        return ("slots", tuple(sorted(set(re.findall(r'\{[1-9]\}', s)))))
    out = []
    try:
        auto = 0
        for _, name, spec, conv in string.Formatter().parse(s):
            if name is None:
                continue
            if name == "":
                name = str(auto); auto += 1
            out.append((name, spec or "", conv or ""))
    except ValueError as e:
        return ("error", str(e))
    return ("fmt", tuple(sorted(out)))


def check(ko, en, keys=None):
    errs = []
    keys = list(keys if keys is not None else ko.keys())
    if keys is None or set(en) - set(ko):
        for k in sorted(set(en) - set(ko)):
            errs.append("{}: key not in ko.txt".format(k))
    for k in keys:
        if k not in en:
            errs.append("{}: missing in en".format(k)); continue
        a, b = ko[k], en[k]
        fa, fb = fields(k, a), fields(k, b)
        if fb[0] == "error":
            errs.append("{}: bad format string: {}".format(k, fb[1]))
        elif fa != fb:
            errs.append("{}: placeholders differ ko={} en={}".format(k, fa[1], fb[1]))
        if Counter(t.lower() for t in TAG.findall(a)) != Counter(t.lower() for t in TAG.findall(b)):
            errs.append("{}: HTML tags differ".format(k))
        for side, x, y in (("leading", a[:1].isspace(), b[:1].isspace()), ("trailing", a[-1:].isspace(), b[-1:].isspace())):
            if x != y:
                errs.append("{}: {} space differs".format(k, side))
        if HANGUL.search(b):
            errs.append("{}: Hangul in en".format(k))
    for name, cat, banned in (("ko", {k: ko[k] for k in keys}, BANNED_KO), ("en", {k: en[k] for k in keys if k in en}, BANNED_EN)):
        for k, v in cat.items():
            if any(d in v for d in DASHES):
                errs.append("{} {}: em/en dash".format(name, k))
            low = v.lower()
            for w in banned:
                if w in low:
                    errs.append("{} {}: avoid '{}'".format(name, k, w))
    return errs


def main():
    ko, _ = load(os.path.join(ROOT, "i18n", "ko.txt"))
    if len(sys.argv) == 3 and sys.argv[1] == "--chunk":
        en, order = load(sys.argv[2])
        errs = check(ko, en, keys=order)
    else:
        en, _ = load(os.path.join(ROOT, "i18n", "en.txt"))
        errs = check(ko, en)
    for e in errs:
        print(e)
    print("{} problems".format(len(errs)))
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
