#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
es_disk_render.py (v0.9.0)
es_disk_collect.sh 가 만든 번들(디렉터리 또는 .tar.gz)을 읽어
지표 계산 → 판정 → HTML 리포트를 생성합니다.

- Python 3.6+ 표준 라이브러리만 사용 (RHEL8 platform-python 호환)
- 서버가 아닌 PC에서 실행해도 됩니다 (번들만 옮기면 됨)

사용:
  python3 es_disk_render.py <번들 디렉터리 | 번들.tar.gz> [-o report.html]
                            [--storage allflash|hybrid] [--bench <es_disk_bench 결과 디렉터리>]
"""
import argparse, html, json, os, re, sys, tarfile, tempfile, datetime

TOOL_VERSION = "0.9.0"

# ─────────────────────────────────────────────────────────────────────────────
# 기준값 (출처를 함께 표기 — 리포트에도 그대로 노출)
# ─────────────────────────────────────────────────────────────────────────────
# Guest에서 관측한 디스크 응답시간(ms). Broadcom vSAN KB 389082(All-Flash <5ms, Hybrid <20ms 정상)
# 를 기준으로 삼고 단계는 실무 기준으로 나눔. Elastic이 공식 수치를 제시하지는 않음.
LAT_TH = {
    "allflash": {"caution": 5.0, "warn": 10.0, "crit": 20.0},
    "hybrid":   {"caution": 10.0, "warn": 20.0, "crit": 30.0},
}
MIN_IOS_PER_INTERVAL = 20      # 이보다 I/O가 적은 구간은 응답시간 통계에서 제외 (소수 I/O 노이즈 방지)
LOW_LOAD_IOPS = 50             # p95 IOPS 가 이보다 낮고
LOW_LOAD_MBPS = 5.0            # p95 처리량도 이보다 낮으면 "부하 부족 → 한계 판정 보류"

SEV_ORDER = {"ok": 0, "na": 0, "info": 1, "caution": 2, "warn": 3, "crit": 4}
SEV_LABEL = {"ok": "정상", "na": "미수집", "info": "참고", "caution": "주의", "warn": "경고", "crit": "위험"}

# ─────────────────────────────────────────────────────────────────────────────
# 입력 로딩
# ─────────────────────────────────────────────────────────────────────────────
def open_bundle(path):
    if os.path.isdir(path):
        return path
    if tarfile.is_tarfile(path):
        tmp = tempfile.mkdtemp(prefix="esdisk_")
        with tarfile.open(path) as t:
            for m in t.getmembers():               # 경로 탈출 방지
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    continue
                t.extract(m, tmp)
        subs = [os.path.join(tmp, d) for d in os.listdir(tmp)]
        subs = [d for d in subs if os.path.isdir(d)]
        return subs[0] if subs else tmp
    sys.exit("번들을 찾을 수 없습니다: " + path)

def rd(base, name, default=""):
    p = os.path.join(base, name)
    try:
        with open(p, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return default

def rjson(base, name):
    t = rd(base, name)
    try:
        return json.loads(t) if t.strip() else None
    except Exception:
        return None

def kv(text, sep="="):
    d = {}
    for line in text.splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            d[k.strip()] = v.strip()
    return d

def num(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def dig(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d

# ─────────────────────────────────────────────────────────────────────────────
# 통계
# ─────────────────────────────────────────────────────────────────────────────
def pctl(vals, p):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)

def avg(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None

def vmax(vals):
    v = [x for x in vals if x is not None]
    return max(v) if v else None

def fmt(x, nd=1, unit=""):
    if x is None:
        return "–"
    if isinstance(x, float) and abs(x) >= 1000:
        s = "{:,.0f}".format(x)
    else:
        s = ("{:,." + str(nd) + "f}").format(x)
    return s + unit

def sev_max(*sevs):
    s = [x for x in sevs if x]
    return max(s, key=lambda x: SEV_ORDER.get(x, 0)) if s else "ok"

def grade(v, th):
    if v is None:
        return "na"
    if v >= th["crit"]:
        return "crit"
    if v >= th["warn"]:
        return "warn"
    if v >= th["caution"]:
        return "caution"
    return "ok"

# ─────────────────────────────────────────────────────────────────────────────
# 정적 구성 파싱
# ─────────────────────────────────────────────────────────────────────────────
class Topo(object):
    def __init__(self, sysfs_text):
        self.attr, self.slaves, self.parts, self.scsihost, self.hostdrv = {}, {}, {}, {}, {}
        for line in sysfs_text.splitlines():
            p = line.split("|")
            if p[0] == "ATTR" and len(p) >= 4:
                self.attr.setdefault(p[1], {})[p[2]] = "|".join(p[3:])
            elif p[0] == "SLAVE" and len(p) == 3:
                self.slaves.setdefault(p[1], []).append(p[2])
            elif p[0] == "PART" and len(p) == 4:
                self.parts[p[1]] = (p[2], int(num(p[3], 0)))
            elif p[0] == "SCSIHOST" and len(p) == 3:
                self.scsihost[p[1]] = p[2]
            elif p[0] == "HOSTDRV" and len(p) == 3:
                self.hostdrv[p[1]] = p[2]
        self.dmname = {a.get("dm/name"): d for d, a in self.attr.items() if a.get("dm/name")}

    def kname(self, src):
        if src.startswith("/dev/mapper/"):
            return self.dmname.get(src[len("/dev/mapper/"):])
        if src.startswith("/dev/"):
            k = src[5:]
            if "/" in k:                       # /dev/vg/lv 형태
                k = self.dmname.get(k.replace("-", "--").replace("/", "-"))
            return k
        return None

    def physical(self, k, depth=0):
        if k is None or depth > 8:
            return []
        if k in self.parts:
            return self.physical(self.parts[k][0], depth + 1)
        if self.slaves.get(k):
            out = []
            for s in self.slaves[k]:
                out += self.physical(s, depth + 1)
            return sorted(set(out))
        return [k]

    def whole(self, k):
        return self.parts[k][0] if k in self.parts else k

def parse_mounts(text):
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4:
            mnt = p[1].replace("\\040", " ")
            out.append({"src": p[0], "mnt": mnt, "fs": p[2], "opts": p[3]})
    return out

def mount_for(path, mounts):
    best = None
    for m in mounts:
        mp = m["mnt"].rstrip("/") or "/"
        if path == mp or path.startswith(mp + "/") or mp == "/":
            if best is None or len(mp) > len(best["mnt"].rstrip("/") or "/"):
                best = m
    return best

def yml_data_paths(text):
    paths, in_path, in_data = [], False, False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r'^\s*path\.data\s*:\s*(.*)$', line)
        if m:
            v = m.group(1).strip()
            if v.startswith("["):
                paths += [x.strip().strip("'\"") for x in v.strip("[]").split(",") if x.strip()]
            elif v:
                paths.append(v.strip("'\""))
            else:
                in_data = True
            continue
        if re.match(r'^path\s*:\s*$', line):
            in_path = True; continue
        if in_path:
            m = re.match(r'^\s+data\s*:\s*(.*)$', line)
            if m:
                v = m.group(1).strip()
                if v:
                    paths += [x.strip().strip("'\"") for x in v.strip("[]").split(",") if x.strip()]
                else:
                    in_data = True
                continue
        if in_data:
            m = re.match(r'^\s*-\s*(\S+)', line)
            if m:
                paths.append(m.group(1).strip("'\"")); continue
            in_data = False
        if line and not line.startswith(" "):
            in_path = False
    return paths

# ─────────────────────────────────────────────────────────────────────────────
# 샘플 파싱
# ─────────────────────────────────────────────────────────────────────────────
def parse_samples(text, es_pid):
    snaps, cur, sec = [], None, None
    for line in text.splitlines():
        if line.startswith("#T "):
            cur = {"t": num(line[3:].strip(), 0.0), "sec": {}}
            snaps.append(cur); sec = None; continue
        if cur is None:
            continue
        if line.startswith("==> "):
            sec = line[4:].strip()
            cur["sec"][sec] = []; continue
        if sec:
            cur["sec"][sec].append(line)
    out = []
    for s in snaps:
        d = {"t": s["t"], "disk": {}, "net": {}}
        for name, lines in s["sec"].items():
            if name == "/proc/diskstats":
                for l in lines:
                    p = l.split()
                    if len(p) >= 14:
                        d["disk"][p[2]] = [int(x) for x in p[3:]]
            elif name == "/proc/stat":
                for l in lines:
                    p = l.split()
                    if p and p[0] == "cpu":
                        d["cpu"] = [int(x) for x in p[1:]]
                    elif p and p[0] == "procs_blocked":
                        d["blocked"] = int(p[1])
            elif name == "/proc/vmstat":
                d["vm"] = {l.split()[0]: int(l.split()[1]) for l in lines if len(l.split()) == 2}
            elif name == "/proc/meminfo":
                d["mem"] = {l.split(":")[0]: int(l.split()[1]) for l in lines if ":" in l}
            elif name == "/proc/net/dev":
                for l in lines:
                    if ":" in l:
                        ifn, rest = l.split(":", 1)
                        v = rest.split()
                        if len(v) >= 16:
                            d["net"][ifn.strip()] = [int(x) for x in v[:16]]
            elif name == "/proc/net/snmp":
                if len(lines) >= 2:
                    h, v = lines[0].split()[1:], lines[1].split()[1:]
                    d["tcp"] = dict(zip(h, [int(x) for x in v]))
            elif name.startswith("/proc/pressure/"):
                key = "psi_" + name.rsplit("/", 1)[1]
                for l in lines:
                    m = re.match(r'^(some|full)\s.*total=(\d+)', l)
                    if m:
                        d.setdefault(key, {})[m.group(1)] = int(m.group(2))
            elif name == "/proc/%s/io" % es_pid:
                d["pio"] = {l.split(":")[0]: int(l.split(":")[1]) for l in lines if ":" in l}
            elif name == "/proc/%s/stat" % es_pid:
                if lines:
                    post = lines[0].rsplit(") ", 1)[-1].split()
                    if len(post) > 9:
                        d["majflt"] = int(post[9])
            elif name == "DSTATE":
                if lines:
                    p = lines[0].split()
                    d["dstate"] = (int(p[0]), int(p[1]))
        out.append(d)
    return out

def disk_intervals(snaps, dev):
    rows = []
    for a, b in zip(snaps, snaps[1:]):
        if dev not in a["disk"] or dev not in b["disk"]:
            continue
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        x, y = a["disk"][dev], b["disk"][dev]
        dd = [yy - xx for xx, yy in zip(x, y)]
        if any(v < 0 for i, v in enumerate(dd[:11]) if i != 8):   # 카운터 리셋/랩어라운드
            continue
        rio, rsec, rtk, wio, wsec, wtk, iotk, wtd = dd[0], dd[2], dd[3], dd[4], dd[6], dd[7], dd[9], dd[10]
        rows.append({
            "t": b["t"], "dt": dt, "rio": rio, "wio": wio, "rtk": rtk, "wtk": wtk,
            "rs": rio / dt, "ws": wio / dt,
            "rmb": rsec * 512 / 1048576.0 / dt, "wmb": wsec * 512 / 1048576.0 / dt,
            "r_await": (rtk / float(rio)) if rio > 0 else None,
            "w_await": (wtk / float(wio)) if wio > 0 else None,
            "aqu": wtd / (dt * 1000.0),
            "util": min(100.0, iotk / (dt * 10.0)),
            "inflight": y[8],
        })
    return rows

def aggregate(per_dev):
    """여러 물리 디스크를 하나로 합산 (IOPS·처리량 합, 응답시간은 I/O 가중)"""
    by_t = {}
    for dev, rows in per_dev.items():
        for r in rows:
            k = round(r["t"], 1)
            by_t.setdefault(k, []).append(r)
    out = []
    for k in sorted(by_t):
        rs = by_t[k]
        rio = sum(r["rio"] for r in rs); wio = sum(r["wio"] for r in rs)
        out.append({
            "t": k, "dt": rs[0]["dt"], "rio": rio, "wio": wio,
            "rs": sum(r["rs"] for r in rs), "ws": sum(r["ws"] for r in rs),
            "rmb": sum(r["rmb"] for r in rs), "wmb": sum(r["wmb"] for r in rs),
            "r_await": (sum(r["rtk"] for r in rs) / float(rio)) if rio else None,
            "w_await": (sum(r["wtk"] for r in rs) / float(wio)) if wio else None,
            "rtk": sum(r["rtk"] for r in rs), "wtk": sum(r["wtk"] for r in rs),
            "aqu": sum(r["aqu"] for r in rs), "util": max(r["util"] for r in rs),
        })
    return out

def sys_intervals(snaps, ncpu):
    rows = []
    for a, b in zip(snaps, snaps[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        r = {"t": b["t"]}
        if "cpu" in a and "cpu" in b:
            d = [y - x for x, y in zip(a["cpu"], b["cpu"])]
            tot = float(sum(d[:8])) or 1.0
            r["iowait"] = 100.0 * d[4] / tot
            r["steal"] = 100.0 * d[7] / tot if len(d) > 7 else None
        r["blocked"] = b.get("blocked")
        for key in ("psi_io", "psi_memory"):
            if key in a and key in b:
                for kind in ("some", "full"):
                    if kind in a[key] and kind in b[key]:
                        r[key + "_" + kind] = min(100.0, (b[key][kind] - a[key][kind]) / (dt * 1e6) * 100.0)
        if "vm" in a and "vm" in b:
            r["swpin"] = (b["vm"].get("pswpin", 0) - a["vm"].get("pswpin", 0)) / dt
            r["swpout"] = (b["vm"].get("pswpout", 0) - a["vm"].get("pswpout", 0)) / dt
            r["majflt_sys"] = (b["vm"].get("pgmajfault", 0) - a["vm"].get("pgmajfault", 0)) / dt
        if "mem" in b:
            r["dirty_mb"] = b["mem"].get("Dirty", 0) / 1024.0
            r["wb_mb"] = b["mem"].get("Writeback", 0) / 1024.0
            r["avail_mb"] = b["mem"].get("MemAvailable", 0) / 1024.0
        if "pio" in a and "pio" in b:
            r["es_rmb"] = (b["pio"].get("read_bytes", 0) - a["pio"].get("read_bytes", 0)) / 1048576.0 / dt
            r["es_wmb"] = (b["pio"].get("write_bytes", 0) - a["pio"].get("write_bytes", 0)) / 1048576.0 / dt
        if "majflt" in a and "majflt" in b:
            r["es_majflt"] = (b["majflt"] - a["majflt"]) / dt
        if "dstate" in b:
            r["es_dstate"] = b["dstate"][0]
        if "tcp" in a and "tcp" in b:
            out_d = b["tcp"].get("OutSegs", 0) - a["tcp"].get("OutSegs", 0)
            re_d = b["tcp"].get("RetransSegs", 0) - a["tcp"].get("RetransSegs", 0)
            r["retrans_pct"] = (100.0 * re_d / out_d) if out_d > 100 else None
        rows.append(r)
    return rows

def net_deltas(snaps):
    if len(snaps) < 2:
        return {}
    a, b = snaps[0]["net"], snaps[-1]["net"]
    dt = snaps[-1]["t"] - snaps[0]["t"] or 1.0
    out = {}
    for ifn in b:
        if ifn in a:
            d = [y - x for x, y in zip(a[ifn], b[ifn])]
            out[ifn] = {"rx_mbs": d[0] / 1048576.0 / dt, "tx_mbs": d[8] / 1048576.0 / dt,
                        "rx_err": d[2], "rx_drop": d[3], "tx_err": d[10], "tx_drop": d[11]}
    return out

# ─────────────────────────────────────────────────────────────────────────────
# sar 이력 파싱 (버전별 헤더 이름 기반)
# ─────────────────────────────────────────────────────────────────────────────
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}:\d{2})(\s?[AP]M)?$')

def parse_sar(text, wanted_devs):
    days, cur_day, hdr = {}, None, None
    for line in text.splitlines():
        if line.startswith("#FILE"):
            cur_day, hdr = line.split()[-1].rsplit("/", 1)[-1], None; continue
        if line.startswith("Linux"):
            m = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2,4})', line)
            if m:
                cur_day = m.group(1)
            continue
        p = line.split()
        if not p or p[0].startswith("Average") or p[0].startswith("평균"):
            continue
        tok = 2 if len(p) > 1 and p[1] in ("AM", "PM") else 1
        if len(p) <= tok or not TIME_RE.match(" ".join(p[:tok])):
            continue
        if p[tok] == "DEV":
            hdr = p[tok + 1:]; continue
        if hdr is None or p[tok] not in wanted_devs:
            continue
        vals = dict(zip(hdr, p[tok + 1:]))
        aw = num(vals.get("await"))
        if aw is None:
            ra, wa = num(vals.get("r_await")), num(vals.get("w_await"))
            aw = max([x for x in (ra, wa) if x is not None] or [None]) if (ra or wa) else None
        rec = days.setdefault(cur_day or "?", {"await": [], "util": [], "worst": (None, None)})
        if aw is not None:
            rec["await"].append(aw)
            if rec["worst"][0] is None or aw > rec["worst"][0]:
                rec["worst"] = (aw, " ".join(p[:tok]) + " " + p[tok])
        u = num(vals.get("%util"))
        if u is not None:
            rec["util"].append(u)
    return days

# ─────────────────────────────────────────────────────────────────────────────
# fio 벤치 결과
# ─────────────────────────────────────────────────────────────────────────────
def load_bench(d):
    res = {}
    if not d or not os.path.isdir(d):
        return res
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        try:
            j = json.load(open(os.path.join(d, f)))
            job = j["jobs"][0]
            name = f[:-5]
            def lat99(side):
                pc = dig(job, side, "clat_ns", "percentile") or {}
                v = pc.get("99.000000")
                return v / 1e6 if v else None
            res[name] = {
                "r_iops": dig(job, "read", "iops"), "w_iops": dig(job, "write", "iops"),
                "r_mbs": (dig(job, "read", "bw") or 0) / 1024.0, "w_mbs": (dig(job, "write", "bw") or 0) / 1024.0,
                "r_p99": lat99("read"), "w_p99": lat99("write"),
                "sync_p99": (dig(job, "sync", "lat_ns", "percentile") or {}).get("99.000000"),
            }
            if res[name]["sync_p99"]:
                res[name]["sync_p99"] /= 1e6
        except Exception:
            continue
    return res

# ─────────────────────────────────────────────────────────────────────────────
# 클러스터 관점 분석 (es_cluster_probe.sh 결과)
# ─────────────────────────────────────────────────────────────────────────────
def analyze_cluster(cdir, add, th):
    """노드별 디스크 지표를 비교해 '이 노드만'인지 '클러스터 전체'인지 가른다."""
    if not cdir or not os.path.isdir(cdir):
        return None
    j = lambda n: rjson(cdir, n)
    s1, s2 = j("node_stats_1.json"), j("node_stats_2.json")
    if not s1 or not s2:
        return None
    meta = kv(rd(cdir, "meta"))
    n1, n2 = s1.get("nodes", {}), s2.get("nodes", {})
    rows, io_ok = [], False
    for nid, a in n1.items():
        b = n2.get(nid)
        if not b:
            continue
        dt = (num(b.get("timestamp"), 0) - num(a.get("timestamp"), 0)) / 1000.0
        if dt <= 0:
            dt = num(meta.get("gap"), 60) or 60
        d = lambda *pth: ((dig(b, *pth) or 0) - (dig(a, *pth) or 0)) if isinstance(dig(a, *pth), (int, float)) else None
        io_a, io_b = dig(a, "fs", "io_stats", "total") or {}, dig(b, "fs", "io_stats", "total") or {}
        busy = rops = wops = rmb = wmb = None
        if io_b:
            io_ok = True
            if "io_time_in_millis" in io_b and "io_time_in_millis" in io_a:   # 없는 버전은 '미수집'으로
                it = io_b["io_time_in_millis"] - io_a["io_time_in_millis"]
                busy = min(100.0, 100.0 * it / (dt * 1000.0)) if it >= 0 else None
            rops = (io_b.get("read_operations", 0) - io_a.get("read_operations", 0)) / dt
            wops = (io_b.get("write_operations", 0) - io_a.get("write_operations", 0)) / dt
            rmb = (io_b.get("read_kilobytes", 0) - io_a.get("read_kilobytes", 0)) / 1024.0 / dt
            wmb = (io_b.get("write_kilobytes", 0) - io_a.get("write_kilobytes", 0)) / 1024.0 / dt
        tot, avail = dig(b, "fs", "total", "total_in_bytes"), dig(b, "fs", "total", "available_in_bytes")
        idx_n, idx_t = d("indices", "indexing", "index_total"), d("indices", "indexing", "index_time_in_millis")
        rows.append({
            "name": b.get("name", nid[:8]), "roles": ",".join(r for r in (b.get("roles") or []) if r in
                     ("data", "data_hot", "data_warm", "data_cold", "data_frozen", "data_content", "master", "ingest", "ml")),
            "busy": busy, "rops": rops, "wops": wops, "rmb": rmb, "wmb": wmb,
            "used_pct": (100.0 * (tot - avail) / tot) if (tot and avail is not None) else None,
            "store_gb": (dig(b, "indices", "store", "size_in_bytes") or 0) / 1024.0 ** 3,
            "idx_rate": (idx_n / dt) if idx_n is not None else None,
            "idx_ms": (idx_t / float(idx_n)) if (idx_t is not None and idx_n) else None,
            "throttle": d("indices", "indexing", "throttle_time_in_millis"),
            "wrej": d("thread_pool", "write", "rejected"), "srej": d("thread_pool", "search", "rejected"),
            "merge_thr": d("indices", "merges", "total_throttled_time_in_millis"),
            "flush_ms": ((d("indices", "flush", "total_time_in_millis") or 0) / float(d("indices", "flush", "total")))
                        if d("indices", "flush", "total") else None,
            "segments": dig(b, "indices", "segments", "count"),
            "translog_mb": (dig(b, "indices", "translog", "size_in_bytes") or 0) / 1048576.0,
            "heap_pct": dig(b, "jvm", "mem", "heap_used_percent"), "cpu": dig(b, "os", "cpu", "percent"),
        })
    rows.sort(key=lambda r: -(r["busy"] or 0))
    data_rows = [r for r in rows if "data" in (r["roles"] or "")] or rows

    # ── 노드 간 쏠림 ─────────────────────────────────────────────────────
    def outlier(key, label, unit, floor, factor=2.0):
        vals = [r[key] for r in data_rows if r[key] is not None]
        if len(vals) < 3:
            return
        med = pctl(vals, 0.5)
        top = max(data_rows, key=lambda r: r[key] if r[key] is not None else -1)
        if med and top[key] and top[key] >= floor and top[key] >= med * factor:
            add("caution", "클러스터", "원인 분리 필요", "{} 쏠림 — {} 노드만 유독 높음".format(label, top["name"]),
                "{} {} vs 클러스터 중앙값 {}".format(top["name"], fmt(top[key], 1, unit), fmt(med, 1, unit)),
                "전체가 아니라 특정 노드만 높다면 그 노드의 스토리지나 샤드 배치가 원인일 가능성이 큽니다. "
                "클러스터 전체가 비슷하게 높다면 스토리지 공통 구간이나 워크로드 자체를 봐야 합니다.",
                "해당 노드에서 es_disk_collect.sh 를 실행해 커널 레벨로 확인하세요.", "_nodes/stats (노드 간 비교)")
    outlier("busy", "디스크 사용 시간(busy)", "%", 40)
    outlier("wmb", "쓰기 처리량", " MB/s", 30)
    outlier("store_gb", "샤드 보관 용량", " GB", 100, factor=1.6)

    # ── 용량·watermark ───────────────────────────────────────────────────
    settings = {}
    cs = j("cluster_settings.json") or {}
    for grp in ("defaults", "persistent", "transient"):
        settings.update(cs.get(grp) or {})
    hi = str(settings.get("cluster.routing.allocation.disk.watermark.high", "90%"))
    hi_pct = num(hi.rstrip("%")) if hi.endswith("%") else None
    over = [r for r in data_rows if r["used_pct"] is not None and hi_pct and r["used_pct"] >= hi_pct - 5]
    if over:
        sev = "crit" if any(r["used_pct"] >= (hi_pct or 90) for r in over) else "caution"
        add(sev, "클러스터", "ES 설정", "디스크 사용률이 watermark에 도달했거나 근접한 노드 있음",
            ", ".join("{} {:.0f}%".format(r["name"], r["used_pct"]) for r in over[:6]) + " (high {})".format(hi),
            "high watermark를 넘으면 ES가 샤드를 다른 노드로 옮깁니다. 이 복사 작업 자체가 큰 디스크 부하라, 느려서 넘쳤는데 더 느려지는 악순환이 생깁니다.",
            "ILM 정책 점검으로 오래된 인덱스를 정리하거나 용량을 늘리세요. 임시로 watermark를 올리는 것은 원인을 미루는 것뿐입니다.",
            "[Elastic 공식] Disk-based shard allocation")

    # ── 클러스터발 디스크 부하 (측정 오염 요인) ────────────────────────
    rec = j("cat_recovery.json") or []
    health = j("health.json") or {}
    snap = j("snapshot_status.json") or {}
    acts = []
    if isinstance(rec, list) and rec:
        acts.append("샤드 복구 {}건 진행 중".format(len(rec)))
    if (health.get("relocating_shards") or 0) > 0:
        acts.append("샤드 이동 {}개".format(health["relocating_shards"]))
    if (health.get("initializing_shards") or 0) > 0:
        acts.append("초기화 중 샤드 {}개".format(health["initializing_shards"]))
    if snap.get("snapshots"):
        acts.append("스냅샷 {}건 실행 중".format(len(snap["snapshots"])))
    pend = j("pending_tasks.json")
    if isinstance(pend, list) and len(pend) > 5:
        acts.append("pending task {}건".format(len(pend)))
    if acts:
        rmax = settings.get("indices.recovery.max_bytes_per_sec", "40mb")
        add("info", "클러스터", "참고", "측정 시점에 클러스터가 스스로 디스크를 쓰고 있었음",
            " · ".join(acts) + " · indices.recovery.max_bytes_per_sec={}".format(rmax),
            "복구·이동·스냅샷은 정상 동작이지만 순간적으로 디스크를 크게 씁니다. 이 시간대 측정값은 평상시보다 나쁘게 나옵니다.",
            "작업이 끝난 뒤 다시 측정해 비교하세요. 복구가 상시 디스크를 압박한다면 indices.recovery.max_bytes_per_sec 조정을 검토합니다(운영 영향 확인 필요).",
            "_cat/recovery, _cluster/health, _snapshot/_status")

    # ── 전 노드 공통 신호 ────────────────────────────────────────────────
    thr_nodes = [r["name"] for r in data_rows if (r["throttle"] or 0) > 0]
    if len(thr_nodes) >= 2:
        add("warn", "클러스터", "원인 분리 필요", "여러 노드에서 동시에 인덱싱 스로틀 발생",
            "{}개 노드: {}".format(len(thr_nodes), ", ".join(thr_nodes[:8])),
            "한 노드만이면 그 노드의 디스크 문제지만, 여러 노드가 동시라면 공용 스토리지(vSAN) 또는 인입량 자체가 원인일 가능성이 큽니다.",
            "vSAN 클러스터 단위 지표를 VMware 관리자와 함께 확인하고, 동시에 인입량·bulk 크기·샤드 수도 점검하세요.",
            "_nodes/stats indices.indexing.throttle_time")
    aw = settings.get("cluster.routing.allocation.awareness.attributes")
    if not aw and len(data_rows) >= 3:
        add("info", "클러스터", "VMware 관리자", "shard allocation awareness 미설정",
            "cluster.routing.allocation.awareness.attributes 없음 · data 노드 {}개".format(len(data_rows)),
            "ESXi 호스트 한 대에 primary와 replica를 가진 VM이 같이 올라가 있으면, 호스트 한 대가 죽을 때 두 벌을 동시에 잃습니다. ES는 VM이 어느 호스트에 있는지 모릅니다.",
            "노드에 호스트 정보를 attribute로 넣고 awareness를 설정하는 방법과, VMware 쪽 DRS anti-affinity 규칙을 함께 검토하세요.",
            "[Elastic 공식] Shard allocation awareness")
    return {"rows": rows, "data_rows": data_rows, "io_ok": io_ok, "meta": meta, "settings": settings,
            "health": health, "recovery": rec if isinstance(rec, list) else [], "acts": acts}


def analyze_local_indices(S, cdir):
    """이 노드에 있는 샤드를 인덱스별로 나눠 쓰기 부하 분포를 본다."""
    a, b = rjson(S, "es_idx_start.json"), rjson(S, "es_idx_end.json")
    if not a or not b:
        return None
    ga = list((a.get("nodes") or {}).values())
    gb = list((b.get("nodes") or {}).values())
    if not ga or not gb:
        return None
    ia, ib = ga[0].get("indices") or {}, gb[0].get("indices") or {}
    ilm = {}
    if cdir:
        il = rjson(cdir, "ilm_explain.json") or {}
        ilm = il.get("indices") or {}
    NODE_KEYS = {"indexing", "search", "merges", "refresh", "flush", "store", "segments", "translog",
                 "docs", "get", "query_cache", "fielddata", "completion", "warmer", "request_cache",
                 "recovery", "bulk", "shard_stats", "mappings", "dense_vector", "sparse_vector"}
    rows = []
    for name, y in ib.items():
        if name in NODE_KEYS or not isinstance(y, dict):   # level=indices 응답이 아닐 때 방어
            continue
        x = ia.get(name, {})
        dn = (dig(y, "indexing", "index_total") or 0) - (dig(x, "indexing", "index_total") or 0)
        dt_ = (dig(y, "indexing", "index_time_in_millis") or 0) - (dig(x, "indexing", "index_time_in_millis") or 0)
        dm = (dig(y, "merges", "total_size_in_bytes") or 0) - (dig(x, "merges", "total_size_in_bytes") or 0)
        dq = (dig(y, "search", "query_total") or 0) - (dig(x, "search", "query_total") or 0)
        if dn <= 0 and dm <= 0 and dq <= 0:
            continue
        rows.append({"index": name, "docs": dn, "ms_per_doc": (dt_ / float(dn)) if dn else None,
                     "merge_mb": dm / 1048576.0, "queries": dq,
                     "store_gb": (dig(y, "store", "size_in_bytes") or 0) / 1024.0 ** 3,
                     "segments": dig(y, "segments", "count"),
                     "phase": dig(ilm.get(name, {}), "phase") or "", "policy": dig(ilm.get(name, {}), "policy") or ""})
    if not rows:
        return None
    rows.sort(key=lambda r: -(r["docs"] + r["merge_mb"] * 50))
    return rows

# ─────────────────────────────────────────────────────────────────────────────
# 분석
# ─────────────────────────────────────────────────────────────────────────────
class Finding(object):
    def __init__(self, sev, dim, owner, title, evidence, why, action, source):
        self.sev, self.dim, self.owner = sev, dim, owner
        self.title, self.evidence, self.why, self.action, self.source = title, evidence, why, action, source

def analyze(base, storage_override=None, bench_dir=None, cluster_dir=None):
    S = os.path.join(base, "static")
    meta = kv(rd(base, "meta"))
    storage = storage_override or meta.get("storage", "allflash")
    th = LAT_TH.get(storage, LAT_TH["allflash"])
    es_pid = meta.get("es_pid", "")
    sysctl = kv(rd(S, "sysctl"))
    virt = kv(rd(S, "virt"))
    topo = Topo(rd(S, "sysfs"))
    mounts = parse_mounts(rd(S, "mounts"))
    meminfo = kv(rd(S, "meminfo"), ":")
    mem_total_mb = num(meminfo.get("MemTotal", "0 kB").split()[0], 0) / 1024.0
    ncpu = int(num(rd(S, "nproc").strip(), 1) or 1)
    es_root = rjson(S, "es_root.json")
    nodeinfo = rjson(S, "es_nodeinfo.json")
    st0, st1 = rjson(S, "es_stats_start.json"), rjson(S, "es_stats_end.json")
    csettings = rjson(S, "es_cluster_settings.json")
    health = rjson(S, "es_health.json")
    node_i = list((nodeinfo or {}).get("nodes", {}).values())[0] if (nodeinfo or {}).get("nodes") else {}
    n0 = list((st0 or {}).get("nodes", {}).values())[0] if (st0 or {}).get("nodes") else None
    n1 = list((st1 or {}).get("nodes", {}).values())[0] if (st1 or {}).get("nodes") else None
    es_version = dig(es_root or {}, "version", "number") or dig(node_i, "version") or "미확인"

    F = []
    add = lambda *a: F.append(Finding(*a))

    # ── ES data path → 디바이스 ─────────────────────────────────────────────
    cand = [p for p in meta.get("user_paths", "").split() if p]
    npd = dig(node_i, "settings", "path", "data")
    if npd:
        cand += npd if isinstance(npd, list) else [npd]
    for l in rd(S, "es_cmdline").splitlines():
        if "path.data=" in l:
            cand += l.split("path.data=", 1)[1].split(",")
    cand += yml_data_paths(rd(S, "es_yml"))
    if not cand:
        cand = ["/var/lib/elasticsearch"]
    data_paths = []
    for p in cand:
        if p and p not in data_paths:
            data_paths.append(p)

    path_map = []
    for p in data_paths:
        m = mount_for(p, mounts)
        k = topo.kname(m["src"]) if m else None
        path_map.append({"path": p, "mount": m, "kname": k, "phys": topo.physical(k) if k else []})
    phys = sorted(set(d for pm in path_map for d in pm["phys"]))
    logical = sorted(set(pm["kname"] for pm in path_map if pm["kname"]))
    dev_guess = False
    if not phys:
        dev_guess = True
        phys = sorted(d for d in topo.attr if not d.startswith("dm-") and not d.startswith("md"))

    # ── 샘플 ────────────────────────────────────────────────────────────────
    snaps = parse_samples(rd(base, "samples.raw"), es_pid)
    per_dev = {d: disk_intervals(snaps, d) for d in phys}
    per_log = {d: disk_intervals(snaps, d) for d in logical if d not in phys}
    agg = aggregate(per_dev)
    sysr = sys_intervals(snaps, ncpu)
    nets = net_deltas(snaps)
    dur = (snaps[-1]["t"] - snaps[0]["t"]) if len(snaps) > 1 else 0

    def dstats(rows):
        valid_r = [r["r_await"] for r in rows if r["rio"] >= MIN_IOS_PER_INTERVAL]
        valid_w = [r["w_await"] for r in rows if r["wio"] >= MIN_IOS_PER_INTERVAL]
        trio, twio = sum(r["rio"] for r in rows), sum(r["wio"] for r in rows)
        return {
            "n": len(rows),
            "r_await_mean": (sum(r["rtk"] for r in rows) / float(trio)) if trio else None,
            "w_await_mean": (sum(r["wtk"] for r in rows) / float(twio)) if twio else None,
            "r_await_p95": pctl(valid_r, 0.95), "w_await_p95": pctl(valid_w, 0.95),
            "r_await_max": vmax(valid_r), "w_await_max": vmax(valid_w),
            "valid_r": len(valid_r), "valid_w": len(valid_w),
            "iops_p95": pctl([r["rs"] + r["ws"] for r in rows], 0.95),
            "iops_avg": avg([r["rs"] + r["ws"] for r in rows]),
            "rs_p95": pctl([r["rs"] for r in rows], 0.95), "ws_p95": pctl([r["ws"] for r in rows], 0.95),
            "mb_p95": pctl([r["rmb"] + r["wmb"] for r in rows], 0.95),
            "rmb_p95": pctl([r["rmb"] for r in rows], 0.95), "wmb_p95": pctl([r["wmb"] for r in rows], 0.95),
            "aqu_p95": pctl([r["aqu"] for r in rows], 0.95), "aqu_max": vmax([r["aqu"] for r in rows]),
            "util_p95": pctl([r["util"] for r in rows], 0.95),
        }
    A = dstats(agg)
    dev_stats = {d: dstats(r) for d, r in per_dev.items()}
    log_stats = {d: dstats(r) for d, r in per_log.items()}

    # ── 부하 수준 ─────────────────────────────────────────────────────────
    low_load = (A["iops_p95"] or 0) < LOW_LOAD_IOPS and (A["mb_p95"] or 0) < LOW_LOAD_MBPS

    # ═════════════ 1. 지연 ═════════════
    r_sev, w_sev = grade(A["r_await_p95"], th), grade(A["w_await_p95"], th)
    lat_ev = "읽기 p95 {} / 평균 {} · 쓰기 p95 {} / 평균 {} (I/O {}건 이상 구간만 집계: 읽기 {}구간, 쓰기 {}구간)".format(
        fmt(A["r_await_p95"], 2, "ms"), fmt(A["r_await_mean"], 2, "ms"),
        fmt(A["w_await_p95"], 2, "ms"), fmt(A["w_await_mean"], 2, "ms"),
        MIN_IOS_PER_INTERVAL, A["valid_r"], A["valid_w"])
    src_lat = "[VMware 공식] Broadcom KB 389082 — All-Flash 5ms 미만 / Hybrid 20ms 미만을 정상으로 제시. 주의·경고·위험 3단계 구분은 실무 기준이며 Elastic 공식 수치는 없음"
    lat_sev = sev_max(r_sev, w_sev)
    if A["valid_r"] + A["valid_w"] == 0:
        add("info", "지연", "참고", "측정 구간에 디스크 I/O가 거의 없어 응답시간을 평가할 수 없음",
            lat_ev, "I/O가 적은 구간의 응답시간은 한두 건의 느린 요청에 좌우되어 신뢰할 수 없습니다.",
            "인덱싱·검색 피크 시간대에 다시 측정하세요 (-d 600 이상 권장).", src_lat)
        lat_sev = "na"
    elif lat_sev in ("ok",):
        add("ok", "지연", "참고", "디스크 응답시간 정상 범위", lat_ev,
            "Guest가 본 응답시간(await)은 vSAN·하이퍼바이저·가상 SCSI를 모두 거친 결과라 ES가 실제로 겪는 지연과 같습니다.",
            "조치 불필요. 피크 시간대 재측정으로 여유를 확인하세요.", src_lat)
    else:
        add(lat_sev, "지연", "원인 분리 필요",
            "디스크 응답시간이 {} 기준 {} 수준".format("All-Flash" if storage == "allflash" else "Hybrid", SEV_LABEL[lat_sev]),
            lat_ev,
            "쓰기 지연은 translog fsync와 segment flush를 늦춰 인덱싱 지연으로, 읽기 지연은 page cache에 없는 segment 조회를 늦춰 검색 지연으로 바로 이어집니다.",
            "아래 '병목 위치' 판정을 먼저 확인하세요. Guest 큐가 원인이면 서버에서, 아니면 VMware 쪽에서 풀어야 합니다.",
            src_lat)

    # 병목 위치 — 큐 사용률과 응답시간의 조합
    qd = [num(topo.attr.get(d, {}).get("device/queue_depth")) for d in phys]
    qd = [q for q in qd if q]
    qd_total = sum(qd) if qd else None
    qratio = (A["aqu_p95"] / qd_total) if (qd_total and A["aqu_p95"] is not None) else None
    if SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
        if qratio is not None and qratio >= 0.8:
            add("warn", "포화", "서버 담당자",
                "병목 위치: Guest 쪽 큐가 가득 참 (큐 사용률 {:.0f}%)".format(qratio * 100),
                "평균 대기 I/O p95 {} / 디바이스 queue_depth 합계 {}".format(fmt(A["aqu_p95"], 1), int(qd_total)),
                "가상 디스크가 동시에 받을 수 있는 I/O 수가 한계에 닿아, 요청이 VM 안에서 줄을 서고 있습니다. 백엔드가 빨라도 이 구간은 느려집니다.",
                "ES data용 VMDK를 여러 개로 나눠 별도 PVSCSI 컨트롤러에 붙이고 LVM stripe로 묶는 방법이 가장 효과적입니다. "
                "그다음 PVSCSI queue depth 상향(cmd_per_lun=254, ring_pages=32, 재부팅 필요)을 검토하세요.",
                "[VMware 공식] KB 2053145 — PVSCSI 기본 큐 64(device)/254(adapter), ring_pages 8→32 및 cmd_per_lun 254 권장")
        else:
            add("warn" if lat_sev in ("warn", "crit") else "caution", "지연", "VMware 관리자",
                "병목 위치: VM 바깥(하이퍼바이저·vSAN) 가능성 높음",
                "응답시간은 높은데 대기 I/O는 적음 (aqu-sz p95 {} / queue_depth {})".format(
                    fmt(A["aqu_p95"], 2), int(qd_total) if qd_total else "미확인"),
                "VM 안에서 기다리는 요청이 적은데도 한 건 한 건이 느리다는 뜻입니다. vSAN resync, 캐시 계층 포화, 같은 호스트 다른 VM의 I/O 경합, "
                "vSAN 네트워크 지연이 전형적인 원인입니다. Guest 설정 변경으로는 개선되지 않습니다.",
                "측정 시각과 이 리포트를 VMware 관리자에게 전달하고 esxtop의 DAVG/KAVG/GAVG, vSAN 성능 서비스의 VM·디스크 그룹 지연, "
                "resync 진행 여부를 같은 시각으로 확인 요청하세요.",
                "[VMware 공식] Broadcom 'Troubleshooting vSAN Performance' — 게스트 내부 지연과 VM/VMDK 레벨 지연의 차이는 큐 깊이가 낮은 컨트롤러의 큐 고갈에서 비롯될 수 있다고 서술")
    # 쓰기만 느림 → vSAN 쓰기 경로 힌트
    if (A["w_await_p95"] and A["r_await_p95"] and A["valid_w"] >= 3 and A["valid_r"] >= 3
            and A["w_await_p95"] >= th["caution"] and A["w_await_p95"] > 3 * A["r_await_p95"]):
        add("caution", "지연", "VMware 관리자", "쓰기만 유독 느림 — vSAN 쓰기 경로 확인 필요",
            "쓰기 p95 {} vs 읽기 p95 {}".format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
            "vSAN은 쓰기를 복제본 전부에서 확인받아야 끝납니다(RAID-1 FTT=1이면 호스트 2대). 그래서 쓰기만 느리면 vSAN 네트워크, "
            "쓰기 버퍼 destage, RAID-5/6 정책의 read-modify-write를 의심할 수 있습니다. Guest에서는 vSAN 네트워크를 직접 볼 수 없어 추정입니다.",
            "VMware 관리자에게 vSAN 네트워크 지연·재전송, 쓰기 버퍼 사용률, 스토리지 정책(RAID/FTT)을 확인 요청하세요.",
            "[VMware 공식] Broadcom vSAN 문서 (쓰기 경로·복제 동작)")

    # ═════════════ 2. 포화 ═════════════
    psi_full = [r.get("psi_io_full") for r in sysr if r.get("psi_io_full") is not None]
    psi_some = [r.get("psi_io_some") for r in sysr if r.get("psi_io_some") is not None]
    iow = [r.get("iowait") for r in sysr if r.get("iowait") is not None]
    dst = [r.get("es_dstate") for r in sysr if r.get("es_dstate") is not None]
    blk = [r.get("blocked") for r in sysr if r.get("blocked") is not None]
    sat_sevs = []
    if psi_full:
        pf95 = pctl(psi_full, 0.95)
        s = "warn" if pf95 >= 20 else "caution" if pf95 >= 5 else "ok"
        psi_ev = "some p95 {} · full p95 {} · full 최대 {}".format(fmt(pctl(psi_some, .95), 1, "%"), fmt(pf95, 1, "%"), fmt(vmax(psi_full), 1, "%"))
        psi_src = "Linux kernel Documentation/accounting/psi.rst (단계 기준은 실무 기준)"
        if s != "ok" and SEV_ORDER.get(lat_sev, 0) <= SEV_ORDER["ok"]:
            # 기다리는 시간은 많은데 한 건 한 건은 빠름 → 디스크가 느린 게 아니라 I/O 양이 많은 상태
            s = "caution" if s == "warn" else "info"
            sat_sevs.append(s)
            add(s, "포화", "원인 분리 필요", "I/O 대기 비율은 높지만 응답시간은 빠름 — 디스크보다 I/O 양이 많은 상태",
                psi_ev + " · 응답시간 p95 읽기 {} / 쓰기 {}".format(fmt(A["r_await_p95"], 2, "ms"), fmt(A["w_await_p95"], 2, "ms")),
                "PSI는 '작업이 I/O를 기다리며 멈춘 시간 비율'이라 동기 쓰기(fsync)를 쉬지 않고 반복하는 작업이 있으면 디스크가 빨라도 높게 나옵니다. "
                "또 서버 전체 지표라 ES가 아닌 백업·로그 수집 에이전트 때문일 수도 있습니다.",
                "ES 물리 I/O(/proc/<pid>/io)와 전체 디스크 처리량을 비교해 I/O를 누가 만드는지 먼저 확인하세요. ES라면 bulk 크기·refresh 주기처럼 I/O를 줄이는 쪽을 봅니다.",
                psi_src)
        else:
            sat_sevs.append(s)
            add(s, "포화", "참고" if s == "ok" else "원인 분리 필요",
                "I/O 압박(PSI io full) p95 {}".format(fmt(pf95, 1, "%")), psi_ev,
                "PSI는 커널이 '실행 가능한 작업이 모두 I/O를 기다리며 멈춘 시간 비율'을 직접 잽니다. iowait보다 정확한 포화 지표입니다. 단, 서버 전체 기준입니다.",
                "응답시간 판정과 함께 원인을 분리하세요." if s != "ok" else "조치 불필요.", psi_src)
    else:
        add("info", "포화", "참고", "PSI(Pressure Stall Information) 미지원 또는 비활성",
            "/proc/pressure/io 없음",
            "RHEL 8은 커널에 포함돼 있지만 기본 비활성입니다. 켜면 I/O 포화를 가장 정확하게 볼 수 있습니다.",
            "필요 시 커널 부트 파라미터 psi=1 추가 후 재부팅 (운영 변경이므로 정기 점검 때 검토). 이번 판정은 D-state·큐·지연으로 대신합니다.",
            "RHEL 8 커널 문서")
    if dst:
        d95, dmx = pctl(dst, .95), vmax(dst)
        s = "warn" if d95 >= 8 else "caution" if d95 >= 3 else "ok"
        sat_sevs.append(s)
        if s != "ok":
            add(s, "포화", "원인 분리 필요", "ES 스레드가 디스크 대기(D 상태)로 자주 멈춤",
                "D 상태 스레드 수 p95 {} · 최대 {}".format(fmt(d95, 0), fmt(dmx, 0)),
                "D 상태는 커널 안에서 I/O 완료를 기다리며 멈춘 상태입니다. write·search 스레드가 여기 걸리면 그 시간만큼 요청 처리가 멈춥니다.",
                "지연 판정과 함께 보세요. 쓰기 쪽이면 translog/merge, 읽기 쪽이면 page cache 부족을 의심합니다.",
                "/proc/<pid>/task/*/stat 상태 필드")
    iow_p95 = pctl(iow, .95)
    add("info", "포화", "참고", "CPU iowait p95 {} (참고 지표)".format(fmt(iow_p95, 1, "%")),
        "평균 {} · 최대 {} · 차단 프로세스(procs_blocked) p95 {}".format(fmt(avg(iow), 1, "%"), fmt(vmax(iow), 1, "%"), fmt(pctl(blk, .95), 0)),
        "iowait는 'CPU가 놀면서 I/O를 기다린 시간'이라 CPU가 바쁘면 I/O가 밀려도 낮게 나오고, 코어가 많으면 희석됩니다. 단독 판정에 쓰지 않고 추세 확인용으로만 씁니다.",
        "판정은 응답시간·PSI·D 상태를 기준으로 합니다.", "Linux proc(5) /proc/stat")
    sat_sev = sev_max(*sat_sevs) if sat_sevs else "na"

    # ═════════════ 3. 오류 (커널 로그) ═════════════
    klog = rd(S, "klog_io").splitlines()
    STO = r'(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9])'
    cats = {
        "I/O 오류": r'I/O error|blk_update_request|Buffer I/O error|critical medium|rejecting I/O',
        "SCSI abort/reset": STO + r'.*\b(abort\w*|reset)\b',
        "hung task (120초 이상 멈춤)": r'hung_task|blocked for more than',
        "파일시스템 오류·읽기전용 전환": r'XFS .*(error|shutdown|corruption)|EXT4-fs error|remount.*read-only',
        "타임아웃": STO + r'.*(timed out|timing out|timeout)',
    }
    cnt = {k: sum(1 for l in klog if re.search(v, l, re.I)) for k, v in cats.items()}
    err_sev = "ok"
    if cnt["I/O 오류"] or cnt["파일시스템 오류·읽기전용 전환"]:
        err_sev = "crit"
    elif cnt["hung task (120초 이상 멈춤)"] or cnt["SCSI abort/reset"]:
        err_sev = "warn"
    elif cnt["타임아웃"]:
        err_sev = "caution"
    if err_sev != "ok":
        add(err_sev, "오류", "VMware 관리자", "커널 로그에 디스크 관련 오류 기록",
            " · ".join("{} {}건".format(k, v) for k, v in cnt.items() if v),
            "vSAN 경로가 잠시 멈추면(호스트 장애, resync 폭주, 네트워크 단절) Guest에서는 SCSI abort/reset, hung task, 심하면 파일시스템 읽기전용 전환으로 나타납니다. "
            "짧은 측정 창에서 안 보인 과거 사고의 흔적입니다.",
            "리포트 부록의 로그 원문 시각을 VMware 관리자에게 전달해 같은 시각의 vSAN 이벤트를 확인하세요. I/O 오류·읽기전용 전환은 ES 데이터 무결성 점검이 필요합니다.",
            "커널 로그 (journalctl -k / dmesg), 최근 7일")
    else:
        add("ok", "오류", "참고", "최근 커널 로그에 디스크 오류 없음", "검사 패턴: I/O error, SCSI abort/reset, hung task, FS error, timeout",
            "vSAN 순간 정지의 흔적이 없다는 뜻입니다.", "조치 불필요.", "journalctl -k / dmesg")

    # ═════════════ 4. ES 영향 ═════════════
    es_sev = "na"
    es_rows = []
    if n0 and n1:
        es_sev = "ok"
        dt_es = (num(n1.get("timestamp"), 0) - num(n0.get("timestamp"), 0)) / 1000.0 or dur or 1
        def dl(*path):
            a, b = dig(n0, *path), dig(n1, *path)
            return (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        idx_n, idx_t = dl("indices", "indexing", "index_total"), dl("indices", "indexing", "index_time_in_millis")
        thr = dl("indices", "indexing", "throttle_time_in_millis")
        q_n, q_t = dl("indices", "search", "query_total"), dl("indices", "search", "query_time_in_millis")
        f_n, f_t = dl("indices", "search", "fetch_total"), dl("indices", "search", "fetch_time_in_millis")
        fl_n, fl_t = dl("indices", "flush", "total"), dl("indices", "flush", "total_time_in_millis")
        rf_n, rf_t = dl("indices", "refresh", "total"), dl("indices", "refresh", "total_time_in_millis")
        mg_t, mg_thr = dl("indices", "merges", "total_time_in_millis"), dl("indices", "merges", "total_throttled_time_in_millis")
        wr_rej, se_rej = dl("thread_pool", "write", "rejected"), dl("thread_pool", "search", "rejected")
        ip_rej = sum(x or 0 for x in (dl("indexing_pressure", "memory", "total", k) for k in
                                      ("coordinating_rejections", "primary_rejections", "replica_rejections")))
        per = lambda t, n: (t / float(n)) if (t is not None and n) else None
        es_rows = [
            ("인덱싱 처리량", fmt((idx_n or 0) / dt_es, 0, " docs/s"), ""),
            ("인덱싱 평균 소요 (문서당, 샤드 기준)", fmt(per(idx_t, idx_n), 3, " ms"), ""),
            ("인덱싱 스로틀 시간 (merge 지연 때문)", fmt(thr, 0, " ms"), "0이 아니면 merge가 쓰기를 못 따라가는 상태"),
            ("검색 query 평균", fmt(per(q_t, q_n), 1, " ms"), "샤드 단위 시간"),
            ("검색 fetch 평균", fmt(per(f_t, f_n), 1, " ms"), "_source 읽기 — 디스크 영향 큼"),
            ("flush 평균 (fsync 포함)", fmt(per(fl_t, fl_n), 0, " ms"), ""),
            ("refresh 평균", fmt(per(rf_t, rf_n), 0, " ms"), ""),
            ("merge 자동 스로틀 비율", fmt((100.0 * mg_thr / mg_t) if (mg_t and mg_thr is not None) else None, 0, "%"), "ES가 의도적으로 merge 속도를 조절한 비율 (정상 동작)"),
            ("write / search 거절", "{} / {}".format(fmt(wr_rej, 0), fmt(se_rej, 0)), "thread pool 큐 초과"),
            ("indexing pressure 거절", fmt(ip_rej, 0), ""),
        ]
        if thr:
            es_sev = sev_max(es_sev, "warn")
            add("warn", "ES 영향", "원인 분리 필요", "ES가 인덱싱을 스스로 늦추고 있음 (merge 적체)",
                "측정 구간 중 indexing throttle {} ms".format(fmt(thr, 0)),
                "merge가 밀리면 ES는 세그먼트 폭증을 막으려고 인덱싱 스레드를 1개로 줄입니다. 디스크 쓰기 성능이 인덱싱 속도를 못 따라간다는 직접 증거입니다.",
                "쓰기 지연 판정과 함께 보세요. 디스크가 원인이면 스토리지 개선, 아니면 refresh_interval 연장·bulk 크기·샤드 수를 검토합니다.",
                "Elasticsearch merge scheduler 동작 (indices.indexing.throttle_time)")
        if (wr_rej or 0) > 0 or ip_rej > 0:
            es_sev = sev_max(es_sev, "warn")
            add("warn", "ES 영향", "원인 분리 필요", "쓰기 요청 거절 발생",
                "write rejected {} · indexing pressure rejected {}".format(fmt(wr_rej, 0), fmt(ip_rej, 0)),
                "쓰기 처리가 밀려 큐가 넘쳤습니다. 디스크가 느린 경우가 흔하지만 CPU·heap·bulk 크기도 원인이 될 수 있습니다.",
                "쓰기 지연·D 상태 지표가 함께 나빠졌는지 확인해 디스크 원인 여부를 가리세요.", "Elasticsearch thread pool / indexing pressure")
        if (se_rej or 0) > 0:
            es_sev = sev_max(es_sev, "caution")
            add("caution", "ES 영향", "원인 분리 필요", "검색 요청 거절 발생", "search rejected {}".format(fmt(se_rej, 0)),
                "검색 큐 초과입니다. 읽기 지연·major fault가 같이 높으면 디스크(캐시 부족) 원인입니다.", "읽기 지표와 대조하세요.",
                "Elasticsearch thread pool")
        # 클러스터 상태가 측정을 오염시키는지
        if health and ((health.get("relocating_shards") or 0) + (health.get("initializing_shards") or 0)) > 0:
            add("info", "ES 영향", "참고", "측정 중 샤드 이동·복구 진행 중",
                "relocating {} · initializing {} · status {}".format(health.get("relocating_shards"), health.get("initializing_shards"), health.get("status")),
                "복구 I/O가 평소 부하에 더해져 있어 이번 수치는 평상시보다 나쁘게 나올 수 있습니다.", "복구 완료 후 재측정해 비교하세요.",
                "_cluster/health")
    else:
        add("info", "ES 영향", "참고", "ES 지표 미수집", "ES API 접속 실패 또는 --no-es",
            "OS 지표만으로도 디스크 판정은 가능하지만, ES 쪽 영향(스로틀·거절·지연)을 확인하지 못했습니다.",
            "--es-user 와 ES_PASSWORD(또는 ES_API_KEY)를 지정해 재실행하면 ES 영향까지 판정합니다.", "-")

    # ═════════════ 5. 메모리·캐시 ═════════════
    mem_sevs = []
    swaps = [l for l in rd(S, "swaps").splitlines()[1:] if l.strip()]
    swap_on = bool(swaps)
    mlock = dig(node_i, "process", "mlockall")
    swp = [(r.get("swpin") or 0) + (r.get("swpout") or 0) for r in sysr if "swpin" in r]
    swp_max = vmax(swp) or 0
    swappiness = num(sysctl.get("vm.swappiness"))
    if swp_max > 0:
        mem_sevs.append("warn")
        add("warn", "메모리·캐시", "서버 담당자", "측정 중 swap 입출력 발생", "swap in+out 최대 {} pages/s".format(fmt(swp_max, 0)),
            "ES heap이나 page cache가 swap으로 밀려나면 GC가 수 초씩 멈추고 그동안 디스크 읽기가 폭증합니다. Elastic은 swap을 꺼 두거나 막아 두라고 권고합니다.",
            "swapoff -a 후 /etc/fstab의 swap 항목 주석 처리(가장 확실), 또는 bootstrap.memory_lock: true 설정. VMware balloon 여부도 함께 확인하세요.",
            "[Elastic 공식] Disable swapping")
    if swap_on and mlock is not True:
        s = "caution" if (swappiness is not None and swappiness <= 1) else "warn"
        mem_sevs.append(s)
        add(s, "메모리·캐시", "서버 담당자", "swap이 켜져 있고 memory_lock도 꺼져 있음",
            "swap 장치 {}개 · vm.swappiness {} · mlockall {}".format(len(swaps), sysctl.get("vm.swappiness"), "미확인" if mlock is None else mlock),
            "지금 swap이 쓰이지 않아도 메모리 압박(balloon 포함) 순간에 ES가 swap으로 밀릴 수 있는 상태입니다.",
            "Elastic 권고 순서: ① swap 비활성화 ② 불가하면 bootstrap.memory_lock: true (memlock unlimited 필요) ③ 최소한 vm.swappiness=1.",
            "[Elastic 공식] Disable swapping")
    heap = dig(node_i, "jvm", "mem", "heap_max_in_bytes") or dig(n1 or {}, "jvm", "mem", "heap_max_in_bytes")
    if heap is None:
        m = re.search(r'-Xmx(\d+)([gGmM])', rd(S, "es_cmdline"))
        if m:
            heap = int(m.group(1)) * (1024 ** 3 if m.group(2) in "gG" else 1024 ** 2)
    heap_mb = heap / 1048576.0 if heap else None
    store_b = dig(n1 or {}, "indices", "store", "size_in_bytes")
    if heap_mb and mem_total_mb:
        cache_mb = mem_total_mb - heap_mb
        hr = heap_mb / mem_total_mb
        if hr > 0.5 or heap_mb > 31 * 1024:
            mem_sevs.append("caution")
            add("caution", "메모리·캐시", "ES 설정", "heap 비중이 커서 page cache 몫이 줄어듦",
                "heap {} / RAM {} ({:.0f}%)".format(fmt(heap_mb / 1024, 1, "GB"), fmt(mem_total_mb / 1024, 1, "GB"), hr * 100),
                "ES는 segment 읽기를 page cache에 기댑니다. heap이 RAM의 50%를 넘거나 compressed oops 경계(약 31GB)를 넘으면 캐시가 줄어 디스크 읽기가 늘어납니다.",
                "heap을 RAM의 50% 이하, 31GB 이하로 맞추는 것이 Elastic 권고입니다.", "[Elastic 공식] Set the JVM heap size")
        ratio_txt = ""
        if store_b:
            ratio = cache_mb * 1048576.0 / store_b
            ratio_txt = " · page cache 가능량 / 이 노드 샤드 용량 = {:.0f}%".format(ratio * 100)
        add("info", "메모리·캐시", "참고", "page cache로 쓸 수 있는 메모리 약 {}".format(fmt(cache_mb / 1024, 1, "GB")),
            "RAM {} − heap {}{}".format(fmt(mem_total_mb / 1024, 1, "GB"), fmt(heap_mb / 1024, 1, "GB"), ratio_txt),
            "검색에서 자주 읽는 segment가 이 안에 들어가면 디스크를 거의 읽지 않습니다. 비율이 낮을수록 검색이 디스크 속도에 좌우됩니다.",
            "검색 지연이 문제라면 이 비율과 ES major fault 추이를 함께 보세요.", "[Elastic 공식] Tune for search speed — Give memory to the filesystem cache")
    mj = [r.get("es_majflt") for r in sysr if r.get("es_majflt") is not None]
    if mj:
        mj95 = pctl(mj, .95)
        if mj95 and mj95 > 200 and SEV_ORDER.get(r_sev, 0) >= SEV_ORDER["caution"]:
            mem_sevs.append("caution")
            add("caution", "메모리·캐시", "원인 분리 필요", "캐시에 없는 segment를 디스크에서 자주 읽음",
                "ES major page fault p95 {}/s · 읽기 지연 {}".format(fmt(mj95, 0), SEV_LABEL[r_sev]),
                "mmap으로 연 segment가 page cache에 없을 때마다 major fault가 나고 디스크 읽기가 발생합니다. 읽기 지연과 함께 높으면 캐시 부족형 병목입니다.",
                "RAM 증설(heap은 그대로), 노드 증설, 콜드 데이터의 frozen/warm 분리를 검토하세요.", "/proc/<pid>/stat majflt")
    dirty = [r.get("dirty_mb") for r in sysr if r.get("dirty_mb") is not None]
    if dirty and mem_total_mb and vmax(dirty) > mem_total_mb * 0.10 and SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]:
        mem_sevs.append("caution")
        add("caution", "메모리·캐시", "서버 담당자", "dirty page가 크게 쌓였다 한꺼번에 쓰이는 패턴",
            "Dirty 최대 {} (RAM의 {:.0f}%) · 쓰기 지연 {}".format(fmt(vmax(dirty), 0, "MB"), 100 * vmax(dirty) / mem_total_mb, SEV_LABEL[w_sev]),
            "비율 기반 기본값(dirty_ratio 20%)은 RAM이 클수록 수 GB를 모았다가 한 번에 씁니다. 그 순간 fsync가 줄 서며 쓰기 지연이 튑니다.",
            "vm.dirty_background_bytes / vm.dirty_bytes 로 상한을 바이트 단위로 낮추는 것을 검토하세요. 운영 영향이 있어 테스트 후 적용해야 하며 Elastic 공식 권고 항목은 아닙니다.",
            "Linux kernel Documentation/admin-guide/sysctl/vm.rst")
    mem_sev = sev_max(*mem_sevs) if mem_sevs else "ok"

    # ═════════════ 6. 설정 ═════════════
    cfg_sevs = []
    # readahead — Elastic 공식 권고 128KiB
    ra_bad = []
    for d in sorted(set(phys + logical)):
        ra = num(topo.attr.get(topo.whole(d), {}).get("queue/read_ahead_kb"))
        if ra is not None and ra > 128:
            ra_bad.append("{}={}KB".format(d, int(ra)))
    if ra_bad:
        s = "warn" if any(int(x.split("=")[1][:-2]) >= 1024 for x in ra_bad) else "caution"
        cfg_sevs.append(s)
        add(s, "설정", "서버 담당자", "readahead가 Elastic 권고값(128KiB)보다 큼", ", ".join(ra_bad),
            "검색은 무작위 읽기가 많아 readahead가 크면 필요 없는 데이터까지 읽어 page cache를 밀어냅니다. LVM·dm 장치는 수 MB로 잡히는 경우가 있습니다.",
            "blockdev --setra 256 /dev/<장치> (512B 섹터 단위 → 128KiB) 로 즉시 적용 가능, udev 규칙으로 영구화. LVM이면 dm 장치에도 적용하세요.",
            "[Elastic 공식] Tune for search speed — LVM·software RAID·dm-crypt에서 readahead가 수 MiB로 커질 수 있으며 128KiB 권장 (blockdev --setra 256)")
    # scheduler
    sch_bad = []
    for d in phys:
        sch = topo.attr.get(d, {}).get("queue/scheduler", "")
        m = re.search(r'\[(\S+)\]', sch)
        cur = m.group(1) if m else sch.strip()
        if cur in ("cfq", "bfq"):
            sch_bad.append("{}={}".format(d, cur))
    if sch_bad:
        cfg_sevs.append("caution")
        add("caution", "설정", "서버 담당자", "I/O 스케줄러가 가상 디스크에 불리함", ", ".join(sch_bad),
            "cfq/bfq는 프로세스 간 공평 분배에 시간을 씁니다. 스케줄링은 vSAN이 이미 하므로 VM에서는 가볍게 두는 편이 낫습니다. none과 mq-deadline의 차이는 작습니다.",
            "mq-deadline 또는 none으로 변경 (echo mq-deadline > /sys/block/<장치>/queue/scheduler, udev 규칙으로 영구화).",
            "Red Hat 'Monitoring and managing system status and performance' — Setting the disk scheduler")
    # max_map_count
    mmc = num(sysctl.get("vm.max_map_count"))
    if mmc is not None and mmc < 262144:
        cfg_sevs.append("crit")
        add("crit", "설정", "서버 담당자", "vm.max_map_count가 ES 최소 요구치 미만", "현재 {}".format(int(mmc)),
            "ES는 segment를 mmap으로 엽니다. 매핑 개수 한도가 부족하면 운영 모드에서 기동이 거부되거나 mmap 실패가 납니다.",
            "sysctl -w vm.max_map_count=1048576 + /etc/sysctl.d/ 에 영구 설정.", "[Elastic 공식] Bootstrap checks — Maximum map count (최소 262144, 권장 1048576)")
    elif mmc is not None and mmc < 1048576:
        cfg_sevs.append("info")
        add("info", "설정", "서버 담당자", "vm.max_map_count 최소치는 충족, 권장치 미만", "현재 {} (권장 1048576)".format(int(mmc)),
            "샤드·segment가 많은 노드일수록 매핑 수가 늘어납니다.", "여유 있게 1048576으로 올려 두는 것을 권장합니다.",
            "Elastic 문서 'Bootstrap checks — Maximum map count'")
    # THP
    thp = sysctl.get("thp.enabled", "")
    m = re.search(r'\[(\w+)\]', thp)
    thp_cur = m.group(1) if m else thp
    if thp_cur == "always":
        cfg_sevs.append("info")
        add("info", "설정", "서버 담당자", "THP(Transparent HugePage) = always", thp,
            "THP 압축(compaction)이 JVM 동작과 겹쳐 순간 지연을 만드는 사례가 있어 DB 계열에서 흔히 끕니다. 다만 Elastic 공식 필수 항목은 아니며 디스크 I/O 직접 요인도 아닙니다.",
            "순간 지연 문제가 있을 때 madvise 또는 never로 바꿔 비교해 보세요.", "Red Hat / DB 벤더 운영 관행 (Elastic 필수 항목 아님)")
    # 파일시스템·마운트
    for pm in path_map:
        m = pm["mount"]
        if not m:
            continue
        opts = m["opts"].split(",")
        if m["fs"] not in ("xfs", "ext4"):
            s = "crit" if m["fs"] in ("nfs", "nfs4", "cifs", "tmpfs") else "caution"
            cfg_sevs.append(s)
            add(s, "설정", "서버 담당자", "ES data 경로 파일시스템이 {}".format(m["fs"]), "{} on {}".format(m["mnt"], m["src"]),
                "ES는 로컬 블록 장치 위의 xfs/ext4를 전제로 합니다. 네트워크 파일시스템은 잠금·fsync 동작이 달라 위험합니다.",
                "xfs 또는 ext4 로컬 볼륨으로 이전하세요.", "[Elastic 공식] Hardware — 로컬 스토리지 권장, 원격 파일시스템 회피")
        if "strictatime" in opts:
            cfg_sevs.append("caution")
            add("caution", "설정", "서버 담당자", "strictatime 마운트 — 읽을 때마다 메타데이터 쓰기", m["opts"],
                "파일을 읽을 때마다 접근 시각을 기록합니다. 기본값 relatime은 이 비용이 거의 없습니다.", "noatime 또는 기본 relatime으로 변경.", "mount(8)")
        if "discard" in opts:
            cfg_sevs.append("caution")
            add("caution", "설정", "서버 담당자", "online discard 마운트 옵션 사용", m["opts"],
                "삭제 때마다 TRIM을 보내 쓰기 지연을 늘릴 수 있습니다. vSAN에서 Guest TRIM은 클러스터 설정이 켜져 있어야 공간 회수로 이어집니다.",
                "discard를 빼고 fstrim.timer(주 1회)로 대체하세요. vSAN Guest TRIM/UNMAP 사용 여부는 VMware 관리자에게 확인.", "mount(8), vSAN Guest TRIM/UNMAP 문서")
    # 파티션 정렬
    for d in phys:
        for part, (parent, start) in topo.parts.items():
            if parent == d and start % 2048 != 0:
                cfg_sevs.append("caution")
                add("caution", "설정", "서버 담당자", "파티션 시작 위치가 1MiB 정렬이 아님", "{} start sector {}".format(part, start),
                    "정렬이 어긋나면 I/O 한 번이 하부 블록 두 개에 걸쳐 불필요한 추가 I/O가 생깁니다.",
                    "신규 구성 시 1MiB(2048 섹터) 정렬로 파티션을 만드세요. 운영 중 변경은 데이터 이전이 필요합니다.", "parted/fdisk 정렬 가이드")
    # SCSI timeout — vSAN failover 대비
    to_bad = ["{}={}s".format(d, topo.attr[d]["device/timeout"]) for d in phys
              if num(topo.attr.get(d, {}).get("device/timeout"), 999) < 60]
    if to_bad:
        cfg_sevs.append("warn")
        add("warn", "설정", "서버 담당자", "SCSI 명령 타임아웃이 짧음 — vSAN 순간 정지 시 I/O 오류 위험", ", ".join(to_bad),
            "vSAN 호스트 장애나 경로 전환 중에는 I/O가 수십 초 멈출 수 있습니다. 타임아웃이 짧으면 Guest가 이를 오류로 처리해 파일시스템이 읽기전용으로 바뀔 수 있습니다.",
            "open-vm-tools를 설치하면 udev 규칙으로 180초가 설정됩니다. 설치 여부와 /sys/block/<장치>/device/timeout 값을 확인하세요.",
            "open-vm-tools udev 규칙 (99-vmware-scsi-udev.rules)")
    # cgroup I/O 제한
    cg = rd(S, "es_cgroup_io").strip()
    if cg and "max" in cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg):
        cfg_sevs.append("warn")
        add("warn", "설정", "서버 담당자", "ES 프로세스에 cgroup I/O 제한이 걸려 있음", cg[:200],
            "커널이 ES의 디스크 사용량을 인위적으로 제한합니다. 스토리지가 빨라도 이 값 이상은 못 냅니다.", "의도된 설정인지 확인하고 해제하세요.", "cgroup v2 io.max")
    # limits
    lim = rd(S, "es_limits")
    def limv(name):
        m = re.search(r'^' + re.escape(name) + r'\s+(\S+)\s+(\S+)', lim, re.M)
        return m.group(1) if m else None
    nofile = limv("Max open files")
    if nofile and nofile != "unlimited" and num(nofile, 0) < 65535:
        cfg_sevs.append("crit")
        add("crit", "설정", "서버 담당자", "ES 파일 핸들 한도 부족", "Max open files {}".format(nofile),
            "segment 파일·translog·소켓을 모두 파일 핸들로 씁니다. 부족하면 'Too many open files'로 인덱싱이 실패합니다.",
            "LimitNOFILE=65535 이상 (systemd override).", "[Elastic 공식] File descriptors")
    fdc = num(rd(S, "es_fdcount").strip())
    if fdc and nofile and nofile != "unlimited" and fdc > 0.8 * num(nofile, 1):
        cfg_sevs.append("warn")
        add("warn", "설정", "서버 담당자", "열린 파일 핸들이 한도의 80% 초과", "{} / {}".format(int(fdc), nofile),
            "샤드·segment 수가 많아 한도에 근접했습니다.", "한도 상향과 함께 샤드 수 정리를 검토하세요.", "/proc/<pid>/fd")
    # 디스크 용량 · watermark
    wm = {}
    for grp in ("defaults", "persistent", "transient"):
        for k, v in ((csettings or {}).get(grp) or {}).items():
            wm[k] = v
    hi = wm.get("cluster.routing.allocation.disk.watermark.high", "90%")
    hi_pct = num(str(hi).rstrip("%")) if str(hi).endswith("%") else None
    for line in rd(S, "df").splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and any(pm["mount"] and pm["mount"]["mnt"] == p[5] for pm in path_map):
            use = num(p[4].rstrip("%"), 0)
            lim_pct = hi_pct or 90
            if use >= lim_pct:
                cfg_sevs.append("crit")
                add("crit", "설정", "ES 설정", "data 디스크 사용률이 high watermark 이상", "{} {}% (high {})".format(p[5], int(use), hi),
                    "high watermark를 넘으면 ES가 샤드를 다른 노드로 옮기기 시작해 대량 복사 I/O가 생깁니다.", "공간 확보(ILM 정책 점검, 오래된 인덱스 정리) 또는 용량 증설.",
                    "[Elastic 공식] Disk-based shard allocation")
            elif use >= lim_pct - 10:
                cfg_sevs.append("caution")
                add("caution", "설정", "ES 설정", "data 디스크 사용률이 watermark에 근접", "{} {}% (high {})".format(p[5], int(use), hi),
                    "여유가 10%p 이내입니다. 인덱싱이 몰리면 곧 샤드 이동이 시작됩니다.", "용량 추이를 보고 미리 대응하세요.", "[Elastic 공식] Disk-based shard allocation")
    # OS와 data가 같은 장치/컨트롤러
    root = mount_for("/", mounts)
    root_phys = topo.physical(topo.kname(root["src"])) if root else []
    share_dev = sorted(set(root_phys) & set(phys))
    if share_dev and not dev_guess:
        cfg_sevs.append("info")
        add("info", "설정", "VMware 관리자", "OS와 ES data가 같은 가상 디스크를 사용", ", ".join(share_dev),
            "로그 쓰기, 패키지 작업 등 OS I/O가 ES I/O와 같은 큐를 나눠 씁니다.", "ES data 전용 VMDK 분리를 권장합니다.", "VMware DB-on-vSAN 구성 권고")
    else:
        hosts = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
        rhosts = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
        if hosts and set(hosts) <= set(rhosts):
            add("info", "설정", "VMware 관리자", "OS 디스크와 data 디스크가 같은 가상 SCSI 컨트롤러에 연결", "controller {}".format(", ".join(hosts)),
                "컨트롤러 하나의 큐를 함께 씁니다. 부하가 높을 때 컨트롤러를 나누면 큐가 분산됩니다.",
                "ES data VMDK를 별도 PVSCSI 컨트롤러(SCSI 1:x 등)에 연결하는 것을 권장합니다.", "VMware 'Performance Best Practices for vSphere' — PVSCSI")
    # LVM linear over multiple PVs
    for d in logical:
        if d.startswith("dm-") and len(topo.slaves.get(d, [])) > 1:
            tbl = rd(S, "dmsetup_table")
            dmn = topo.attr.get(d, {}).get("dm/name", "")
            lines = [l for l in tbl.splitlines() if l.startswith(dmn + ":")]
            if lines and all(" linear " in l for l in lines):
                cfg_sevs.append("info")
                add("info", "설정", "서버 담당자", "여러 디스크를 LVM linear로 이어 붙임 (stripe 아님)",
                    "{} ← {}".format(dmn, ", ".join(topo.slaves[d])),
                    "linear는 앞 디스크를 채운 뒤 다음 디스크를 씁니다. I/O가 한 디스크에 몰려 병렬 효과가 거의 없습니다.",
                    "신규 구성 시 lvcreate -i <디스크 수> -I 256k 로 stripe 구성을 권장합니다.", "LVM lvcreate(8)")
    # iostats 비활성 — 이러면 측정 자체가 무의미
    for d in phys:
        if topo.attr.get(d, {}).get("queue/iostats") == "0":
            cfg_sevs.append("warn")
            add("warn", "설정", "서버 담당자", "블록 장치 I/O 통계 수집이 꺼져 있음 ({})".format(d),
                "/sys/block/{}/queue/iostats = 0".format(d),
                "커널이 이 장치의 I/O 통계를 쌓지 않습니다. iostat·sar를 포함해 어떤 도구도 이 디스크의 응답시간을 볼 수 없습니다. 이 리포트의 해당 장치 수치도 신뢰할 수 없습니다.",
                "iostats를 1로 되돌린 뒤 재측정하세요 (성능 영향은 무시할 수준).", "Linux block layer sysfs")
    # writeback throttling — 커널이 쓰기를 의도적으로 억제
    wbt = [(d, num(topo.attr.get(d, {}).get("queue/wbt_lat_usec"))) for d in phys]
    wbt_on = [(d, v) for d, v in wbt if v and v > 0]
    if wbt_on and SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]:
        cfg_sevs.append("info")
        add("info", "설정", "서버 담당자", "writeback throttling이 켜져 있고 쓰기 지연도 높음",
            ", ".join("{} wbt_lat_usec={}".format(d, int(v)) for d, v in wbt_on),
            "커널이 읽기 지연을 지키려고 쓰기를 스스로 늦추는 기능입니다. 기본 활성이며 대개 유익하지만, 쓰기 지연이 이미 높은 상태에서는 어느 쪽이 원인인지 헷갈리게 만듭니다.",
            "원인 분리가 필요하면 점검 시간에 이 값을 조정해 비교해 볼 수 있습니다. 기본값 유지가 무난하므로 우선순위는 낮습니다.",
            "Linux block layer writeback throttling (wbt)")
    # 스토리지 인터럽트가 특정 vCPU에 몰리는지
    i0, i1 = rd(S, "interrupts_start"), rd(S, "interrupts_end")
    if i0 and i1:
        def irq_map(t):
            out = {}
            for line in t.splitlines()[1:]:
                p_ = line.split()
                if len(p_) > 2 and re.match(r'^\d+:$', p_[0]) and re.search(r'(pvscsi|virtio\d+-req|nvme|ahci|mpt)', line, re.I):
                    vals = []
                    for x in p_[1:]:
                        if x.isdigit():
                            vals.append(int(x))
                        else:
                            break
                    out[p_[0]] = vals
            return out
        m0, m1 = irq_map(i0), irq_map(i1)
        tot = None
        for k in m1:
            if k in m0 and len(m0[k]) == len(m1[k]):
                d_ = [b - a for a, b in zip(m0[k], m1[k])]
                tot = d_ if tot is None else [x + y for x, y in zip(tot, d_)]
        if tot and sum(tot) > 1000 and len(tot) > 1:
            share = 100.0 * max(tot) / sum(tot)
            if share > 70:
                cfg_sevs.append("caution")
                add("caution", "설정", "서버 담당자", "스토리지 인터럽트가 vCPU 한 개에 몰림",
                    "상위 vCPU가 전체 스토리지 인터럽트의 {:.0f}% 처리 (vCPU {}개 중)".format(share, len(tot)),
                    "I/O 완료 처리가 한 코어에 집중되면 그 코어가 한계일 때 전체 IOPS가 거기서 막힙니다. 디스크는 여유가 있는데 더 안 나오는 상황이 됩니다.",
                    "irqbalance 동작 여부를 확인하세요. PVSCSI·vmxnet3는 다중 큐를 지원하므로 큐 수와 인터럽트 분산 설정을 VMware 관리자와 함께 점검합니다.",
                    "/proc/interrupts")
    # mmap 여유 (ES는 segment를 mmap으로 연다)
    mapc = num(rd(S, "es_mapcount").strip())
    if mapc and mmc:
        use = 100.0 * mapc / mmc
        if use > 60:
            cfg_sevs.append("warn" if use > 80 else "caution")
            add("warn" if use > 80 else "caution", "설정", "서버 담당자", "mmap 사용량이 한도에 근접",
                "현재 매핑 {:,}개 / 한도 {:,}개 ({:.0f}%)".format(int(mapc), int(mmc), use),
                "한도에 닿으면 새 segment를 열지 못해 인덱싱·검색이 실패합니다. 샤드와 segment가 늘수록 함께 증가합니다.",
                "vm.max_map_count를 1048576으로 올리고, 동시에 샤드·segment 수가 과한지 점검하세요.",
                "Elastic 문서 'Bootstrap checks — Maximum map count'")
    # ES 로그에 남은 디스크 관련 메시지
    eslog = [l for l in rd(S, "es_log").splitlines() if l.strip() and not l.startswith("#FILE")]
    if eslog:
        pats = [("인덱싱 스로틀", r'now throttling indexing'), ("디스크 watermark", r'disk watermark|flood stage'),
                ("flush 실패", r'failed to flush|failed to write'), ("파일 핸들 부족", r'Too many open files'),
                ("GC 지연", r'overhead, spent|\[gc\]\['), ("translog 문제", r'translog')]
        hit = [(n, sum(1 for l in eslog if re.search(pt, l, re.I))) for n, pt in pats]
        hit = [(n, c) for n, c in hit if c]
        if hit:
            sev = "warn" if any(n in ("인덱싱 스로틀", "디스크 watermark", "flush 실패", "파일 핸들 부족") for n, _ in hit) else "info"
            cfg_sevs.append("info")
            add(sev, "ES 영향", "원인 분리 필요", "ES 로그에 디스크 관련 메시지 기록",
                " · ".join("{} {}건".format(n, c) for n, c in hit),
                "ES가 직접 남긴 기록이라 가장 확실한 증거입니다. 'now throttling indexing'은 merge가 밀려 ES가 스스로 인덱싱을 늦춘 상태를 뜻합니다.",
                "리포트 부록의 로그 원문에서 발생 시각을 확인하고, 같은 시각의 디스크 지표와 대조하세요.",
                "Elasticsearch 서버 로그 (최근 7일)")

    cfg_sev = sev_max(*cfg_sevs) if cfg_sevs else "ok"

    # ═════════════ 7. VMware 자원 ═════════════
    vm_sevs = []
    is_vmware = "vmware" in (virt.get("detect_virt", "") + virt.get("sys_vendor", "")).lower()
    drivers = sorted(set(topo.hostdrv.get(topo.scsihost.get(d), "?") for d in phys if topo.scsihost.get(d)))
    if any(dr.startswith("nvme") for dr in phys):
        drivers.append("nvme")
    if is_vmware:
        if any(dr in ("mptspi", "mptsas", "mpt2sas", "mpt3sas", "ata_piix", "ahci") for dr in drivers):
            vm_sevs.append("caution")
            add("caution", "VMware 자원", "VMware 관리자", "ES data 디스크가 LSI Logic/SATA 가상 컨트롤러에 연결", ", ".join(drivers),
                "VMware는 I/O가 많은 워크로드에 PVSCSI를 권장합니다. 같은 I/O를 더 적은 CPU로 처리하고 큐도 깊게 쓸 수 있습니다.",
                "VM 정지 후 ES data VMDK를 PVSCSI 컨트롤러로 옮기는 작업을 VMware 관리자에게 요청하세요 (Guest 드라이버 vmw_pvscsi 필요).",
                "[VMware 공식] KB 1010398, Performance Best Practices for vSphere")
        tools = virt.get("tools_version", "absent")
        if tools == "absent":
            vm_sevs.append("caution")
            add("caution", "VMware 자원", "서버 담당자", "open-vm-tools 미설치", "vmware-toolbox-cmd 없음",
                "SCSI 타임아웃 자동 설정(180초)과 balloon·메모리 예약 상태 확인이 모두 이 패키지에 의존합니다.",
                "open-vm-tools 설치 (RHEL/Ubuntu 기본 저장소).", "VMware open-vm-tools")
        def mb(x):
            m = re.search(r'(-?\d+)', x or "")
            return int(m.group(1)) if m else None
        balloon, hswap = mb(virt.get("stat_balloon")), mb(virt.get("stat_swap"))
        memres, memlim = mb(virt.get("stat_memres")), mb(virt.get("stat_memlimit"))
        cpulim = mb(virt.get("stat_cpulimit"))
        if balloon:
            vm_sevs.append("warn")
            add("warn", "VMware 자원", "VMware 관리자", "balloon이 이 VM의 메모리를 회수 중", "{} MB".format(balloon),
                "하이퍼바이저가 메모리를 빼 가면 가장 먼저 page cache가 줄어 검색이 디스크를 더 읽게 되고, 심하면 swap까지 갑니다.",
                "이 VM에 메모리 예약(reservation) 100%를 요청하세요. ES 같은 메모리 의존 워크로드의 표준 권고입니다.", "VMware memory management 문서")
        if hswap:
            vm_sevs.append("crit")
            add("crit", "VMware 자원", "VMware 관리자", "하이퍼바이저가 이 VM 메모리를 디스크로 swap 중", "{} MB".format(hswap),
                "VM은 모르는 사이에 메모리 접근이 디스크 속도로 떨어집니다. 모든 지표가 불규칙하게 나빠집니다.",
                "즉시 메모리 예약 100% 설정 또는 호스트 메모리 과할당 해소를 요청하세요.", "VMware memory management 문서")
        if memres is not None and mem_total_mb and memres < mem_total_mb * 0.95:
            vm_sevs.append("caution")
            add("caution", "VMware 자원", "VMware 관리자", "메모리 예약이 VM 메모리보다 작음",
                "예약 {} MB / VM 메모리 약 {} MB".format(memres, int(mem_total_mb)),
                "예약되지 않은 만큼은 호스트 메모리가 부족할 때 balloon·swap 대상이 됩니다. 지금은 괜찮아도 다른 VM이 몰리면 ES가 먼저 영향을 받습니다.",
                "ES VM은 메모리 예약 100%(Reserve all guest memory)를 권장합니다.", "VMware 'Performance Best Practices for vSphere' — memory")
        if memlim is not None and 0 < memlim < mem_total_mb * 0.95 and memlim < 4000000:
            vm_sevs.append("warn")
            add("warn", "VMware 자원", "VMware 관리자", "VM에 메모리 한도(limit)가 설정됨", "{} MB".format(memlim),
                "한도를 넘는 메모리는 항상 balloon·swap으로 처리됩니다.", "메모리 limit 해제를 요청하세요.", "VMware resource management 문서")
        if cpulim is not None and 0 < cpulim < 4000000:
            vm_sevs.append("caution")
            add("caution", "VMware 자원", "VMware 관리자", "VM에 CPU 한도(limit)가 설정됨", "{} MHz".format(cpulim),
                "I/O 완료 처리와 vSAN 클라이언트 동작에도 CPU가 필요합니다.", "CPU limit 해제를 요청하세요.", "VMware resource management 문서")
    steal = [r.get("steal") for r in sysr if r.get("steal") is not None]
    st95 = pctl(steal, .95)
    if st95 is not None and st95 >= 5:
        s = "warn" if st95 >= 10 else "caution"
        vm_sevs.append(s)
        add(s, "VMware 자원", "VMware 관리자", "CPU steal — 호스트 CPU 경합", "p95 {} · 최대 {}".format(fmt(st95, 1, "%"), fmt(vmax(steal), 1, "%")),
            "VM이 실행하려 할 때 호스트가 CPU를 내주지 못한 시간입니다. vSAN은 호스트 CPU로 동작하므로 경합이 크면 I/O 처리도 늦어집니다.",
            "호스트 과할당 여부, VM CPU ready 값을 VMware 관리자에게 확인 요청하세요.", "Linux /proc/stat steal")
    vm_sev = sev_max(*vm_sevs) if vm_sevs else ("ok" if is_vmware else "na")

    # ═════════════ 8. 네트워크 (보조) ═════════════
    net_sev = "ok"
    netinfo = [l.split("|") for l in rd(S, "net").splitlines() if l.startswith("IF|")]
    for p in netinfo:
        if is_vmware and "e1000" in p[2]:
            net_sev = sev_max(net_sev, "caution")
            add("caution", "네트워크", "VMware 관리자", "ES NIC가 e1000 에뮬레이션 사용", "{} {}".format(p[1], p[2]),
                "ES 복제본 쓰기·샤드 복구는 ES 노드 간 네트워크로 오갑니다. 에뮬레이션 NIC는 CPU를 더 쓰고 처리량이 낮습니다.",
                "VMXNET3로 변경을 요청하세요.", "[VMware 공식] KB 1001805 (VMXNET3)")
    drops = {k: v for k, v in nets.items() if (v["rx_drop"] + v["tx_drop"] + v["rx_err"] + v["tx_err"]) > 0}
    if drops:
        net_sev = sev_max(net_sev, "caution")
        add("caution", "네트워크", "서버 담당자", "측정 중 NIC 패킷 드롭/오류 발생",
            ", ".join("{} drop {} err {}".format(k, v["rx_drop"] + v["tx_drop"], v["rx_err"] + v["tx_err"]) for k, v in drops.items()),
            "ES 복제·복구 트래픽이 재전송으로 늦어지면 복제본 쓰기가 끝나지 않아 인덱싱 응답이 늦어집니다(디스크 문제처럼 보일 수 있음).",
            "ethtool -g로 RX ring 최대값 확인 후 상향 검토, 호스트 쪽 vmxnet3 큐 상태는 VMware 관리자에게 확인.", "ethtool(8)")
    rt = [r.get("retrans_pct") for r in sysr if r.get("retrans_pct") is not None]
    if rt and pctl(rt, .95) >= 1.0:
        net_sev = sev_max(net_sev, "caution")
        add("caution", "네트워크", "원인 분리 필요", "TCP 재전송률이 높음", "p95 {}".format(fmt(pctl(rt, .95), 2, "%")),
            "ES 노드 간 통신 품질이 떨어져 복제본 응답이 늦어질 수 있습니다.", "네트워크 경로·NIC 설정 점검.", "/proc/net/snmp")
    add("info", "네트워크", "참고", "vSAN 네트워크는 Guest에서 보이지 않음",
        "Guest NIC에는 ES 트래픽만 흐름. vSAN 복제 트래픽은 ESXi vmkernel 포트로 흐름",
        "vSAN 네트워크 지연은 Guest에서 '쓰기 응답시간 증가'로만 간접 관측됩니다. 그래서 네트워크는 보조 지표로만 씁니다.",
        "쓰기 지연이 높으면 VMware 관리자에게 vSAN 네트워크(전용 대역, 25GbE 이상 권장, 재전송·지연)를 확인 요청하세요.", "[VMware 공식] Troubleshooting vSAN Performance — 2% 패킷 손실로 스토리지 성능 32% 저하, vSwitch 드롭 0.0001% 이하 권고")

    # ═════════════ 9. 과거 이력 (sar) ═════════════
    want = set(phys) | set(logical) | set(topo.attr.get(d, {}).get("dm/name", "") for d in logical)
    sar_days = parse_sar(rd(S, "sar_d"), want)
    hist = []
    for day, rec in sorted(sar_days.items()):
        if rec["await"]:
            hist.append((day, pctl(rec["await"], .95), rec["worst"][0], rec["worst"][1], vmax(rec["util"])))
    if hist:
        worst = max(hist, key=lambda x: x[2] or 0)
        s = grade(worst[2], th)
        if SEV_ORDER.get(s, 0) >= SEV_ORDER["warn"]:
            add("caution", "지연", "원인 분리 필요", "과거 이력에 응답시간 급등 구간 존재 (sar)",
                "{} {} 에 {} (10분 평균)".format(worst[0], worst[3], fmt(worst[2], 1, "ms")),
                "이번 측정 창 밖에서 일어난 피크입니다. sar는 10분 평균이라 실제 순간값은 더 높았을 수 있습니다.",
                "해당 시각의 배치·스냅샷·백업·vSAN 작업 이력을 대조하고, 같은 시간대에 이 도구로 재측정하세요.", "sysstat sar 이력")

    # ═════════════ 10. 벤치(선택) → 여유율 ═════════════
    bench = load_bench(bench_dir)
    headroom = []
    if bench:
        def hr(label, obs, cap, unit):
            if obs is not None and cap:
                headroom.append((label, obs, cap, 100.0 * obs / cap, unit))
        b = bench
        hr("무작위 읽기 IOPS (검색)", A["rs_p95"], (b.get("randread_4k") or {}).get("r_iops"), "IOPS")
        hr("순차 쓰기 처리량 (flush·merge)", A["wmb_p95"], (b.get("seqwrite_1m") or {}).get("w_mbs"), "MB/s")
        hr("순차 읽기 처리량 (merge·복구)", A["rmb_p95"], (b.get("seqread_1m") or {}).get("r_mbs"), "MB/s")
        hr("혼합 쓰기 IOPS", A["ws_p95"], (b.get("randrw_16k") or {}).get("w_iops"), "IOPS")
        worst_hr = max([h[3] for h in headroom] or [0])
        if worst_hr >= 60:
            s = "warn" if worst_hr >= 80 else "caution"
            add(s, "포화", "원인 분리 필요", "측정 최대 능력 대비 사용률 {:.0f}%".format(worst_hr),
                ", ".join("{} {:.0f}%".format(h[0], h[3]) for h in headroom),
                "평상시 피크가 이미 최대 능력의 60~80%를 쓰고 있으면 merge·복구가 겹칠 때 여유가 없습니다.",
                "피크 증가 추세를 보고 노드·VMDK 증설을 계획하세요.", "fio 벤치 결과 (es_disk_bench.sh)")


    # ═════════════ Best practice 대조표 (통과 항목 포함 전체) ═════════════
    BP = []
    def bp(cat, item, rec, cur, st, src):
        BP.append((cat, item, rec, cur, st, src))
    es_devs = sorted(set(phys + logical))
    ras = [(d, num(topo.attr.get(topo.whole(d), {}).get("queue/read_ahead_kb"))) for d in es_devs]
    ras = [(d, v) for d, v in ras if v is not None]
    if ras:
        mx = max(v for _, v in ras)
        bp("OS·블록 장치", "readahead", "128KiB 이하", ", ".join("{} {}KB".format(d, int(v)) for d, v in ras),
           "ok" if mx <= 128 else ("warn" if mx >= 1024 else "caution"), "Elastic 공식")
    schs = []
    for d in phys:
        m_ = re.search(r'\[(\S+)\]', topo.attr.get(d, {}).get("queue/scheduler", ""))
        if m_:
            schs.append((d, m_.group(1)))
    if schs:
        bp("OS·블록 장치", "I/O 스케줄러", "mq-deadline 또는 none", ", ".join("{} {}".format(d, v) for d, v in schs),
           "caution" if any(v in ("cfq", "bfq") for _, v in schs) else "ok", "[Red Hat 공식] Setting the disk scheduler")
    ios = [topo.attr.get(d, {}).get("queue/iostats") for d in phys]
    if any(ios):
        bp("OS·블록 장치", "I/O 통계 수집(iostats)", "1 (켜짐)", ", ".join(x or "-" for x in ios), "warn" if "0" in ios else "ok", "커널 block layer")
    tos = [num(topo.attr.get(d, {}).get("device/timeout")) for d in phys]
    tos = [t for t in tos if t is not None]
    bp("OS·블록 장치", "SCSI 명령 타임아웃", "60초 이상 (open-vm-tools 180초)", ", ".join("{}s".format(int(t)) for t in tos) or "해당 없음",
       ("warn" if min(tos) < 60 else "ok") if tos else "na", "open-vm-tools udev 규칙")
    qds = [topo.attr.get(d, {}).get("device/queue_depth") for d in phys if topo.attr.get(d, {}).get("device/queue_depth")]
    bp("OS·블록 장치", "가상 디스크 queue_depth", "기본값 유지, 큐 포화가 확인될 때만 상향(254)", ", ".join(qds) or "-",
       ("warn" if (qratio or 0) >= 0.8 else "ok") if qds else "na", "VMware KB 2053145")
    mis = [(pt, st) for pt, (par, st) in topo.parts.items() if par in phys and st % 2048]
    bp("OS·블록 장치", "파티션 정렬", "1MiB(2048 섹터) 단위", "어긋남: " + ", ".join(p_ for p_, _ in mis) if mis else "정렬됨 / 파티션 없음",
       "caution" if mis else "ok", "parted 정렬 가이드")
    for pm in path_map:
        m_ = pm["mount"]
        if not m_:
            continue
        o = m_["opts"].split(",")
        bp("파일시스템", "파일시스템 종류 ({})".format(m_["mnt"]), "xfs 또는 ext4 (로컬 블록 장치)", m_["fs"],
           "ok" if m_["fs"] in ("xfs", "ext4") else "crit", "Elastic 공식")
        bp("파일시스템", "atime 기록 ({})".format(m_["mnt"]), "noatime 또는 relatime", next((x for x in o if "atime" in x), "relatime(기본)"),
           "caution" if "strictatime" in o else "ok", "mount(8)")
        bp("파일시스템", "online discard ({})".format(m_["mnt"]), "끔 (fstrim.timer로 대체)", "켜짐" if "discard" in o else "꺼짐",
           "caution" if "discard" in o else "ok", "mount(8)")
    fst = rd(S, "fstrim").split()
    bp("파일시스템", "fstrim.timer", "주기 실행 권장 (vSAN Guest TRIM 사용 시 의미)", fst[0] if fst else "미확인", "info", "systemd fstrim.timer")
    bp("커널", "vm.max_map_count", "최소 262144, 권장 1048576", sysctl.get("vm.max_map_count", "-"),
       "crit" if (mmc or 0) < 262144 else ("ok" if (mmc or 0) >= 1048576 else "info"), "Elastic 공식")
    if mapc and mmc:
        bp("커널", "ES mmap 사용량", "한도의 60% 미만", "{:,} / {:,} ({:.0f}%)".format(int(mapc), int(mmc), 100.0 * mapc / mmc),
           "ok" if mapc / mmc < 0.6 else ("warn" if mapc / mmc > 0.8 else "caution"), "Elastic 공식")
    if not swap_on:
        sw_cur, sw_st = "swap 없음", "ok"
    elif mlock is True:
        sw_cur, sw_st = "swap 있음 · memory_lock 켜짐", "ok"
    elif swappiness is not None and swappiness <= 1:
        sw_cur, sw_st = "swap 있음 · swappiness {}".format(sysctl.get("vm.swappiness")), "caution"
    else:
        sw_cur, sw_st = "swap 있음 · swappiness {} · memory_lock {}".format(sysctl.get("vm.swappiness"), "미확인" if mlock is None else "꺼짐"), "warn"
    bp("커널", "swap 차단", "swap 비활성 > memory_lock > swappiness=1", sw_cur, sw_st, "Elastic 공식")
    bp("커널", "Transparent HugePage", "madvise 또는 never (Elastic 필수 아님)", thp_cur or "-", "info" if thp_cur == "always" else "ok", "[참고] DB 벤더 운영 관행 — Elastic 공식 요구사항 아님")
    bp("커널", "dirty page 기준", "기본값 유지, 쓰기 지연 급등 시 바이트 단위 검토",
       "ratio {}/{} · bytes {}/{}".format(sysctl.get("vm.dirty_background_ratio", "-"), sysctl.get("vm.dirty_ratio", "-"),
                                         sysctl.get("vm.dirty_background_bytes", "-"), sysctl.get("vm.dirty_bytes", "-")), "info", "커널 문서")
    bp("커널", "PSI(I/O 압박 지표)", "사용 가능 (RHEL 8은 psi=1)", sysctl.get("psi", "-"), "ok" if sysctl.get("psi") == "available" else "info", "커널 문서")
    bp("ES 프로세스", "파일 핸들 한도", "65535 이상", nofile or "-", ("ok" if (nofile == "unlimited" or num(nofile, 0) >= 65535) else "crit") if nofile else "na", "Elastic 공식")
    if heap_mb and mem_total_mb:
        bp("ES 프로세스", "JVM heap", "RAM의 50% 이하, 약 31GB 이하",
           "{:.1f}GB / RAM {:.1f}GB ({:.0f}%)".format(heap_mb / 1024, mem_total_mb / 1024, 100 * heap_mb / mem_total_mb),
           "caution" if (heap_mb / mem_total_mb > 0.5 or heap_mb > 31 * 1024) else "ok", "Elastic 공식")
    bp("ES 프로세스", "cgroup I/O 제한", "없음", "있음" if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else "없음",
       "warn" if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else "ok", "cgroup v2")
    bp("구성", "OS와 ES data 디스크 분리", "별도 가상 디스크", "같은 디스크: " + ", ".join(share_dev) if share_dev else "분리됨",
       "info" if share_dev else "ok", "[VMware 공식] Performance Best Practices — 워크로드별 컨트롤러 분리")
    dh = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
    rh = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
    if dh and rh:
        shared = sorted(set(dh) & set(rh))
        bp("구성", "ES data 전용 가상 컨트롤러", "OS와 다른 PVSCSI 컨트롤러", "OS와 공유: " + ", ".join(shared) if shared else "분리됨 (" + ", ".join(dh) + ")",
           "info" if shared else "ok", "[VMware 공식] Performance Best Practices for vSphere")
    lin = [d for d in logical if d.startswith("dm-") and len(topo.slaves.get(d, [])) > 1]
    bp("구성", "여러 디스크 묶는 방식", "LVM stripe (-i 디스크 수)", "linear: " + ", ".join(lin) if lin else "단일 디스크 또는 stripe",
       "info" if lin and any(" linear " in l for l in rd(S, "dmsetup_table").splitlines()) else "ok", "LVM lvcreate(8)")
    if is_vmware:
        def mbv(x):
            m_ = re.search(r'(-?\d+)', x or "")
            return int(m_.group(1)) if m_ else None
        bal, hsw = mbv(virt.get("stat_balloon")), mbv(virt.get("stat_swap"))
        mres, mlim, clim = mbv(virt.get("stat_memres")), mbv(virt.get("stat_memlimit")), mbv(virt.get("stat_cpulimit"))
        bp("VMware", "가상 SCSI 컨트롤러", "PVSCSI (또는 vNVMe)", ", ".join(drivers) or "-",
           "caution" if any(x in ("mptspi", "mptsas", "ata_piix", "ahci") for x in drivers) else "ok", "[VMware 공식] Performance Best Practices for vSphere")
        bp("VMware", "open-vm-tools", "설치", virt.get("tools_version", "absent"), "caution" if virt.get("tools_version", "absent") == "absent" else "ok", "VMware")
        if mres is not None:
            bp("VMware", "메모리 예약", "VM 메모리 100%", "{} MB / VM {} MB".format(mres, int(mem_total_mb)),
               "ok" if mres >= mem_total_mb * 0.95 else "caution", "[VMware 공식] Performance Best Practices for vSphere")
        if mlim is not None:
            bp("VMware", "메모리 limit", "설정 안 함", "없음" if (mlim <= 0 or mlim >= 4000000 or mlim >= mem_total_mb) else "{} MB".format(mlim),
               "ok" if (mlim <= 0 or mlim >= 4000000 or mlim >= mem_total_mb) else "warn", "[VMware 공식] vSphere Resource Management")
        if clim is not None:
            bp("VMware", "CPU limit", "설정 안 함", "없음" if (clim <= 0 or clim >= 4000000) else "{} MHz".format(clim),
               "ok" if (clim <= 0 or clim >= 4000000) else "caution", "[VMware 공식] vSphere Resource Management")
        if bal is not None:
            bp("VMware", "balloon 회수 메모리", "0", "{} MB".format(bal), "ok" if not bal else "warn", "VMware")
        if hsw is not None:
            bp("VMware", "하이퍼바이저 swap", "0", "{} MB".format(hsw), "ok" if not hsw else "crit", "VMware")
        nics = [p_[2].replace("driver=", "") for p_ in netinfo]
        if nics:
            bp("VMware", "NIC", "VMXNET3", ", ".join(sorted(set(nics))), "caution" if any("e1000" in n for n in nics) else "ok", "[VMware 공식] KB 1001805 (VMXNET3)")
    for line in rd(S, "df").splitlines()[1:]:
        p_ = line.split()
        if len(p_) >= 6 and any(pm["mount"] and pm["mount"]["mnt"] == p_[5] for pm in path_map):
            use = num(p_[4].rstrip("%"), 0); lp = hi_pct or 90
            bp("ES 용량", "data 디스크 사용률 ({})".format(p_[5]), "high watermark({})보다 10%p 이상 여유".format(hi), "{}%".format(int(use)),
               "crit" if use >= lp else ("caution" if use >= lp - 10 else "ok"), "Elastic 공식")


    # Guest 큐 기준 이론 상한 (Little's law: 동시 처리 수 ÷ 1건 처리 시간). 백엔드가 먼저 막히므로 낙관적 상한
    q_ceiling = None
    tio = sum(r["rio"] + r["wio"] for r in agg); ttk = sum(r["rtk"] + r["wtk"] for r in agg)
    if qd_total and tio >= 200 and ttk > 0:
        q_ceiling = qd_total / ((ttk / float(tio)) / 1000.0)

    if not cluster_dir:
        auto = os.path.join(S, "cluster")
        if os.path.isdir(auto):
            cluster_dir = auto
    CL = analyze_cluster(cluster_dir, add, th)
    IDX = analyze_local_indices(S, cluster_dir)
    me_name = dig(node_i, "name")
    if CL and CL["data_rows"]:
        dr = CL["data_rows"]
        # (교차 1) 샤드 쏠림 ↔ 이 노드의 디스크 부하
        alloc = rjson(cluster_dir, "cat_allocation.json") if cluster_dir else None
        if isinstance(alloc, list) and len(alloc) >= 3:
            sh = [(r.get("node"), num(r.get("shards"), 0)) for r in alloc if r.get("node") and r.get("node") != "UNASSIGNED"]
            vals = [v for _, v in sh if v is not None]
            mine = next((v for n_, v in sh if n_ == me_name), None)
            med = pctl(vals, 0.5)
            if mine and med and mine >= med * 1.3:
                loaded = SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"] or SEV_ORDER.get(sat_sev, 0) >= SEV_ORDER["caution"]
                add("warn" if loaded else "info", "클러스터", "ES 설정" if loaded else "참고",
                    "이 노드에 샤드가 몰려 있음" + (" — 디스크 부하와 함께 나타남" if loaded else ""),
                    "{} 샤드 {:.0f}개 / 클러스터 중앙값 {:.0f}개 · 이 노드 지연 {} · 포화 {}".format(
                        me_name, mine, med, SEV_LABEL.get(lat_sev, lat_sev), SEV_LABEL.get(sat_sev, sat_sev)),
                    "샤드가 많으면 그만큼 segment·merge·translog가 이 노드 디스크에 집중됩니다. 스토리지가 느린 게 아니라 일이 몰린 것일 수 있습니다."
                    if loaded else "지금은 디스크가 버티고 있지만, 부하가 늘면 이 노드가 먼저 막힙니다.",
                    "샤드 배분을 먼저 확인하세요. 특정 인덱스가 이 노드에 몰렸는지 _cat/shards로 보고, 필요하면 재배치 또는 인덱스별 샤드 수를 조정합니다. "
                    "스토리지 교체보다 배분 조정이 먼저입니다.", "_cat/allocation + 로컬 측정")
        # (교차 2) 같은 데이터스토어 동시 저하
        busy = [(r["name"], r["busy"]) for r in dr if r["busy"] is not None]
        if len(busy) >= 3:
            high = [n_ for n_, v in busy if v >= 70]
            if len(high) >= max(2, len(busy) // 2) and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
                add("warn", "클러스터", "VMware 관리자", "여러 노드가 동시에 디스크를 많이 쓰고 있음 — 공용 스토리지 의심",
                    "디스크 busy 70% 이상 {}개 노드 ({}) / 전체 {}개 · 이 노드 지연 {}".format(
                        len(high), ", ".join(high[:6]), len(busy), SEV_LABEL.get(lat_sev, lat_sev)),
                    "노드 하나가 아니라 여러 노드가 같은 시간에 느려졌다면, 각 VM의 문제가 아니라 그 VM들이 공유하는 vSAN 데이터스토어나 호스트 쪽 원인일 가능성이 큽니다. "
                    "ES 노드가 같은 vSAN 클러스터에 있다면 서로의 I/O가 같은 자원을 두고 경쟁합니다.",
                    "VMware 관리자에게 해당 시각의 vSAN 클러스터 단위 지표(디스크 그룹 지연, 캐시 사용률, resync, 네트워크)를 요청하세요. "
                    "ES 노드 VM들이 같은 호스트·데이터스토어에 몰려 있는지도 함께 확인합니다.",
                    "노드 간 동시성 비교 (_nodes/stats fs.io_stats)")
    # (교차 3) 쓰기가 특정 인덱스에 집중
    if IDX:
        tot = sum(r["docs"] for r in IDX) or 1
        top = IDX[0]
        if top["docs"] / float(tot) > 0.5 and top["docs"] > 1000:
            add("info", "ES 영향", "ES 설정", "이 노드 쓰기가 인덱스 하나에 집중",
                "{} 가 이 노드 인덱싱의 {:.0f}% ({:,}건){}".format(
                    top["index"], 100.0 * top["docs"] / tot, int(top["docs"]),
                    " · ILM phase {}".format(top["phase"]) if top["phase"] else ""),
                "쓰기가 한 인덱스에 몰리면 그 인덱스의 샤드가 있는 노드만 디스크를 혹사당합니다. 데이터 스트림이면 현재 write 대상 backing index가 이 노드에 있다는 뜻입니다.",
                "해당 인덱스의 샤드 수와 배치를 확인하세요. 샤드 수가 data 노드 수보다 적으면 쓰기가 일부 노드에만 갑니다.",
                "_nodes/_local/stats/indices?level=indices + _ilm/explain")


    cl_sev = "na"
    if CL:
        aw_ = CL["settings"].get("cluster.routing.allocation.awareness.attributes")
        bp("ES 클러스터", "shard allocation awareness", "ESXi 호스트 단위로 설정", aw_ or "미설정", "ok" if aw_ else "info", "Elastic 공식")
        rmax_ = CL["settings"].get("indices.recovery.max_bytes_per_sec", "40mb")
        bp("ES 클러스터", "indices.recovery.max_bytes_per_sec", "기본 40mb, 복구가 서비스를 방해하면 조정", rmax_, "info", "Elastic 공식")
        cl_sev = sev_max(*[f.sev for f in F if f.dim == "클러스터"]) if any(f.dim == "클러스터" for f in F) else "ok"

    dims = [
        ("지연", lat_sev), ("포화", sat_sev), ("오류", err_sev), ("ES 영향", es_sev),
        ("메모리·캐시", mem_sev), ("설정", cfg_sev), ("VMware 자원", vm_sev), ("네트워크(보조)", net_sev), ("클러스터", cl_sev),
    ]
    disk_rt = sev_max(lat_sev, sat_sev, err_sev)          # 디스크 자체의 측정 결과
    runtime = sev_max(disk_rt, es_sev)
    latent = sev_max(mem_sev, cfg_sev, vm_sev)
    n_act = sum(1 for f in F if SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"])
    disk_clean = SEV_ORDER.get(lat_sev, 0) <= SEV_ORDER["ok"] and not low_load
    if disk_clean:
        for f in F:
            if f.dim == "ES 영향" and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"]:
                f.action += " 이번 측정에서는 디스크 응답시간이 정상이었으므로 디스크 외 원인(CPU, heap, bulk 크기, 샤드 수)을 먼저 보세요."
    if SEV_ORDER.get(disk_rt, 0) >= SEV_ORDER["warn"]:
        verdict = ("bad", "디스크 성능 저하 징후가 있습니다",
                   "측정 구간에 ES가 체감할 수준의 디스크 지연·포화·오류가 관측됐습니다. 아래 '병목 위치' 판정부터 확인하세요.")
    elif SEV_ORDER.get(es_sev, 0) >= SEV_ORDER["warn"] and disk_clean:
        verdict = ("risk", "ES에 처리 지연 신호가 있지만, 디스크 응답은 정상입니다",
                   "인덱싱 스로틀이나 요청 거절이 관측됐지만 같은 시간 디스크 응답시간은 기준 안이었습니다. 디스크보다는 CPU·heap·bulk 크기·샤드 설계 쪽 원인일 가능성이 큽니다.")
    elif low_load and SEV_ORDER.get(runtime, 0) <= SEV_ORDER["caution"]:
        verdict = ("hold", "설정 점검은 완료, 성능 판정은 보류합니다",
                   "측정 시간대의 디스크 부하가 낮아(p95 {} IOPS, {} MB/s) 디스크가 부하를 견디는지 판단할 근거가 부족합니다. "
                   "인덱싱·검색 피크 시간대에 다시 측정하세요.".format(fmt(A["iops_p95"], 0), fmt(A["mb_p95"], 1)))
    elif SEV_ORDER.get(latent, 0) >= SEV_ORDER["warn"] or SEV_ORDER.get(runtime, 0) == SEV_ORDER["caution"]:
        verdict = ("risk", "지금은 버티고 있지만 위험 요인이 있습니다",
                   "측정 구간의 디스크 응답은 심각하지 않지만, 부하가 늘거나 호스트 자원이 부족해지면 문제가 될 설정·구성이 있습니다.")
    else:
        verdict = ("good", "디스크는 정상입니다",
                   "측정 구간의 응답시간·포화·오류가 모두 기준 안이고, ES 운영에 불리한 설정도 발견되지 않았습니다.")

    if CL and CL["data_rows"]:
        busy = [r["busy"] for r in CL["data_rows"] if r["busy"] is not None]
        if len(busy) >= 3:
            med = pctl(busy, 0.5)
            me = next((r["busy"] for r in CL["data_rows"] if r["name"] == dig(node_i, "name")), None)
            if me is not None and med:
                many_high = sum(1 for b in busy if b >= 70) >= max(2, len(busy) // 2)
                if me > med * 1.5:
                    rel = "이 노드만 다른 노드보다 뚜렷하게 높습니다"
                elif many_high:
                    rel = "이 노드만의 문제가 아니라 여러 노드가 동시에 높습니다 — 공용 스토리지를 함께 보세요"
                else:
                    rel = "다른 노드들과 비슷한 수준입니다"
                verdict = (verdict[0], verdict[1],
                           verdict[2] + " 클러스터 비교로는 디스크 사용 시간이 {} (이 노드 {:.0f}% / 중앙값 {:.0f}%).".format(rel, me, med))


    # 우선 조치: ① 병목 위치(누가 움직일지 결정) → ② 실제로 손댈 수 있는 항목을 심각도순
    top = [f for f in F if f.title.startswith("병목 위치") and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"]][:1]
    # 클러스터 교차 판정은 "누가 움직여야 하는가"를 바꾸므로 개별 설정 항목보다 앞에 둔다
    top += [f for f in F if f.dim == "클러스터" and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["warn"] and f not in top][:2 - len(top)]
    act_owner = ("VMware 관리자", "서버 담당자", "ES 설정")
    top += sorted([f for f in F if f.owner in act_owner and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"] and f not in top],
                  key=lambda f: -SEV_ORDER.get(f.sev, 0))[:3 - len(top)]

    # 수집 오버헤드
    ov = rd(base, "self_overhead").split()
    def tsec(x):
        m = re.match(r'(\d+)m([\d.]+)s', x)
        return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0
    cpu_s = sum(tsec(x) for x in ov)
    overhead = {"cpu_s": cpu_s, "dur": dur, "pct_core": (100.0 * cpu_s / dur) if dur else None,
                "out_kb": num(meta.get("output_kb"), 0), "samples": meta.get("samples")}

    return {
        "meta": meta, "storage": storage, "th": th, "es_version": es_version, "is_vmware": is_vmware,
        "os": kv(rd(S, "os-release")).get("PRETTY_NAME", "").strip('"'), "kernel": rd(S, "uname").split()[2] if rd(S, "uname") else "",
        "ncpu": ncpu, "mem_gb": mem_total_mb / 1024.0, "path_map": path_map, "phys": phys, "logical": logical,
        "dev_guess": dev_guess, "A": A, "dev_stats": dev_stats, "log_stats": log_stats, "topo": topo,
        "agg": agg, "sysr": sysr, "findings": F, "dims": dims, "verdict": verdict, "n_act": n_act,
        "low_load": low_load, "klog": klog, "hist": hist, "es_rows": es_rows, "headroom": headroom,
        "bench": bench, "overhead": overhead, "sysctl": sysctl, "virt": virt, "drivers": drivers,
        "CL": CL, "IDX": IDX, "BP": BP, "top": top, "q_ceiling": q_ceiling, "net_sev": net_sev, "nets": nets, "netinfo": netinfo, "dur": dur, "qd_total": qd_total, "qratio": qratio,
    }

# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────
E = lambda s: html.escape(str(s), quote=True)

CSS = """
:root{--ink:#1c2430;--mut:#5d6776;--line:#e2e5ea;--panel:#f5f6f8;--bg:#fff;--nav:#23324a;
--ok:#1d7a4b;--info:#2d5f9e;--caution:#946300;--warn:#bf4a10;--crit:#b3261e;--na:#8b93a1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 "Pretendard","Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR","Segoe UI",sans-serif;
word-break:keep-all;font-variant-numeric:tabular-nums}
.wrap{max-width:1080px;margin:0 auto;padding:36px 24px 72px}
h1{font-size:15px;font-weight:600;color:var(--mut);margin:0 0 4px}
h2{font-size:20px;margin:48px 0 6px;letter-spacing:-.2px}
h2+p.lead{margin:0 0 16px;color:var(--mut);font-size:14px}
.meta{font-size:13px;color:var(--mut)}
.verdict{margin:18px 0 8px;padding:26px 28px;border-radius:6px;border-left:8px solid}
.verdict.good{background:#eef7f1;border-color:var(--ok)}.verdict.risk{background:#fdf6e7;border-color:var(--caution)}
.verdict.bad{background:#fcefec;border-color:var(--crit)}.verdict.hold{background:#eef3fa;border-color:var(--info)}
.verdict b{display:block;font-size:30px;line-height:1.25;letter-spacing:-.6px;margin-bottom:8px}
.verdict span{font-size:15px;color:#3a4453}
.dims{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:0;margin:18px 0 0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.dim{padding:12px 10px;border-right:1px solid var(--line);background:#fff}.dim:last-child{border-right:0}
.dim .n{font-size:12px;color:var(--mut)}.dim .s{font-size:15px;font-weight:700;margin-top:2px}
.dim .bar{height:4px;border-radius:2px;margin-top:8px}
.s-ok{color:var(--ok)}.s-info{color:var(--info)}.s-caution{color:var(--caution)}.s-warn{color:var(--warn)}.s-crit{color:var(--crit)}.s-na{color:var(--na)}
.b-ok{background:var(--ok)}.b-info{background:var(--info)}.b-caution{background:var(--caution)}.b-warn{background:var(--warn)}.b-crit{background:var(--crit)}.b-na{background:#d5d9e0}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}
.kpi{background:var(--panel);border-radius:6px;padding:14px 16px}.kpi .l{font-size:12px;color:var(--mut)}
.kpi .v{font-size:24px;font-weight:700;margin-top:2px}.kpi .d{font-size:12px;color:var(--mut)}
.chart{border:1px solid var(--line);border-radius:6px;padding:12px 14px 6px;margin:12px 0}
.chart .t{font-size:13px;font-weight:600}.chart .lg{font-size:12px;color:var(--mut)}
.chart svg{width:100%;height:190px;display:block}.chart .ro{font-size:12px;color:var(--mut);min-height:18px}
.topact{margin:16px 0 0;border:1px solid var(--line);border-radius:6px;padding:14px 18px}.topact .tt{font-weight:700;color:var(--nav);margin-bottom:6px}.topact ol{margin:0;padding-left:20px}.topact li{margin:6px 0}.topact .who{font-size:12px;color:var(--mut);margin-left:4px}.bp td.st{white-space:nowrap}.grp{margin-top:18px}.grp>h3{font-size:14px;margin:0 0 8px;color:var(--nav)}
details.f{border:1px solid var(--line);border-left:5px solid;border-radius:5px;margin:8px 0;background:#fff}
details.f[open]{box-shadow:0 1px 0 var(--line)}
details.f summary{cursor:pointer;padding:11px 14px;list-style:none;display:flex;gap:10px;align-items:baseline}
details.f summary::-webkit-details-marker{display:none}
.tag{font-size:12px;font-weight:700;padding:1px 8px;border-radius:3px;color:#fff;white-space:nowrap}
.f-body{padding:0 16px 14px 16px;font-size:14px}.f-body dt{font-weight:700;margin-top:8px;font-size:13px;color:var(--nav)}
.f-body dd{margin:2px 0 0}.f-body .src{font-size:12px;color:var(--mut);margin-top:10px}
code{font-family:Consolas,"D2Coding",monospace;font-size:13px;background:var(--panel);padding:1px 5px;border-radius:3px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0}th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--panel);font-weight:600;color:#3a4453}td.n{text-align:right}
.scroll{overflow-x:auto}.note{font-size:13px;color:var(--mut)}
pre{background:var(--panel);padding:10px 12px;border-radius:5px;font-size:12px;overflow-x:auto;white-space:pre-wrap}
@media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}.verdict b{font-size:24px}}
@media print{.wrap{padding:0}details.f{break-inside:avoid}details.f .f-body{display:block}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""
function chart(id, series, ths, unit){
  const el=document.getElementById(id); if(!el) return;
  const svg=el.querySelector('svg'), ro=el.querySelector('.ro');
  const W=1000,H=190,L=46,R=10,T=10,B=24;
  const xs=DATA.t; if(!xs.length){ro.textContent='데이터 없음';return;}
  let mx=0; series.forEach(s=>DATA[s.k].forEach(v=>{if(v!=null&&v>mx)mx=v}));
  ths.forEach(t=>{if(t.v>mx*0.6&&t.v<mx*2)mx=Math.max(mx,t.v)}); mx=mx*1.1||1;
  const x=i=>L+(W-L-R)*(xs.length>1?i/(xs.length-1):0), y=v=>T+(H-T-B)*(1-v/mx);
  let g='';
  for(let k=0;k<=4;k++){const v=mx*k/4;g+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#eceef2"/>`+
    `<text x="${L-6}" y="${y(v)+4}" font-size="11" fill="#8b93a1" text-anchor="end">${v<10?v.toFixed(1):Math.round(v)}</text>`;}
  ths.forEach(t=>{if(t.v<=mx){g+=`<line x1="${L}" x2="${W-R}" y1="${y(t.v)}" y2="${y(t.v)}" stroke="${t.c}" stroke-dasharray="5 4" stroke-width="1.2"/>`+
    `<text x="${W-R-4}" y="${y(t.v)-4}" font-size="11" fill="${t.c}" text-anchor="end">${t.l}</text>`;}});
  const n=xs.length, step=Math.max(1,Math.round(n/6));
  for(let i=0;i<n;i+=step){g+=`<text x="${x(i)}" y="${H-6}" font-size="11" fill="#8b93a1" text-anchor="middle">${Math.round(xs[i])}s</text>`;}
  series.forEach(s=>{let d='',pen=false;DATA[s.k].forEach((v,i)=>{if(v==null){pen=false;return;}d+=(pen?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1);pen=true;});
    g+=`<path d="${d}" fill="none" stroke="${s.c}" stroke-width="1.8"/>`;});
  g+=`<line id="${id}_c" x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="#9aa3b2" visibility="hidden"/>`;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML=g;
  const cur=document.getElementById(id+'_c');
  svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();const px=(e.clientX-r.left)/r.width*W;
    let i=Math.round((px-L)/(W-L-R)*(n-1));i=Math.max(0,Math.min(n-1,i));cur.setAttribute('x1',x(i));cur.setAttribute('x2',x(i));cur.setAttribute('visibility','visible');
    ro.textContent=`${Math.round(xs[i])}초 · `+series.map(s=>`${s.n} ${DATA[s.k][i]==null?'–':DATA[s.k][i].toFixed(2)}${unit}`).join(' · ');});
  svg.addEventListener('mouseleave',()=>{cur.setAttribute('visibility','hidden');ro.textContent='';});
}
"""

OWNER_ORDER = ["원인 분리 필요", "서버 담당자", "VMware 관리자", "ES 설정", "참고"]
OWNER_DESC = {
    "원인 분리 필요": "측정값 자체가 나쁜 항목입니다. 원인이 VM 안인지 밖인지 판정 근거를 함께 적었습니다.",
    "서버 담당자": "Guest OS에서 바꿀 수 있는 항목입니다. 모두 운영 변경이므로 담당자 검토 후 적용하세요. 이 도구는 아무것도 바꾸지 않았습니다.",
    "VMware 관리자": "VM 바깥 설정이라 Guest에서는 바꿀 수 없습니다. 근거와 함께 요청하세요.",
    "ES 설정": "Elasticsearch 설정·운영으로 풀어야 하는 항목입니다.",
    "참고": "정상 확인 또는 해석에 필요한 참고 정보입니다.",
}
COLOR = {"ok": "#1d7a4b", "info": "#2d5f9e", "caution": "#946300", "warn": "#bf4a10", "crit": "#b3261e", "na": "#8b93a1"}

def render(R, out_path):
    A, th = R["A"], R["th"]
    t0 = R["agg"][0]["t"] if R["agg"] else 0
    sys_by_t = {round(r["t"], 1): r for r in R["sysr"]}
    data = {"t": [], "ra": [], "wa": [], "rs": [], "ws": [], "rmb": [], "wmb": [], "aqu": [],
            "psif": [], "psis": [], "iow": [], "st": [], "dst": [], "dirty": []}
    for r in R["agg"]:
        s = sys_by_t.get(round(r["t"], 1), {})
        data["t"].append(round(r["t"] - t0, 1))
        data["ra"].append(round(r["r_await"], 3) if (r["r_await"] is not None and r["rio"] >= MIN_IOS_PER_INTERVAL) else None)
        data["wa"].append(round(r["w_await"], 3) if (r["w_await"] is not None and r["wio"] >= MIN_IOS_PER_INTERVAL) else None)
        for k, src in (("rs", r["rs"]), ("ws", r["ws"]), ("rmb", r["rmb"]), ("wmb", r["wmb"]), ("aqu", r["aqu"])):
            data[k].append(round(src, 2))
        for k, sk in (("psif", "psi_io_full"), ("psis", "psi_io_some"), ("iow", "iowait"), ("st", "steal"),
                      ("dst", "es_dstate"), ("dirty", "dirty_mb")):
            v = s.get(sk)
            data[k].append(round(v, 2) if v is not None else None)

    v = R["verdict"]
    meta = R["meta"]
    h = []
    h.append('<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append('<title>ES Disk I/O 진단 — {}</title><style>{}</style></head><body><div class="wrap">'.format(E(meta.get("host")), CSS))
    h.append('<h1>Elasticsearch 디스크 I/O 진단 리포트</h1>')
    h.append('<div class="meta">{} · {} · kernel {} · vCPU {} · RAM {:.0f}GB · ES {} · {} · 측정 {} ~ {} ({:.0f}초, {}초 간격)</div>'.format(
        E(meta.get("host")), E(R["os"]), E(R["kernel"]), R["ncpu"], R["mem_gb"], E(R["es_version"]),
        "VMware Guest" if R["is_vmware"] else E(R["virt"].get("detect_virt", "")),
        E(meta.get("start_wall", "")), E(meta.get("end_wall", "")[11:19]), R["dur"], E(meta.get("interval"))))

    h.append('<div class="verdict {}"><b>{}</b><span>{}</span></div>'.format(v[0], E(v[1]), E(v[2])))
    h.append('<div class="dims">')
    for name, s in R["dims"]:
        h.append('<div class="dim"><div class="n">{}</div><div class="s s-{}">{}</div><div class="bar b-{}"></div></div>'.format(E(name), s, SEV_LABEL.get(s, s), s))
    h.append('</div>')
    h.append('<p class="note">판정 기준: {} vSAN · 조치가 필요한 항목 {}건 · 측정 대상 디스크 {}{}</p>'.format(
        "All-Flash" if R["storage"] == "allflash" else "Hybrid", R["n_act"], E(", ".join(R["phys"]) or "-"),
        " (ES data 경로를 특정하지 못해 전체 디스크 기준)" if R["dev_guess"] else ""))

    # 우선 조치
    if R.get("top"):
        h.append('<div class="topact"><div class="tt">먼저 할 일</div><ol>')
        for f in R["top"]:
            first = re.split(r'(?<=[.다])\s', f.action, 1)[0]
            h.append('<li><span class="tag" style="background:{}">{}</span> <b>{}</b> <span class="who">{}</span><div class="note">{}</div></li>'.format(
                COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.owner), E(first)))
        h.append('</ol><div class="note">자세한 근거는 아래 "판정 근거와 조치 안내"에 있습니다.</div></div>')

    # KPI
    h.append('<h2>핵심 수치</h2><p class="lead">응답시간은 I/O가 {}건 이상 있었던 구간만으로 계산했습니다. 몇 건 안 되는 I/O의 우연한 지연이 결과를 흔들지 않게 하기 위해서입니다.</p>'.format(MIN_IOS_PER_INTERVAL))
    h.append('<div class="kpis">')
    kp = [("읽기 응답시간 p95", A["r_await_p95"], "ms", grade(A["r_await_p95"], th), "평균 " + fmt(A["r_await_mean"], 2, "ms")),
          ("쓰기 응답시간 p95", A["w_await_p95"], "ms", grade(A["w_await_p95"], th), "평균 " + fmt(A["w_await_mean"], 2, "ms")),
          ("IOPS p95", A["iops_p95"], "", "info", "읽기 {} · 쓰기 {}".format(fmt(A["rs_p95"], 0), fmt(A["ws_p95"], 0))),
          ("처리량 p95", A["mb_p95"], "MB/s", "info", "읽기 {} · 쓰기 {}".format(fmt(A["rmb_p95"], 1), fmt(A["wmb_p95"], 1)))]
    for l, val, u, s, d in kp:
        h.append('<div class="kpi"><div class="l">{}</div><div class="v s-{}">{}</div><div class="d">{}</div></div>'.format(
            E(l), s if s != "na" else "na", fmt(val, 2 if u == "ms" else 1, " " + u if u else ""), E(d)))
    h.append('</div>')

    # 차트
    h.append('<h2>시간대별 흐름</h2><p class="lead">응답시간이 튄 순간에 IOPS·대기 I/O·PSI가 함께 올랐는지 보면 원인을 가를 수 있습니다. 함께 오르면 부하 때문이고, 부하는 그대로인데 응답시간만 오르면 VM 바깥 문제일 가능성이 큽니다. 차트 위에 마우스를 올리면 값이 보입니다.</p>')
    charts = [
        ("c1", "디스크 응답시간 (ms)", [{"k": "ra", "n": "읽기", "c": "#2d5f9e"}, {"k": "wa", "n": "쓰기", "c": "#bf4a10"}],
         [{"v": th["caution"], "l": "주의 {}ms".format(th["caution"]), "c": "#946300"}, {"v": th["crit"], "l": "위험 {}ms".format(th["crit"]), "c": "#b3261e"}], "ms"),
        ("c2", "IOPS", [{"k": "rs", "n": "읽기", "c": "#2d5f9e"}, {"k": "ws", "n": "쓰기", "c": "#bf4a10"}], [], ""),
        ("c3", "처리량 (MB/s)", [{"k": "rmb", "n": "읽기", "c": "#2d5f9e"}, {"k": "wmb", "n": "쓰기", "c": "#bf4a10"}], [], "MB/s"),
        ("c4", "대기 중인 I/O (aqu-sz)", [{"k": "aqu", "n": "aqu-sz", "c": "#23324a"}],
         [{"v": R["qd_total"], "l": "queue_depth {}".format(int(R["qd_total"])), "c": "#b3261e"}] if R["qd_total"] else [], ""),
        ("c5", "포화 지표 (%)", [{"k": "psif", "n": "PSI io full", "c": "#b3261e"}, {"k": "psis", "n": "PSI io some", "c": "#946300"},
                             {"k": "iow", "n": "iowait", "c": "#8b93a1"}, {"k": "st", "n": "CPU steal", "c": "#6b4fa0"}], [], "%"),
        ("c6", "ES 스레드 D 상태 수 · Dirty page(MB)", [{"k": "dst", "n": "D 상태 스레드", "c": "#23324a"}, {"k": "dirty", "n": "Dirty MB", "c": "#1d7a4b"}], [], ""),
    ]
    for cid, title, _s, _t, _u in charts:
        h.append('<div class="chart" id="{}"><div class="t">{}</div><div class="lg">{}</div><svg preserveAspectRatio="none"></svg><div class="ro"></div></div>'.format(
            cid, E(title), " · ".join('<span style="color:{}">━</span> {}'.format(s["c"], E(s["n"])) for s in _s)))

    # 조치 항목
    h.append('<h2>판정 근거와 조치 안내</h2><p class="lead">누가 조치해야 하는지로 묶었습니다. 항목을 펼치면 측정 근거, 왜 중요한지, 어떻게 조치하는지, 기준의 출처가 나옵니다.</p>')
    F = sorted(R["findings"], key=lambda f: -SEV_ORDER.get(f.sev, 0))
    for owner in OWNER_ORDER:
        items = [f for f in F if f.owner == owner]
        if not items:
            continue
        h.append('<div class="grp"><h3>{} <span class="note">({}건)</span></h3><p class="note" style="margin:0 0 6px">{}</p>'.format(E(owner), len(items), E(OWNER_DESC[owner])))
        for f in items:
            op = " open" if SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["warn"] else ""
            h.append('<details class="f"{} style="border-left-color:{}"><summary><span class="tag" style="background:{}">{}</span>'
                     '<span><b>{}</b> <span class="note">· {}</span></span></summary><div class="f-body"><dl>'
                     '<dt>측정 근거</dt><dd>{}</dd><dt>왜 중요한가</dt><dd>{}</dd><dt>조치 안내</dt><dd>{}</dd></dl>'
                     '<div class="src">기준 출처: {}</div></div></details>'.format(
                         op, COLOR[f.sev], COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.dim), E(f.evidence), E(f.why), E(f.action), E(f.source)))
        h.append('</div>')

    # Best practice 대조표
    if R.get("BP"):
        nbad = sum(1 for b in R["BP"] if SEV_ORDER.get(b[4], 0) >= SEV_ORDER["caution"])
        h.append('<h2>Best practice 대조표</h2><p class="lead">확인한 항목 전부를 권장값과 나란히 놓았습니다. 문제 없는 항목도 빠짐없이 보여 주므로 "이것도 확인했는가"를 여기서 검증할 수 있습니다. 전체 {}개 중 조치 검토 {}개.</p>'.format(len(R["BP"]), nbad))
        h.append('<div class="scroll"><table class="bp"><tr><th>구분</th><th>항목</th><th>권장</th><th>현재</th><th>결과</th><th>출처</th></tr>')
        cat_prev = None
        for cat, item, rec, cur, st, src in R["BP"]:
            h.append('<tr><td class="note">{}</td><td>{}</td><td>{}</td><td>{}</td><td class="st s-{}"><b>{}</b></td><td class="note">{}</td></tr>'.format(
                E(cat) if cat != cat_prev else "", E(item), E(rec), E(cur), st, SEV_LABEL.get(st, st), E(src)))
            cat_prev = cat
        h.append('</table></div>')

    # ES 지표
    if R["es_rows"]:
        h.append('<h2>Elasticsearch 쪽 영향 (측정 구간 변화량)</h2><p class="lead">OS에서 본 디스크 상태가 ES 동작에 실제로 영향을 줬는지 확인하는 표입니다. 시작·종료 시점 node stats의 차이로 계산했습니다.</p>')
        h.append('<table><tr><th>지표</th><th>값</th><th>해석</th></tr>')
        for a, b, c in R["es_rows"]:
            h.append('<tr><td>{}</td><td class="n">{}</td><td class="note">{}</td></tr>'.format(E(a), E(b), E(c)))
        h.append('</table>')

    # 여유율
    if R["headroom"]:
        h.append('<h2>최대 능력 대비 사용률</h2><p class="lead">es_disk_bench.sh로 잰 최대 능력과 이번 측정의 p95를 비교했습니다. vSAN 캐시 계층에 벤치 파일이 들어가면 최대 능력이 실제보다 높게 나오므로 사용률은 낙관적인 값입니다.</p>')
        h.append('<table><tr><th>항목</th><th>관측 p95</th><th>측정 최대</th><th>사용률</th></tr>')
        for l, o, c, p, u in R["headroom"]:
            s = "warn" if p >= 80 else "caution" if p >= 60 else "ok"
            h.append('<tr><td>{}</td><td class="n">{}</td><td class="n">{}</td><td class="n s-{}"><b>{:.0f}%</b></td></tr>'.format(E(l), fmt(o, 1, " " + u), fmt(c, 1, " " + u), s, p))
        h.append('</table>')
        sync = (R["bench"].get("fsync_4k") or {})
        if sync.get("sync_p99") or sync.get("w_p99"):
            h.append('<p class="note">동기 쓰기(fdatasync) p99: {} — translog를 요청마다 fsync하는 기본 설정(durability: request)에서 인덱싱 요청 한 번이 최소로 기다리는 시간입니다.</p>'.format(
                fmt(sync.get("sync_p99") or sync.get("w_p99"), 2, " ms")))
    else:
        h.append('<h2>한계 추정</h2><p class="lead">부하 테스트 없이 계산할 수 있는 것은 "가상 디스크 큐 기준 상한"까지입니다. 동시에 처리할 수 있는 요청 수(queue_depth 합계)를 1건 평균 처리 시간으로 나눈 값입니다. '
                 '실제로는 부하가 늘면 처리 시간도 늘고 vSAN 쪽이 먼저 막히므로, 이 값은 <b>넘을 수 없는 천장</b>이지 도달 가능한 값이 아닙니다.</p>')
        if R.get("q_ceiling"):
            use = 100.0 * (A["iops_p95"] or 0) / R["q_ceiling"]
            h.append('<table><tr><th>항목</th><th>값</th></tr><tr><td>Guest 큐 기준 이론 상한</td><td class="n">{}</td></tr>'
                     '<tr><td>이번 측정 p95</td><td class="n">{}</td></tr><tr><td>천장 대비 사용</td><td class="n"><b>{:.0f}%</b></td></tr></table>'.format(
                         fmt(R["q_ceiling"], 0, " IOPS"), fmt(A["iops_p95"], 0, " IOPS"), use))
        else:
            h.append('<p class="note">측정 구간의 I/O가 적거나 queue_depth를 읽지 못해 계산하지 않았습니다.</p>')
        h.append('<p class="note">실제 한계는 서비스 투입 전이나 점검 시간에 <code>es_disk_bench.sh</code>로 재고 <code>--bench</code> 옵션으로 넣으면 "최대 능력 대비 사용률"이 이 자리에 표시됩니다.</p>')

    # 클러스터 관점
    CL = R.get("CL")
    if CL:
        h.append('<h2>클러스터 관점 — 이 노드만인가, 전체인가</h2>')
        h.append('<p class="lead">각 노드가 자기 커널에서 읽은 디스크 카운터를 ES 조회 API로 모아 비교한 것입니다. 노드에 접속하지 않고 얻은 값이라 커널 레벨 응답시간만큼 정밀하지는 않지만, 어느 노드가 유독 바쁜지 가려내는 데는 충분합니다.{}</p>'.format(
            "" if CL["io_ok"] else " 이 클러스터에서는 노드별 디스크 카운터(fs.io_stats)가 보고되지 않아 용량·인덱싱 지표로만 비교했습니다."))
        h.append('<div class="scroll"><table><tr><th>노드</th><th>역할</th><th>디스크 busy</th><th>읽기</th><th>쓰기</th><th>디스크 사용률</th>'
                 '<th>보관 용량</th><th>인덱싱</th><th>스로틀</th><th>write 거절</th><th>segment</th><th>heap</th><th>CPU</th></tr>')
        for r in CL["rows"]:
            hot = ' style="background:#fdf6e7"' if (r["busy"] or 0) >= 80 else ""
            h.append('<tr{}><td><b>{}</b></td><td class="note">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td>'
                     '<td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td>'
                     '<td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                         hot, E(r["name"]), E(r["roles"]), fmt(r["busy"], 0, "%"),
                         "{} / {}".format(fmt(r["rops"], 0), fmt(r["rmb"], 1, "MB/s")),
                         "{} / {}".format(fmt(r["wops"], 0), fmt(r["wmb"], 1, "MB/s")),
                         fmt(r["used_pct"], 0, "%"), fmt(r["store_gb"], 0, "GB"),
                         fmt(r["idx_rate"], 0, "/s"), fmt(r["throttle"], 0, "ms"), fmt(r["wrej"], 0),
                         fmt(r["segments"], 0), fmt(r["heap_pct"], 0, "%"), fmt(r["cpu"], 0, "%")))
        h.append('</table></div>')
        h.append('<p class="note">디스크 busy = 장치가 I/O를 처리하고 있던 시간 비율. 100%에 가까워도 병렬 처리가 되는 장치라면 여유가 있을 수 있어, 노드 간 비교용으로만 봅니다. '
                 '특정 노드가 눈에 띄면 그 노드에서 es_disk_collect.sh 를 실행해 커널 레벨로 확인하세요.</p>')
        if CL["acts"]:
            h.append('<p class="note">측정 시점 클러스터 작업: {}</p>'.format(E(" · ".join(CL["acts"]))))

    # 이 노드의 인덱스별 쓰기 분포
    IDX = R.get("IDX")
    if IDX:
        h.append('<h2>이 노드 디스크를 쓰는 인덱스</h2><p class="lead">측정 구간 동안 이 노드에 있는 샤드가 실제로 한 일입니다. 디스크가 느릴 때 "무엇 때문에 바빴는지"를 인덱스 단위로 좁힐 수 있습니다.</p>')
        h.append('<div class="scroll"><table><tr><th>인덱스</th><th>ILM phase</th><th>인덱싱(건)</th><th>문서당</th><th>merge 쓰기</th><th>검색(건)</th><th>보관 용량</th><th>segment</th></tr>')
        tot = sum(r["docs"] for r in IDX) or 1
        for r in IDX[:12]:
            share = 100.0 * r["docs"] / tot
            hot = ' style="background:#fdf6e7"' if share >= 50 else ""
            h.append('<tr{}><td><b>{}</b></td><td class="note">{}</td><td class="n">{} <span class="note">({:.0f}%)</span></td><td class="n">{}</td>'
                     '<td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                         hot, E(r["index"]), E(r["phase"] or "-"), fmt(r["docs"], 0), share, fmt(r["ms_per_doc"], 3, "ms"),
                         fmt(r["merge_mb"], 0, "MB"), fmt(r["queries"], 0), fmt(r["store_gb"], 1, "GB"), fmt(r["segments"], 0)))
        h.append('</table></div>')
        if len(IDX) > 12:
            h.append('<p class="note">상위 12개만 표시 (전체 {}개).</p>'.format(len(IDX)))

    # 디바이스 상세
    h.append('<h2>디바이스별 상세</h2>')
    h.append('<div class="scroll"><table><tr><th>ES data 경로</th><th>마운트</th><th>FS · 옵션</th><th>논리 장치</th><th>물리 디스크</th></tr>')
    for pm in R["path_map"]:
        m = pm["mount"] or {}
        h.append('<tr><td>{}</td><td>{}</td><td>{} · <span class="note">{}</span></td><td>{}</td><td>{}</td></tr>'.format(
            E(pm["path"]), E(m.get("mnt", "-")), E(m.get("fs", "-")), E(m.get("opts", "-")), E(pm["kname"] or "-"), E(", ".join(pm["phys"]) or "-")))
    h.append('</table></div>')
    topo = R["topo"]
    h.append('<div class="scroll"><table><tr><th>디스크</th><th>컨트롤러</th><th>queue_depth</th><th>scheduler</th><th>readahead</th><th>timeout</th><th>섹터(논리/물리)</th>'
             '<th>읽기 p95</th><th>쓰기 p95</th><th>IOPS p95</th><th>aqu p95</th><th>%util p95</th></tr>')
    for d in R["phys"] + [x for x in R["logical"] if x not in R["phys"]]:
        a = topo.attr.get(topo.whole(d), {})
        st = R["dev_stats"].get(d) or R["log_stats"].get(d) or {}
        host = topo.scsihost.get(d, "")
        h.append('<tr><td><b>{}</b>{}</td><td>{}</td><td class="n">{}</td><td>{}</td><td class="n">{}KB</td><td class="n">{}s</td><td>{}/{}</td>'
                 '<td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                     E(d), " <span class='note'>(" + E(a.get("dm/name")) + ")</span>" if a.get("dm/name") else "",
                     E((host + " " + topo.hostdrv.get(host, "")).strip() or "-"), E(a.get("device/queue_depth", "-")),
                     E(a.get("queue/scheduler", "-")), E(a.get("queue/read_ahead_kb", "-")), E(a.get("device/timeout", "-")),
                     E(a.get("queue/logical_block_size", "-")), E(a.get("queue/physical_block_size", "-")),
                     fmt(st.get("r_await_p95"), 2, "ms"), fmt(st.get("w_await_p95"), 2, "ms"), fmt(st.get("iops_p95"), 0),
                     fmt(st.get("aqu_p95"), 2), fmt(st.get("util_p95"), 0, "%")))
    h.append('</table></div><p class="note">%util은 참고용입니다. vSAN·SSD처럼 요청을 병렬로 처리하는 장치는 %util이 100%여도 여유가 있을 수 있어 판정에 쓰지 않았습니다.</p>')

    # 이력
    if R["hist"]:
        h.append('<h2>과거 7일 이력 (sar)</h2><p class="lead">서버에 이미 쌓여 있던 sysstat 기록입니다. 추가 부하 없이 이번 측정 창 밖의 피크를 볼 수 있습니다. 10분 평균이라 순간 최고치는 더 높았을 수 있습니다.</p>')
        h.append('<table><tr><th>날짜</th><th>응답시간 p95</th><th>최고</th><th>최고 시각</th><th>%util 최고</th></tr>')
        for day, p95, mxv, when, ut in R["hist"]:
            h.append('<tr><td>{}</td><td class="n s-{}">{}</td><td class="n s-{}">{}</td><td>{}</td><td class="n">{}</td></tr>'.format(
                E(day), grade(p95, th), fmt(p95, 1, "ms"), grade(mxv, th), fmt(mxv, 1, "ms"), E(when), fmt(ut, 0, "%")))
        h.append('</table>')

    # 측정 범위와 한계
    ov = R["overhead"]
    h.append('<h2>무엇을 어떻게 쟀고, 무엇은 못 보는가</h2>')
    h.append('<p class="lead">이 도구는 읽기만 합니다. 이번 수집에 쓴 CPU는 {}초 (측정 시간 대비 CPU 1개의 {}), 결과 파일은 {}입니다.</p>'.format(
        fmt(ov["cpu_s"], 2), fmt(ov["pct_core"], 2, "%"), fmt((ov["out_kb"] or 0) / 1024.0, 1, "MB")))
    h.append('<table><tr><th>지표</th><th>출처</th><th>알 수 있는 것</th><th>판정에 쓰는 방식</th></tr>')
    rows = [
        ("응답시간 r_await / w_await", "/proc/diskstats (iostat과 같은 원본, 직접 계산)", "ES가 실제로 겪는 I/O 1건당 지연", "p95로 판정. I/O 적은 구간 제외"),
        ("대기 I/O aqu-sz ÷ queue_depth", "/proc/diskstats, /sys/block/*/device/queue_depth", "지연이 VM 안(큐)에서 생기는지 밖에서 생기는지", "병목 위치 분리"),
        ("PSI io some/full", "/proc/pressure/io (커널 4.20+, RHEL8은 psi=1 필요)", "작업이 I/O 때문에 멈춘 시간 비율", "포화 판정"),
        ("ES 스레드 D 상태", "/proc/<pid>/task/*/stat", "ES 스레드가 디스크 때문에 멈춘 순간", "포화 판정"),
        ("iowait · procs_blocked", "/proc/stat", "추세 참고 (CPU가 바쁘면 낮게 나와 단독 판정 불가)", "참고만"),
        ("ES major fault · 물리 읽기/쓰기", "/proc/<pid>/stat, /proc/<pid>/io", "page cache에 없어 디스크를 읽은 양", "캐시 부족형 병목 판정"),
        ("swap · dirty page", "/proc/vmstat, /proc/meminfo", "메모리 압박이 디스크로 번지는지", "메모리 판정"),
        ("CPU steal · balloon · 메모리 예약/한도", "/proc/stat, vmware-toolbox-cmd stat", "호스트 자원 경합과 VM 자원 설정", "VMware 자원 판정"),
        ("커널 로그", "journalctl -k / dmesg (최근 7일)", "vSAN 순간 정지 흔적 (abort/reset, hung task)", "오류 판정"),
        ("ES node stats 변화량", "_nodes/_local/stats (조회 API 2회)", "인덱싱 스로틀, 거절, flush·검색 지연", "ES 영향 판정"),
        ("설정값", "/sys/block, sysctl, mount, udev, ES 설정", "readahead·스케줄러·타임아웃·limits 등", "설정 판정"),
        ("과거 이력", "sysstat sar 파일 (이미 기록된 것)", "측정 창 밖의 피크", "주의 알림"),
        ("ES 서버 로그", "/var/log/elasticsearch/*.log (최근 7일, 관련 줄만)", "ES가 직접 남긴 스로틀·watermark·flush 실패 기록", "ES 영향 판정"),
        ("mmap 사용량", "/proc/<pid>/maps 줄 수 vs vm.max_map_count", "segment 매핑 한도 여유", "설정 판정"),
        ("스토리지 인터럽트 분포", "/proc/interrupts 시작·종료 스냅샷", "I/O 완료 처리가 vCPU 한 개에 몰리는지", "설정 판정"),
        ("노드 간 비교", "_nodes/stats fs.io_stats (측정과 같은 창으로 2회)", "이 노드만인지 클러스터 전체인지", "클러스터 판정"),
        ("인덱스별 분포", "_nodes/_local/stats/indices?level=indices + _ilm/explain", "이 노드 디스크를 쓰는 인덱스와 ILM phase", "쓰기 집중 판정"),
        ("샤드 배분", "_cat/allocation", "샤드가 이 노드에 몰렸는지", "교차 판정"),
    ]
    for r in rows:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in r) + '</tr>')
    h.append('</table>')
    h.append('<p><b>Elastic System/Linux integration 으로는 볼 수 없는 것</b> — 상시 수집 지표와 이 툴이 겹치지 않는 부분입니다. '
             '상시 모니터링을 대체하려는 것이 아니라, 경보가 울린 뒤 원인을 좁힐 때 필요한 항목들입니다.</p>')
    h.append('<table><tr><th>항목</th><th>상시 수집 지표</th><th>이 툴</th><th>왜 필요한가</th></tr>')
    gaps = [
        ("응답시간 백분위", "10~30초 평균값만 저장", "p95 + I/O 적은 구간 제외", "평균은 짧은 지연 급등을 지워 버립니다"),
        ("병목 위치 판정", "없음", "대기 I/O ÷ queue_depth 로 VM 안/밖 구분", "VMware 관리자에게 넘길지 서버에서 풀지가 갈립니다"),
        ("PSI (I/O 압박)", "Linux integration에 pressure 지표 일부", "io some/full 을 측정 구간 전체로 계산", "iowait보다 정확한 포화 지표"),
        ("ES 스레드 D 상태", "없음", "ES 스레드만 골라 카운트", "ES가 실제로 디스크에 멈춰 있었는지"),
        ("블록 장치 설정 전수", "없음", "readahead·scheduler·timeout·iostats·wbt 등", "설정 문제는 지표로 안 보이고 설정을 봐야 압니다"),
        ("LVM·파티션 토폴로지", "없음", "dm → 물리 디스크 역추적, 정렬 확인", "ES 경로가 실제로 어느 디스크인지"),
        ("커널 로그 상관", "없음 (로그는 별도 수집)", "SCSI abort/reset·hung task 분류", "vSAN 순간 정지의 흔적"),
        ("VMware 자원", "없음", "balloon·host swap·예약·limit", "메모리 회수는 디스크 문제로 위장해 나타납니다"),
        ("스토리지 IRQ 편중", "없음", "측정 구간 IRQ 분포", "vCPU 한 개에 몰리면 IOPS가 거기서 막힙니다"),
        ("mmap 여유", "없음", "현재 매핑 수 / max_map_count", "한도에 닿으면 인덱싱이 실패합니다"),
        ("인덱스별 쓰기 분포", "인덱스 지표는 있으나 노드 로컬 관점 아님", "이 노드 샤드의 인덱스별 delta + ILM phase", "디스크를 쓰는 주체를 인덱스까지 좁힘"),
        ("Best practice 대조", "없음", "공식 문서 기준 전수 대조표", "경보가 아니라 사전 예방"),
    ]
    for g in gaps:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in g) + '</tr>')
    h.append('</table>')
    h.append('<p class="note">반대로 상시 수집 지표가 더 나은 영역도 분명합니다. 장기 추세, 여러 노드 동시 시계열, 경보 자동화는 Elastic 쪽이 맞습니다. '
             '이 툴은 그 경보가 울린 순간에 한 번 깊게 파는 용도입니다. 상시 경보 기준은 GUARDLINE.md 4장에 정리했습니다.</p>')
    h.append('<p><b>Guest OS에서 원리상 볼 수 없는 것</b> — 아래 항목은 판정에 넣지 않았고, 필요하면 VMware 관리자에게 확인해야 합니다.</p>')
    h.append('<table><tr><th>항목</th><th>왜 중요한가</th><th>관리자 확인 방법</th></tr>')
    blind = [
        ("vSAN 스토리지 정책 (RAID·FTT·stripe·IOPS 제한)", "OSA에서 RAID-5/6은 쓰기마다 읽기-수정-쓰기가 생겨 RAID-1보다 쓰기 지연이 큽니다. 정책의 IOPS 제한은 그 이상을 막습니다.", "VM 스토리지 정책 확인"),
        ("ES 복제본 × vSAN 복제의 쓰기 증폭", "ES 복제본 1 + vSAN FTT=1(RAID-1)이면 문서 하나가 물리적으로 4벌 기록됩니다. 가용성은 ES에서도 보장되므로 용량·쓰기 부하 설계 때 함께 고려해야 합니다.", "스토리지 정책 + ES 인덱스 replicas 비교"),
        ("VM 스냅샷 존재", "스냅샷이 남아 있으면 쓰기가 delta 파일로 가서 성능이 떨어집니다.", "vCenter 스냅샷 관리자"),
        ("같은 호스트의 ES 노드 배치", "ES primary와 replica가 같은 ESXi 호스트에 있으면 호스트 장애 한 번에 둘 다 잃습니다.", "DRS anti-affinity 규칙 + ES shard allocation awareness"),
        ("vSAN 네트워크·resync·캐시 사용률", "Guest에서는 쓰기 지연으로만 간접 관측됩니다.", "vSAN 성능 서비스, esxtop DAVG/KAVG"),
        ("실제 물리 디스크 상태·지연", "Guest의 SMART 조회는 가상 디스크라 의미가 없습니다.", "vSAN Skyline Health"),
        ("vNUMA 경계, CPU hot-add", "CPU hot-add를 켜면 vNUMA가 꺼져 메모리 접근이 느려질 수 있습니다.", "VM 고급 설정"),
    ]
    for r in blind:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in r) + '</tr>')
    h.append('</table>')

    # 부록
    h.append('<h2>부록</h2>')
    sc = R["sysctl"]
    h.append('<details class="f" style="border-left-color:#8b93a1"><summary><b>커널·메모리 설정 원본</b></summary><div class="f-body"><table>')
    for k in ("vm.swappiness", "vm.max_map_count", "vm.dirty_ratio", "vm.dirty_background_ratio", "vm.dirty_bytes", "vm.dirty_background_bytes",
              "vm.zone_reclaim_mode", "fs.file-max", "thp.enabled", "thp.defrag", "psi"):
        h.append('<tr><td><code>{}</code></td><td>{}</td></tr>'.format(E(k), E(sc.get(k, "-"))))
    for k, vv in sorted(R["virt"].items()):
        h.append('<tr><td><code>{}</code></td><td>{}</td></tr>'.format(E(k), E(vv)))
    h.append('</table></div></details>')
    if R["klog"]:
        h.append('<details class="f" style="border-left-color:#8b93a1"><summary><b>커널 로그 원문 (디스크 관련 최근 {}줄)</b></summary><div class="f-body"><pre>{}</pre></div></details>'.format(
            min(60, len(R["klog"])), E("\n".join(R["klog"][-60:]))))
    h.append('<p class="note" style="margin-top:28px">es-disk-probe v{} · 이 리포트는 시스템을 변경하지 않은 읽기 전용 진단 결과입니다. 조치 안내는 담당자 검토 후 적용하세요. 설계 기준과 상시 감시 방법은 GUARDLINE.md에 있습니다.</p>'.format(E(meta.get("tool_version", TOOL_VERSION))))

    h.append('</div><script>const DATA={};{}'.format(json.dumps(data), JS))
    for cid, title, s, t, u in charts:
        h.append('chart({},{},{},{});'.format(json.dumps(cid), json.dumps(s, ensure_ascii=False), json.dumps(t, ensure_ascii=False), json.dumps(u)))
    h.append('</script></body></html>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(h))

def render_cluster_only(cdir, out_path):
    F = []
    add = lambda *a: F.append(Finding(*a))
    CL = analyze_cluster(cdir, add, LAT_TH["allflash"])
    if not CL:
        sys.exit("클러스터 번들을 읽을 수 없습니다: " + cdir)
    root = rjson(cdir, "root.json") or {}
    worst = sev_max(*[f.sev for f in F]) if F else "ok"
    if SEV_ORDER.get(worst, 0) >= SEV_ORDER["warn"]:
        v = ("bad", "클러스터에 디스크 관련 경고가 있습니다", "여러 노드에 걸친 신호가 있습니다. 공용 스토리지나 워크로드 자체를 먼저 의심하세요.")
    elif SEV_ORDER.get(worst, 0) >= SEV_ORDER["caution"]:
        v = ("risk", "일부 노드에 쏠림이나 위험 요인이 있습니다", "표에서 눈에 띄는 노드에 접속해 es_disk_collect.sh 로 커널 레벨을 확인하세요.")
    else:
        v = ("good", "노드 간 디스크 사용이 고르고 경고 신호가 없습니다", "이 결과는 ES가 보고한 수치 기준입니다. 커널 레벨 응답시간은 노드 수집기로 확인하세요.")
    h = ['<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         '<title>ES 클러스터 디스크 관점 리포트</title><style>{}</style></head><body><div class="wrap">'.format(CSS),
         '<h1>Elasticsearch 클러스터 — 디스크 관점 리포트</h1>',
         '<div class="meta">{} · ES {} · 노드 {}개 · {} ~ {} (간격 {}초)</div>'.format(
             E(dig(root, "cluster_name") or "-"), E(dig(root, "version", "number") or "-"), len(CL["rows"]),
             E(CL["meta"].get("start_wall", "")), E(CL["meta"].get("end_wall", "")[11:19]), E(CL["meta"].get("gap", ""))),
         '<div class="verdict {}"><b>{}</b><span>{}</span></div>'.format(v[0], E(v[1]), E(v[2]))]
    h.append('<h2>노드별 비교</h2><div class="scroll"><table><tr><th>노드</th><th>역할</th><th>디스크 busy</th><th>쓰기</th><th>디스크 사용률</th><th>보관 용량</th><th>인덱싱</th><th>스로틀</th><th>write 거절</th><th>heap</th><th>CPU</th></tr>')
    for r in CL["rows"]:
        h.append('<tr><td><b>{}</b></td><td class="note">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
            E(r["name"]), E(r["roles"]), fmt(r["busy"], 0, "%"), fmt(r["wmb"], 1, " MB/s"), fmt(r["used_pct"], 0, "%"),
            fmt(r["store_gb"], 0, "GB"), fmt(r["idx_rate"], 0, "/s"), fmt(r["throttle"], 0, "ms"), fmt(r["wrej"], 0), fmt(r["heap_pct"], 0, "%"), fmt(r["cpu"], 0, "%")))
    h.append('</table></div>')
    if not CL["io_ok"]:
        h.append('<p class="note">이 클러스터는 노드별 디스크 카운터(fs.io_stats)를 보고하지 않아 busy·쓰기 열이 비어 있습니다.</p>')
    h.append('<h2>판정 근거와 조치 안내</h2>')
    for f in sorted(F, key=lambda f: -SEV_ORDER.get(f.sev, 0)):
        h.append('<details class="f" open style="border-left-color:{}"><summary><span class="tag" style="background:{}">{}</span><span><b>{}</b> <span class="note">· {}</span></span></summary>'
                 '<div class="f-body"><dl><dt>측정 근거</dt><dd>{}</dd><dt>왜 중요한가</dt><dd>{}</dd><dt>조치 안내</dt><dd>{}</dd></dl><div class="src">기준 출처: {}</div></div></details>'.format(
                     COLOR[f.sev], COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.owner), E(f.evidence), E(f.why), E(f.action), E(f.source)))
    if not F:
        h.append('<p class="note">경고 항목 없음.</p>')
    h.append('<p class="note" style="margin-top:28px">es-disk-probe v{} · ES 조회 API(GET)만으로 만든 읽기 전용 리포트입니다.</p>'.format(TOOL_VERSION) + '</div></body></html>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(h))
    return v

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--cluster-only")
    ap.add_argument("-o", "--out")
    ap.add_argument("--storage", choices=["allflash", "hybrid"])
    ap.add_argument("--bench")
    ap.add_argument("--cluster")
    a = ap.parse_args()
    if a.cluster_only:
        cdir = open_bundle(a.cluster_only)
        out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.cluster_only)), "es_cluster_report.html")
        v = render_cluster_only(cdir, out)
        print(out); print("판정: " + v[1])
        return
    if not a.bundle:
        ap.error("노드 번들 경로 또는 --cluster-only <클러스터 번들> 이 필요합니다")
    base = open_bundle(a.bundle)
    R = analyze(base, a.storage, a.bench, a.cluster)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.bundle)), "es_disk_report_{}.html".format(R["meta"].get("host", "node")))
    render(R, out)
    print(out)
    print("판정: {} · 조치 필요 {}건".format(R["verdict"][1], R["n_act"]))

if __name__ == "__main__":
    main()
