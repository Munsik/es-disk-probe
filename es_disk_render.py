#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
es_disk_render.py (v0.10.0)
es_disk_collect.sh 가 만든 번들(디렉터리 또는 .tar.gz)을 읽어
지표 계산 → 판정 → HTML 리포트를 생성합니다.

- Python 3.6+ 표준 라이브러리만 사용 (RHEL8 platform-python 호환)
- 서버가 아닌 PC에서 실행해도 됩니다 (번들만 옮기면 됨)

사용:
  python3 es_disk_render.py <번들 디렉터리 | 번들.tar.gz> [-o report.html]
                            [--platform auto|vmware|baremetal|vm]
                            [--storage auto|allflash|hybrid|nvme|ssd|hdd] [--bench <es_disk_bench 결과 디렉터리>]
"""
import argparse, html, json, os, re, sys, tarfile, tempfile, datetime

TOOL_VERSION = "0.10.0"

# ─────────────────────────────────────────────────────────────────────────────
# 기준값 (출처를 함께 표기. 리포트에도 그대로 노출)
# ─────────────────────────────────────────────────────────────────────────────
# OS에서 관측한 디스크 응답시간(ms). Elastic이 공식 수치를 제시하지는 않는다.
# - vSAN(allflash/hybrid): Broadcom KB 389082 의 VM 관점 정상 범위(All-Flash <5ms, Hybrid <20ms)를 기준
# - bare-metal·SAN(nvme/ssd/hdd): Broadcom KB 424485 의 장치 관점 경보 기준
#   (NVMe >1ms, 엔터프라이즈 SSD >3ms, HDD >25ms, HDD 30ms 초과는 critical)을 '주의' 선으로 둔다.
#   bare-metal 에는 hypervisor·가상 SCSI 계층이 없어 장치 관점 수치가 곧 OS 관점 기대치다.
#   단, await 에는 block layer 대기 시간이 포함되므로 부하가 몰리면 장치 지연보다 크게 나온다.
# - vm(KVM, Hyper-V, 클라우드 등): 백엔드를 알 수 없어 vSAN All-Flash 수치를 공통 기준으로 빌려 쓴다.
# 주의 위 단계(경고·위험)는 모두 실무 기준이다.
LAT_TH = {
    "allflash": {"caution": 5.0, "warn": 10.0, "crit": 20.0},
    "hybrid":   {"caution": 10.0, "warn": 20.0, "crit": 30.0},
    "nvme":     {"caution": 1.0, "warn": 3.0, "crit": 10.0},
    "ssd":      {"caution": 3.0, "warn": 6.0, "crit": 15.0},
    "hdd":      {"caution": 25.0, "warn": 30.0, "crit": 50.0},
    "vm":       {"caution": 5.0, "warn": 10.0, "crit": 20.0},
    "vmware":   {"caution": 5.0, "warn": 10.0, "crit": 20.0},   # VMware, 데이터스토어 종류 미확인 (기본값)
    "vmfs":     {"caution": 5.0, "warn": 10.0, "crit": 20.0},   # VMware, SAN·NFS 데이터스토어
    "cloud":    {"caution": 5.0, "warn": 10.0, "crit": 20.0},   # 클라우드 블록 볼륨 (EBS, Azure Disk, PD 등)
    "network":  {"caution": 5.0, "warn": 10.0, "crit": 20.0},   # 네트워크 블록 장치 (Ceph RBD, NBD)
}
STORAGE_LABEL = {"allflash": "vSAN All-Flash", "hybrid": "vSAN Hybrid", "nvme": "NVMe", "ssd": "SSD",
                 "hdd": "HDD", "vm": "가상 디스크 공통", "vmware": "VMware 공유 스토리지",
                 "vmfs": "VMware SAN·NFS 데이터스토어", "cloud": "클라우드 블록 볼륨", "network": "네트워크 블록 스토리지"}
LAT_SRC = {
    "vsan": "[VMware 공식] Broadcom KB 389082. All-Flash 5ms 미만 / Hybrid 20ms 미만을 정상으로 제시. "
            "주의·경고·위험 3단계 구분은 실무 기준이며 Elastic 공식 수치는 없음",
    "device": "[VMware 공식] Broadcom KB 424485 의 장치 관점 경보 기준(NVMe 1ms, 엔터프라이즈 SSD 3ms, HDD 25ms 초과, "
              "HDD 30ms 초과 critical)을 주의 선으로 사용. vSAN 문서지만 수치는 장치 자체의 기대치. "
              "경고·위험 단계는 실무 기준이며 Elastic 공식 수치는 없음",
    "vm": "[실무 기준] 하이퍼바이저·스토리지 백엔드를 알 수 없어 Broadcom KB 389082 의 VM 관점 수치(5ms)를 공통 기준으로 차용. "
          "Elastic 공식 수치는 없음",
    "vmware": "[실무 기준] Guest 에서는 데이터스토어가 vSAN 인지 SAN·NFS 인지 알 수 없어 Broadcom KB 389082 의 "
              "All-Flash VM 관점 수치(5ms)를 공통 기준으로 사용. Elastic 공식 수치는 없음",
    "cloud": "[실무 기준] 클라우드 블록 볼륨은 네트워크를 거치는 원격 스토리지라 로컬 SSD 기준을 쓸 수 없어 "
             "Broadcom KB 389082 의 VM 관점 수치(5ms)를 공통 기준으로 차용. 볼륨 종류별 기대치는 각 클라우드 문서를 따름",
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
            for m in t.getmembers():
                # 경로 탈출 방지: 절대경로·상위참조·심볼릭/하드링크는 건너뛴다
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    continue
                if m.issym() or m.islnk():
                    continue
                if not (m.isfile() or m.isdir()):
                    continue
                t.extract(m, tmp)
        subs = sorted(os.path.join(tmp, d) for d in os.listdir(tmp))
        subs = [d for d in subs if os.path.isdir(d)]
        # 번들 루트는 meta 파일이 있는 디렉터리. 여러 개면 그것으로 고른다
        for d in subs:
            if os.path.isfile(os.path.join(d, "meta")):
                return d
        if os.path.isfile(os.path.join(tmp, "meta")):
            return tmp
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
        self.hctl, self.hostattr, self.raiddev, self.aer = {}, {}, [], {}
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
            elif p[0] == "AER" and len(p) >= 6:
                e = {"pci": p[2]}
                for x in p[3:]:
                    k_, _, v_ = x.partition("=")
                    e[k_] = int(v_) if v_.strip().isdigit() else None
                self.aer[p[1]] = e
            elif p[0] == "HCTL" and len(p) == 3:
                self.hctl[p[1]] = p[2]
            elif p[0] == "HOSTATTR" and len(p) >= 4:
                self.hostattr.setdefault(p[1], {})[p[2]] = "|".join(p[3:])
            elif p[0] == "RAIDDEV" and len(p) >= 6:
                self.raiddev.append(dict([("name", p[1])] + [tuple(x.split("=", 1)) for x in p[2:] if "=" in x]))
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

# ─────────────────────────────────────────────────────────────────────────────
# 플랫폼 판별
#   kind   : vmware | baremetal | vm (그 밖의 hypervisor·클라우드) | unknown
#   attach : virtual | local | san | nvmeof | cloud
# 수집기는 근거만 모으고 여기서 판정한다. 확실한 근거가 없으면 bare-metal 로 단정하지 않는다.
# ─────────────────────────────────────────────────────────────────────────────
CONTAINER_IDS = ("docker", "podman", "lxc", "lxc-libvirt", "systemd-nspawn", "openvz", "rkt", "wsl", "proot", "pouch")
HV_LABEL = {"vmware": "VMware", "kvm": "KVM", "qemu": "QEMU", "microsoft": "Hyper-V", "xen": "Xen",
            "amazon": "AWS Nitro", "google": "Google Compute Engine", "oracle": "VirtualBox", "powervm": "IBM PowerVM",
            "zvm": "IBM z/VM", "parallels": "Parallels", "bhyve": "bhyve", "unknown-vm": "알 수 없는 hypervisor"}
FC_DRV = ("qla2xxx", "lpfc", "bfa", "qedf", "bnx2fc", "fnic", "zfcp", "csiostor")
ISCSI_DRV = ("iscsi_tcp", "be2iscsi", "bnx2i", "qedi", "cxgb3i", "cxgb4i", "ib_iser")
RAID_DRV = ("megaraid_sas", "hpsa", "smartpqi", "aacraid", "arcmsr", "3w-9xxx", "3w-sas", "mpt2sas", "mpt3sas", "mptsas")
# mpt*sas 는 IT(HBA) 모드면 디스크를 그대로 넘기므로 모델명으로 RAID 논리 디스크인지 한 번 더 본다
RAID_MODEL = re.compile(r'PERC|LOGICAL VOLUME|MR9\d|MegaRAID|ServeRAID|RAID|Virtual Disk|AVAGO|SmartArray|ThinkSystem R', re.I)
SAN_VENDOR = re.compile(r'^(PURE|NETAPP|3PARdata|HITACHI|HP HSV|EMC|DGC|IBM\s+2145|IBM\s+2107|HUAWEI|Nimble|NEXSAN|FUJITSU|DataCore|COMPELNT|Dell EMC|XtremIO|INFINIDAT|LIO-ORG|TrueNAS)', re.I)
VM_DISK_VENDOR = re.compile(r'^(VMware|QEMU|Msft|Virtual|Google|Amazon|0x1af4|RHEV|Xen|NUTANIX)', re.I)
# 클라우드 볼륨(네트워크 블록)과 인스턴스 로컬 디스크를 모델명으로 구분한다
CLOUD_BLOCK_MODEL = re.compile(r'Amazon Elastic Block Store|PersistentDisk|nvme_card-pd', re.I)
CLOUD_LOCAL_MODEL = re.compile(r'Amazon EC2 NVMe Instance Storage|Microsoft NVMe Direct Disk|nvme_card$|EphemeralDisk|Google EphemeralDisk', re.I)

def parse_storage(text):
    nv, fc, iscsi = {}, [], 0
    for line in text.splitlines():
        p = line.split("|")
        if p[0] == "NVME" and len(p) >= 4:
            nv.setdefault(p[1], {})[p[2]] = "|".join(p[3:]).strip()
        elif p[0] == "FCHOST" and len(p) >= 4:
            fc.append({"host": p[1], "state": p[2].split("=", 1)[-1], "speed": p[3].split("=", 1)[-1]})
        elif p[0] == "ISCSI" and len(p) == 3 and p[1] == "sessions":
            iscsi = int(num(p[2], 0) or 0)
    return {"nvme": nv, "fc": fc, "iscsi": iscsi}

def nvme_ctrl(dev):
    """nvme0n1 → nvme0"""
    m = re.match(r'^(nvme\d+)', dev or "")
    return m.group(1) if m else None

def detect_platform(virt, override="auto"):
    ev = []
    dv = (virt.get("detect_virt") or "").strip()
    dvm = (virt.get("detect_virt_vm") or "").strip()
    dvc = (virt.get("detect_virt_container") or "").strip()
    vendor = (virt.get("sys_vendor") or "").strip()
    prod = (virt.get("product_name") or "").strip()
    flag = (virt.get("cpu_hypervisor_flag") or "").strip()
    container = ""
    if dvc and dvc != "none":
        container = dvc
    elif dv in CONTAINER_IDS:
        container = dv
    elif virt.get("dockerenv") == "1":
        container = "docker"
    if virt.get("kubernetes") == "1":
        container = (container or "container") + " (Kubernetes)"
    hv = ""
    if dvm and dvm != "none":
        hv = dvm; ev.append("systemd-detect-virt -v = {}".format(dvm))
    elif dvm == "none":
        ev.append("systemd-detect-virt -v = none")
    elif dv and dv not in CONTAINER_IDS and dv not in ("none", "unknown"):
        hv = dv; ev.append("systemd-detect-virt = {}".format(dv))
    elif dv == "none":
        ev.append("systemd-detect-virt = none")
    if not hv:                       # detect-virt 가 없거나 컨테이너만 보고한 경우의 보조 근거
        xp = (vendor + " " + prod).lower()
        if "vmware" in xp:
            hv = "vmware"
        elif (virt.get("sys_hypervisor") or "").strip() == "xen" or "hvm domu" in xp:
            hv = "xen"
        elif "microsoft" in vendor.lower() and "virtual machine" in prod.lower():
            hv = "microsoft"
        elif "google" in xp:
            hv = "google"
        elif vendor == "Amazon EC2" and not prod.endswith(".metal"):
            hv = "amazon"
        elif re.search(r'\b(kvm|qemu|standard pc|bochs|openstack|rhev|ovirt|proxmox|nutanix|ahv)\b', xp):
            hv = "kvm"
        elif flag == "1" and dvm != "none" and dv != "none":
            hv = "unknown-vm"
        if hv:
            ev.append("DMI/CPU 플래그: {}".format((vendor + " " + prod).strip() or "hypervisor 플래그"))
    bare_evidence = (dvm == "none" or dv == "none" or flag == "0")
    if hv == "vmware":
        kind = "vmware"
    elif hv:
        kind = "vm"
    elif bare_evidence:
        kind = "baremetal"
        if vendor or prod:
            ev.append("DMI: {}".format((vendor + " " + prod).strip()))
    else:
        kind = "unknown"
    cloud = ""
    tag = (virt.get("chassis_asset_tag") or "").strip()
    if vendor == "Amazon EC2" or hv == "amazon":
        cloud = "AWS"
    elif tag == "7783-7084-3265-9085-8269-3286-77":
        cloud = "Azure"
    elif hv == "google" or "Google" in vendor:
        cloud = "Google Cloud"
    forced = override not in (None, "", "auto")
    if forced:
        ev.append("사용자 지정 --platform {}".format(override))
        kind = override
    return {"kind": kind, "hv": hv, "container": container, "cloud": cloud, "evidence": ev, "forced": forced}

# ─────────────────────────────────────────────────────────────────────────────
# 하드웨어 RAID 컨트롤러 (storcli·perccli / ssacli / arcconf 출력, 커널 raid_class)
# 결과는 공통 형태로 모은다.
#   ctrl: [{name, status, cache, battery}]
#   vds : [{id, dev, level, state, ok, cache_cur, cache_init, wb, media, pds:[{id, state, med, ok}]}]
#   pds_bad: [(설명, 심각도)]   bg: [진행 중 백그라운드 작업]
# ─────────────────────────────────────────────────────────────────────────────
def _cmd_blocks(text):
    """'#CMD ...' 로 구분된 도구 출력을 (명령, 본문) 목록으로"""
    out, cur, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("#CMD "):
            if cur is not None:
                out.append((cur, "\n".join(buf)))
            cur, buf = line[5:].strip(), []
        else:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf)))
    return out

def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            for x in _walk(v):
                yield x
    elif isinstance(o, list):
        for v in o:
            for x in _walk(v):
                yield x

def parse_storcli(text):
    R = {"tool": "storcli", "ctrl": [], "vds": [], "pds_bad": [], "bg": [], "pd_all": []}
    vd_map, pd_by_dg = {}, {}
    for cmd, body in _cmd_blocks(text):
        i = body.find("{")
        try:
            j = json.loads(body[i:]) if i >= 0 else None
        except ValueError:
            j = None
        if not j:
            continue
        for c in j.get("Controllers", []):
            rd_ = c.get("Response Data") or {}
            if not isinstance(rd_, dict):
                continue
            if "Basics" in rd_ or "Status" in rd_:
                st = (rd_.get("Status") or {})
                bb = [x.get("State") for x in (rd_.get("BBU_Info") or []) + (rd_.get("Cachevault_Info") or []) if isinstance(x, dict)]
                R["ctrl"].append({"name": dig(rd_, "Basics", "Model") or "controller",
                                  "status": st.get("Controller Status"), "battery": ", ".join(str(b) for b in bb if b) or None})
                for v in rd_.get("VD LIST") or []:
                    dg, _, vn = str(v.get("DG/VD", "")).partition("/")
                    vd_map.setdefault(vn, {}).update({"id": vn, "dg": dg, "level": v.get("TYPE"), "state": v.get("State"),
                                                     "cache_cur": v.get("Cache")})
                for pd in rd_.get("PD LIST") or []:
                    R["pd_all"].append(pd)
                    pd_by_dg.setdefault(str(pd.get("DG")), []).append(pd)
            for k, v in rd_.items():
                m = re.match(r'^/c\d+/v(\d+)$', k)
                if m and isinstance(v, list) and v:
                    x = v[0]; dg, _, _vn = str(x.get("DG/VD", "")).partition("/")
                    vd_map.setdefault(m.group(1), {}).update({"id": m.group(1), "dg": dg, "level": x.get("TYPE"),
                                                             "state": x.get("State"), "cache_cur": x.get("Cache")})
                m = re.match(r'^PDs for VD (\d+)$', k)
                if m and isinstance(v, list):
                    vd_map.setdefault(m.group(1), {})["pds"] = v
                m = re.match(r'^VD(\d+) Properties$', k)
                if m and isinstance(v, dict):
                    e = vd_map.setdefault(m.group(1), {})
                    e["dev"] = v.get("OS Drive Name")
                    e["cache_init"] = v.get("Write Cache(initial setting)")
            # 백그라운드 작업: patrol read, consistency check
            for d_ in _walk(rd_):
                for k, v in d_.items():
                    if re.search(r'(PR|CC) Current State', str(k)) and "activ" in str(v).lower():
                        R["bg"].append("{} {}".format("patrol read" if k.startswith("PR") else "consistency check", v))
            # 물리 디스크 상세: media error, predictive failure, SMART 경고
            for k, v in rd_.items():
                m = re.match(r'^Drive (/c\d+/e\d+/s\d+|/c\d+/s\d+) - Detailed Information$', k)
                if m and isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk.endswith(" State") and isinstance(vv, dict):
                            me = num(vv.get("Media Error Count"), 0) or 0
                            pf = num(vv.get("Predictive Failure Count"), 0) or 0
                            sm = str(vv.get("S.M.A.R.T alert flagged by drive", "")).lower() == "yes"
                            if pf or sm:
                                R["pds_bad"].append(("{} predictive failure {} · SMART 경고 {}".format(m.group(1), int(pf), "예" if sm else "아니오"), "warn"))
                            elif me:
                                R["pds_bad"].append(("{} media error {}".format(m.group(1), int(me)), "caution"))
    for vn, e in sorted(vd_map.items()):
        pds = e.get("pds") or pd_by_dg.get(str(e.get("dg")), [])
        meds = set(str(p_.get("Med", "")).upper() for p_ in pds)
        e["media"] = "ssd" if meds == {"SSD"} else ("hdd" if "HDD" in meds else None)
        e["pds"] = [{"id": p_.get("EID:Slt"), "state": p_.get("State"), "med": p_.get("Med"),
                     "ok": p_.get("State") in ("Onln", "GHS", "DHS", "UGood", "JBOD")} for p_ in pds]
        cc = str(e.get("cache_cur") or "")
        e["wb"] = None if not cc else ("WB" in cc)          # WB, AWB 모두 write-back. WT 는 write-through
        e["ok"] = e.get("state") == "Optl"
        R["vds"].append(e)
    for p_ in R["pd_all"]:
        st = p_.get("State")
        if st in ("Rbld",):
            R["bg"].append("{} rebuild 중".format(p_.get("EID:Slt")))
        elif st in ("Offln", "UBad", "Failed", "F"):
            R["pds_bad"].append(("{} 상태 {}".format(p_.get("EID:Slt"), st), "warn"))
    return R

def parse_ssacli(text):
    R = {"tool": "ssacli", "ctrl": [], "vds": [], "pds_bad": [], "bg": []}
    ctrl = arr = ld = pd = None
    arrays = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r'^(Smart Array|HPE Smart Array|Smart HBA|HPE [A-Z]).* in Slot (\S+)', line)
        if m:
            ctrl = {"name": line.strip(), "status": None, "cache": None, "battery": None}; R["ctrl"].append(ctrl); continue
        m = re.match(r'^\s+Array:\s*(\S+)', line)
        if m:
            arr = arrays.setdefault(m.group(1), {"lds": [], "pds": []}); ld = pd = None; continue
        m = re.match(r'^\s+Logical Drive:\s*(\d+)', line)
        if m:
            ld = {"id": m.group(1), "dev": None, "level": None, "state": None, "cache_cur": None, "pds": []}
            if arr is not None:
                arr["lds"].append(ld)
            R["vds"].append(ld); pd = None; continue
        m = re.match(r'^\s+physicaldrive\s+(\S+)', line)
        if m:
            pd = {"id": m.group(1), "state": None, "med": None}
            if arr is not None:
                arr["pds"].append(pd)
            ld = None; continue
        m = re.match(r'^\s+([^:]+):\s*(.*)$', line)
        if not m:
            continue
        k, v = m.group(1).strip(), m.group(2).strip()
        if pd is not None:
            if k == "Status":
                pd["state"] = v
            elif k == "Interface Type":
                pd["med"] = "SSD" if "Solid State" in v else ("HDD" if v else None)
            elif k == "Rotational Speed" and not pd["med"]:
                pd["med"] = "HDD"
        elif ld is not None:
            if k == "Fault Tolerance":
                ld["level"] = "RAID " + v
            elif k == "Status":
                ld["state"] = v
            elif k == "Caching":
                ld["cache_cur"] = v
            elif k == "Disk Name":
                ld["dev"] = v
        elif ctrl is not None and arr is None:
            if k == "Controller Status":
                ctrl["status"] = v
            elif k == "Cache Status":
                ctrl["cache"] = v
            elif k == "Battery/Capacitor Status":
                ctrl["battery"] = v
    for a in arrays.values():
        meds = set((p_["med"] or "") for p_ in a["pds"])
        for ld in a["lds"]:
            ld["pds"] = [{"id": p_["id"], "state": p_["state"], "med": p_["med"], "ok": (p_["state"] or "OK") == "OK"} for p_ in a["pds"]]
            ld["media"] = "ssd" if meds == {"SSD"} else ("hdd" if "HDD" in meds else None)
    for ld in R["vds"]:
        st = ld.get("state") or ""
        ld["ok"] = st == "OK"
        ld.setdefault("media", None); ld.setdefault("pds", [])
        cc = (ld.get("cache_cur") or "").lower()
        ld["wb"] = None if not cc else cc.startswith("enabled")
        if "recover" in st.lower() or "rebuild" in st.lower():
            R["bg"].append("LD {} {}".format(ld["id"], st))
        for p_ in ld["pds"]:
            if p_["state"] and p_["state"] != "OK":
                R["pds_bad"].append(("physicaldrive {} 상태 {}".format(p_["id"], p_["state"]), "warn"))
    # 컨트롤러 캐시가 꺼졌으면 LD 의 Caching: Enabled 도 실제로는 write-back 이 아니다
    for c in R["ctrl"]:
        if c.get("cache") and c["cache"] != "OK":
            for ld in R["vds"]:
                if ld.get("wb"):
                    ld["wb"] = False
    return R

def parse_arcconf(text):
    R = {"tool": "arcconf", "ctrl": [], "vds": [], "pds_bad": [], "bg": []}
    ld = None; sect = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r'^Controller information', line):
            sect = "ctrl"; R["ctrl"].append({"name": "Adaptec/Microchip", "status": None, "battery": None}); continue
        m = re.match(r'^Logical [Dd]evice number (\d+)', line)
        if m:
            sect = "ld"; ld = {"id": m.group(1), "dev": None, "level": None, "state": None, "cache_cur": None, "media": None, "pds": []}
            R["vds"].append(ld); continue
        if re.match(r'^Physical Device information', line):
            sect = "pd"; continue
        m = re.match(r'^\s+([^:]+?)\s*:\s*(.*)$', line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2).strip()
        if sect == "ctrl" and R["ctrl"]:
            if k == "controller status":
                R["ctrl"][-1]["status"] = v
            elif k in ("overall backup unit status", "status") and R["ctrl"][-1]["battery"] is None and v:
                R["ctrl"][-1]["battery"] = v
            elif k == "controller model":
                R["ctrl"][-1]["name"] = v
        elif sect == "ld" and ld is not None:
            if k == "raid level":
                ld["level"] = "RAID " + v
            elif k == "status of logical device":
                ld["state"] = v
            elif k in ("write-cache mode", "write-cache status"):
                ld["cache_cur"] = v
            elif k == "device type":
                ld["media"] = "ssd" if v.upper() == "SSD" else ("hdd" if v.upper() == "HDD" else None)
            elif k.startswith("segment") or k.startswith("group"):
                st = v.split("(")[0].strip()
                md = "SSD" if ", SSD," in v else ("HDD" if ", HDD," in v else None)
                ld["pds"].append({"id": m.group(1).strip(), "state": st, "med": md, "ok": st == "Present"})
        elif sect == "pd":
            if k == "state" and v.lower() not in ("online", "ready", "hot spare", "raw (pass through)"):
                R["pds_bad"].append(("물리 디스크 상태 {}".format(v), "warn"))
    for ld in R["vds"]:
        st = (ld.get("state") or "")
        ld["ok"] = st.lower() == "optimal"
        cc = (ld.get("cache_cur") or "").lower()
        ld["wb"] = None if not cc else ("write-back" in cc or cc in ("on", "enabled"))
        if not ld["media"]:
            meds = set(p_["med"] for p_ in ld["pds"] if p_["med"])
            ld["media"] = "ssd" if meds == {"SSD"} else ("hdd" if "HDD" in meds else None)
        if re.search(r'build|verify|rebuild|impacted', st, re.I):
            R["bg"].append("LD {} {}".format(ld["id"], st))
    return R

def load_hwraid(S, topo):
    """번들에 있는 RAID 도구 출력을 모두 읽어 OS 장치 이름에 붙인다"""
    res, absent = [], []
    for fname, fn in (("raid_storcli", parse_storcli), ("raid_ssacli", parse_ssacli), ("raid_arcconf", parse_arcconf)):
        t = rd(S, fname)
        if not t:
            continue
        if t.startswith("#TOOL_ABSENT"):
            absent.append(t.split(None, 1)[1].strip()); continue
        try:
            r = fn(t)
        except Exception:
            continue
        # OS 장치 이름이 없으면 SCSI 주소로 잇는다. megaraid 는 논리 디스크를 channel 2, target = VD 번호로,
        # aacraid 는 channel 0, target = LD 번호로 내보낸다
        drv_chan = {"storcli": ("megaraid_sas", "2"), "arcconf": ("aacraid", "0")}.get(r["tool"])
        for v in r["vds"]:
            if v.get("dev"):
                v["dev"] = v["dev"].replace("/dev/", "")
            elif drv_chan:
                for d, h in topo.hctl.items():
                    H, C, T, L = h.split(":")
                    if topo.hostdrv.get("host" + H) == drv_chan[0] and C == drv_chan[1] and T == str(v.get("id")):
                        v["dev"] = d
        res.append(r)
    return res, absent

def classify_devices(phys, topo, sto, dmtable, hwraid=None, cloud=""):
    """ES data 물리 디스크별 매체·연결 방식. bare-metal·SAN 기준값 선택에 쓴다."""
    out = {}
    raid_vd = {}
    for r in hwraid or []:
        for v in r["vds"]:
            if v.get("dev"):
                raid_vd[v["dev"]] = (r["tool"], v)
    for d in phys:
        a = topo.attr.get(d, {})
        drv = topo.hostdrv.get(topo.scsihost.get(d, ""), "")
        vendor, model = (a.get("device/vendor") or "").strip(), (a.get("device/model") or "").strip()
        info = {"drv": drv, "vendor": vendor, "model": model, "rot": a.get("queue/rotational"),
                "raid": False, "attach": "local", "media": None, "sure": True, "why": ""}
        c = nvme_ctrl(d)
        if c:
            nv = sto["nvme"].get(c, {})
            info["model"] = nv.get("model", model)
            tr = (nv.get("transport") or "pcie").lower()
            if tr != "pcie":
                info["attach"], info["media"], info["why"] = "nvmeof", "ssd", "NVMe-oF ({})".format(tr)
            elif CLOUD_BLOCK_MODEL.search(info["model"] or ""):
                info["attach"], info["media"], info["why"] = "cloud", "ssd", "클라우드 블록 볼륨 ({})".format(info["model"])
            elif CLOUD_LOCAL_MODEL.search(info["model"] or ""):
                info["media"], info["why"] = "nvme", "인스턴스 로컬 NVMe ({})".format(info["model"])
            else:
                info["media"], info["why"] = "nvme", "NVMe"
        elif re.match(r'^(rbd|nbd)\d+', d):
            info["attach"], info["media"], info["sure"] = "network", "ssd", False
            info["why"] = "Ceph RBD (네트워크 블록 장치)" if d.startswith("rbd") else "NBD (네트워크 블록 장치)"
        elif re.match(r'^xvd', d) and cloud:
            info["attach"], info["media"], info["why"] = "cloud", "ssd", "클라우드 블록 볼륨 (Xen xvd)"
        elif cloud and (CLOUD_BLOCK_MODEL.search(model) or (vendor.lower() == "msft" and "virtual disk" in model.lower())):
            info["attach"], info["media"], info["why"] = "cloud", "ssd", "클라우드 블록 볼륨 ({} {})".format(vendor, model).strip()
        elif drv in FC_DRV or drv in ISCSI_DRV or SAN_VENDOR.search(vendor):
            info["attach"], info["media"], info["sure"] = "san", "ssd", False
            info["why"] = "FC HBA" if drv in FC_DRV else ("iSCSI" if drv in ISCSI_DRV else "외부 스토리지 벤더 " + vendor)
        else:
            if drv in RAID_DRV and (drv not in ("mpt2sas", "mpt3sas", "mptsas") or RAID_MODEL.search(model)):
                info["raid"] = True
            elif RAID_MODEL.search(model) and not VM_DISK_VENDOR.search(vendor):
                info["raid"] = True
            if a.get("device/raid_level"):            # hpsa·smartpqi 는 sysfs 에 RAID 레벨을 내준다
                info["raid"] = True
            info["media"] = "hdd" if info["rot"] == "1" else "ssd"
            info["sure"] = not info["raid"]
            info["why"] = ("RAID 논리 디스크 ({}), rotational={}".format(drv or model, info["rot"]) if info["raid"]
                           else "rotational={}".format(info["rot"]))
            if d in raid_vd:
                tool, v = raid_vd[d]
                info["raid"] = True
                info["raid_level"] = v.get("level")
                if v.get("media"):
                    info["media"], info["sure"] = v["media"], True
                info["why"] = "RAID 논리 디스크 {} · 구성 디스크 {} {}개 ({} 조회)".format(
                    v.get("level") or "", (v.get("media") or "?").upper(), len(v.get("pds") or []), tool)
        out[d] = info
    # dm-multipath 로 묶인 장치는 SAN (로컬 디스크를 multipath 로 묶는 경우는 드물다)
    mp_members = set()
    for line in (dmtable or "").splitlines():
        if " multipath " in line:
            k = topo.dmname.get(line.split(":", 1)[0].strip())
            mp_members.update(topo.slaves.get(k, []))
    for d in out:
        if d in mp_members and out[d]["attach"] == "local":
            out[d]["attach"], out[d]["sure"] = "san", False
            out[d]["media"] = "ssd"
            out[d]["why"] = "dm-multipath"
    return out

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
        # 커널 5.5+ 는 flush 요청 수·시간(필드 16·17)을 준다. fsync 가 장치 캐시를 비우는 비용이 여기 보인다
        fio_, ftk_ = (dd[15], dd[16]) if len(dd) >= 17 and dd[15] >= 0 and dd[16] >= 0 else (None, None)
        rows.append({
            "t": b["t"], "dt": dt, "rio": rio, "wio": wio, "rtk": rtk, "wtk": wtk,
            "rmerge": max(dd[1], 0), "wmerge": max(dd[5], 0), "rsec": rsec, "wsec": wsec, "fio": fio_, "ftk": ftk_,
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
            "rmerge": sum(r.get("rmerge", 0) for r in rs), "wmerge": sum(r.get("wmerge", 0) for r in rs),
            "rsec": sum(r.get("rsec", 0) for r in rs), "wsec": sum(r.get("wsec", 0) for r in rs),
            "fio": sum(r["fio"] for r in rs) if all(r.get("fio") is not None for r in rs) else None,
            "ftk": sum(r["ftk"] for r in rs) if all(r.get("ftk") is not None for r in rs) else None,
            "aqu": sum(r["aqu"] for r in rs), "util": max(r["util"] for r in rs),
            "inflight": sum(r["inflight"] for r in rs),
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
            with open(os.path.join(d, f)) as fh:
                j = json.load(fh)
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
                "sync_avg": j.get("sync_avg_ms"), "engine": j.get("engine", "fio"),
            }
            if res[name]["sync_p99"]:
                res[name]["sync_p99"] /= 1e6
        except Exception:
            continue
    return res

# ─────────────────────────────────────────────────────────────────────────────
# 클러스터 관점 분석 (es_cluster_probe.sh 결과)
# ─────────────────────────────────────────────────────────────────────────────
def analyze_cluster(cdir, add, th, kind="unknown"):
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
    # data tier 가 다른 노드(hot vs warm vs cold)는 보관 용량·부하 특성이 원래 달라
    # 섞어서 비교하면 오탐이 난다. 같은 tier 안에서만 비교한다.
    def tier_of(r):
        for t in ("data_hot", "data_warm", "data_cold", "data_frozen", "data_content"):
            if t in (r["roles"] or ""):
                return t
        return "data"
    tiers = {}
    for r in data_rows:
        tiers.setdefault(tier_of(r), []).append(r)

    def outlier(key, label, unit, floor, factor=2.0):
        for tname, group in sorted(tiers.items()):
            vals = [r[key] for r in group if r[key] is not None]
            if len(vals) < 3:      # 같은 tier 노드가 3개 미만이면 중앙값이 의미 없음
                continue
            med = pctl(vals, 0.5)
            top = max(group, key=lambda r: r[key] if r[key] is not None else -1)
            if med and top[key] and top[key] >= floor and top[key] >= med * factor:
                scope = "" if len(tiers) == 1 else " ({} tier 내 비교)".format(tname)
                add("caution", "클러스터", "원인 분리 필요", "{} 쏠림. {} 노드만 유독 높음".format(label, top["name"]),
                    "{} {} vs 같은 tier 중앙값 {}{} · 비교 대상 {}개 노드".format(
                        top["name"], fmt(top[key], 1, unit), fmt(med, 1, unit), scope, len(vals)),
                    "전체가 아니라 특정 노드만 높다면 그 노드의 스토리지나 샤드 배치가 원인일 가능성이 큽니다. "
                    "클러스터 전체가 비슷하게 높다면 스토리지 공통 구간이나 워크로드 자체를 봐야 합니다.",
                    "해당 노드에서 es_disk_collect.sh 를 실행해 커널 레벨로 확인하세요.", "_nodes/stats (같은 tier 노드 간 비교)")
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
            {"vmware": "한 노드만이면 그 노드의 디스크 문제지만, 여러 노드가 동시라면 공용 스토리지(vSAN 또는 데이터스토어) 또는 인입량 자체가 원인일 가능성이 큽니다.",
             "baremetal": "한 노드만이면 그 노드의 디스크 문제지만, 여러 노드가 동시라면 인입량 자체가 원인일 가능성이 큽니다. 노드들이 같은 SAN 어레이를 쓴다면 어레이도 후보입니다."
             }.get(kind, "한 노드만이면 그 노드의 디스크 문제지만, 여러 노드가 동시라면 공용 스토리지 또는 인입량 자체가 원인일 가능성이 큽니다."),
            {"vmware": "데이터스토어(vSAN 또는 SAN·NFS) 단위 지표를 VMware 관리자와 함께 확인하고, 동시에 인입량·bulk 크기·샤드 수도 점검하세요.",
             "baremetal": "인입량·bulk 크기·샤드 수를 먼저 점검하고, 공용 SAN 이라면 같은 시각의 어레이 지표를 스토리지 관리자에게 요청하세요."
             }.get(kind, "스토리지 백엔드 지표를 인프라 관리자와 함께 확인하고, 동시에 인입량·bulk 크기·샤드 수도 점검하세요."),
            "_nodes/stats indices.indexing.throttle_time")
    aw = settings.get("cluster.routing.allocation.awareness.attributes")
    if not aw and len(data_rows) >= 3:
        if kind == "vmware":
            add("info", "클러스터", "VMware 관리자", "shard allocation awareness 미설정",
                "cluster.routing.allocation.awareness.attributes 없음 · data 노드 {}개".format(len(data_rows)),
                "ESXi 호스트 한 대에 primary와 replica를 가진 VM이 같이 올라가 있으면, 호스트 한 대가 죽을 때 두 벌을 동시에 잃습니다. ES는 VM이 어느 호스트에 있는지 모릅니다.",
                "노드에 호스트 정보를 attribute로 넣고 awareness를 설정하는 방법과, VMware 쪽 DRS anti-affinity 규칙을 함께 검토하세요.",
                "[Elastic 공식] Shard allocation awareness")
        elif kind == "baremetal":
            add("info", "클러스터", "ES 설정", "shard allocation awareness 미설정",
                "cluster.routing.allocation.awareness.attributes 없음 · data 노드 {}개".format(len(data_rows)),
                "같은 랙·같은 전원 계통에 있는 서버들이 한꺼번에 멈추면 primary와 replica를 동시에 잃을 수 있습니다. ES는 서버가 어느 랙에 있는지 모릅니다.",
                "노드에 랙이나 전원 계통 정보를 attribute(node.attr.rack_id 등)로 넣고 awareness를 설정하는 방법을 검토하세요. 한 서버에 ES 노드를 여러 개 띄웠다면 호스트 단위 attribute가 먼저입니다.",
                "[Elastic 공식] Shard allocation awareness")
        else:
            add("info", "클러스터", "ES 설정", "shard allocation awareness 미설정",
                "cluster.routing.allocation.awareness.attributes 없음 · data 노드 {}개".format(len(data_rows)),
                "물리 호스트(또는 클라우드 가용 영역) 하나에 primary와 replica를 가진 노드가 같이 있으면, 그 호스트가 멈출 때 두 벌을 동시에 잃습니다. ES는 노드가 어느 호스트에 있는지 모릅니다.",
                "노드에 호스트나 가용 영역 정보를 attribute로 넣고 awareness를 설정하세요. 가상화 쪽 anti-affinity 규칙도 함께 검토합니다.",
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

def analyze(base, storage_override=None, bench_dir=None, cluster_dir=None, platform_override=None):
    S = os.path.join(base, "static")
    meta = kv(rd(base, "meta"))
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
    # -p 로 지정한 경로: 공백 포함 경로를 위해 한 줄에 하나씩 적힌 파일을 먼저 본다
    cand = [l.strip() for l in rd(base, "user_paths").splitlines() if l.strip()]
    if not cand:
        cand = [p for p in meta.get("user_paths", "").split() if p]
    npd = dig(node_i, "settings", "path", "data")
    if npd:
        cand += npd if isinstance(npd, list) else [npd]
    for l in rd(S, "es_cmdline").splitlines():
        if "path.data=" in l:
            cand += l.split("path.data=", 1)[1].split(",")
    cand += yml_data_paths(rd(S, "es_yml"))
    cand += [l.strip() for l in rd(S, "data_paths").splitlines() if l.strip()]
    if not cand:
        cand = ["/var/lib/elasticsearch"]
    data_paths = []
    for p in cand:
        if p and p not in data_paths:
            data_paths.append(p)

    # 수집기가 ES 프로세스의 mountinfo 에서 찾은 data 장치 (컨테이너 안 ES 도 정확). 있으면 이것을 우선한다
    datadev = {}
    for l in rd(S, "datadev").splitlines():
        f_ = l.split("|")
        if len(f_) >= 7 and f_[0] == "DATADEV" and f_[3] not in ("", "?"):
            datadev[f_[1]] = {"kname": f_[3], "fs": f_[4], "src": f_[5], "mnt": f_[6]}
    path_map = []
    for p in data_paths:
        m = mount_for(p, mounts)
        k = topo.kname(m["src"]) if m else None
        dd = datadev.get(p)
        if dd and dd["kname"] != k:
            # 호스트 mount 목록에서 같은 장치를 찾아 옵션을 가져오고, 없으면 mountinfo 값으로 채운다
            hm = next((x for x in mounts if topo.kname(x["src"]) == dd["kname"]), None)
            m = {"src": dd["src"], "mnt": dd["mnt"], "host_mnt": hm["mnt"] if hm else None,
                 "fs": dd["fs"], "opts": hm["opts"] if hm else "-"}
            k = dd["kname"]
        path_map.append({"path": p, "mount": m, "kname": k, "phys": topo.physical(k) if k else [],
                         "via": "mountinfo" if dd else "mounts"})
    phys = sorted(set(d for pm in path_map for d in pm["phys"]))
    logical = sorted(set(pm["kname"] for pm in path_map if pm["kname"]))
    dev_guess = False
    if not phys:
        dev_guess = True
        phys = sorted(d for d in topo.attr if not d.startswith("dm-") and not d.startswith("md"))

    # ── 플랫폼과 판정 기준 ──────────────────────────────────────────────────
    plat = detect_platform(virt, platform_override or meta.get("platform", "auto"))
    kind = plat["kind"]
    sto = parse_storage(rd(S, "storage"))
    hwraid, raid_absent = load_hwraid(S, topo)
    devcls = classify_devices(phys, topo, sto, rd(S, "dmsetup_table"), hwraid, plat["cloud"])
    attaches = sorted(set(c["attach"] for c in devcls.values()))
    if kind in ("vmware",):
        attach = "virtual"
    elif kind == "baremetal":
        attach = ("san" if any(a_ in attaches for a_ in ("san", "nvmeof", "network"))
                  else ("cloud" if "cloud" in attaches else "local"))
    else:
        attach = "cloud" if "cloud" in attaches else ("network" if "network" in attaches else "virtual")
    req = storage_override or meta.get("storage") or "auto"
    # 0.9.x 수집기는 -s 를 안 줘도 storage=allflash 를 기록했다. 사용자 선택과 구분할 수 없으므로 auto 로 본다
    if str(meta.get("tool_version", "")).startswith("0.9") and req == "allflash" and not storage_override:
        req = "auto"
    storage_auto = req in ("", "auto")
    media_note = ""
    if not storage_auto:
        storage = req
    elif kind == "vmware":
        storage = "vmware"                  # vSAN 인지 SAN·NFS 데이터스토어인지 Guest 에서는 알 수 없다
    elif kind == "baremetal":
        order = ["hdd", "ssd", "nvme"]      # 섞여 있으면 가장 느린 매체 기준 (빠른 매체 기준을 느린 디스크에 대면 전부 오탐)
        media = [c["media"] for c in devcls.values() if c["media"]]
        storage = ("cloud" if attach == "cloud" else "network" if "network" in attaches
                   else next((m for m in order if m in media), "ssd"))
        if len(set(media)) > 1 and attach != "cloud":
            media_note = "ES data 디스크의 매체가 섞여 있어 가장 느린 {} 기준으로 판정".format(STORAGE_LABEL[storage])
    else:
        # 클라우드 인스턴스 로컬 NVMe 만 쓰면 장치 기준, 클라우드 볼륨이면 클라우드 기준, 그 밖은 VM 공통
        media = [c["media"] for c in devcls.values()]
        if devcls and all(c["attach"] == "local" and c["media"] == "nvme" and nvme_ctrl(d) for d, c in devcls.items()):
            storage = "nvme"
        elif attach == "cloud":
            storage = "cloud"
        else:
            storage = "vm"
    th = LAT_TH.get(storage, LAT_TH["allflash"])
    src_lat = (LAT_SRC["vsan"] if storage in ("allflash", "hybrid") else LAT_SRC["device"] if storage in ("nvme", "ssd", "hdd")
               else LAT_SRC["vmware"] if storage in ("vmware", "vmfs") else LAT_SRC["cloud"] if storage in ("cloud", "network") else LAT_SRC["vm"])
    # VMware 데이터스토어 종류: vsan (-s allflash|hybrid) / ds (-s vmfs) / unknown (기본)
    VMBK = "vsan" if storage in ("allflash", "hybrid") else ("ds" if storage == "vmfs" else "unknown")
    VMB = {"vsan": "vSAN", "ds": "데이터스토어 스토리지", "unknown": "vSAN·데이터스토어 스토리지"}[VMBK]
    def vs(vsan_txt, ds_txt, unknown_txt=None):
        """VMware 데이터스토어 종류에 맞는 문구"""
        return {"vsan": vsan_txt, "ds": ds_txt}.get(VMBK, unknown_txt if unknown_txt is not None else vsan_txt)
    unsure = [d for d, c in devcls.items() if not c["sure"]]
    is_vmware = kind == "vmware"
    # "OS 바깥"을 맡는 담당자와 자원 차원 이름
    if kind == "vmware":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "VMware 관리자", "VMware 자원", "VM 안", "VM 바깥(하이퍼바이저·{})".format(VMB)
    elif kind == "baremetal" and attach == "san" and "network" in attaches:
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "스토리지 관리자", "하드웨어", "서버 안", "서버 바깥(스토리지 클러스터·네트워크)"
    elif kind == "baremetal" and attach == "san":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "스토리지 관리자", "하드웨어", "서버 안(HBA 큐)", "서버 바깥(스토리지 어레이·SAN 경로)"
    elif kind == "baremetal" and attach == "local":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "하드웨어 담당자", "하드웨어", "OS 큐", "디스크·컨트롤러"
    elif attach == "cloud":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "가상화·클라우드 관리자", "가상화 자원", "VM 안", "VM 바깥(클라우드 볼륨·인스턴스 한도)"
    else:
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = "가상화·클라우드 관리자", "가상화 자원", "VM 안", "VM 바깥(하이퍼바이저·스토리지 백엔드)"

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
            # inflight: 장치에 넘겨졌으나 아직 끝나지 않은 I/O 수 (queue_depth와 직접 비교 가능)
            "inflight_p95": pctl([r.get("inflight") for r in rows], 0.95),
            "inflight_max": vmax([r.get("inflight") for r in rows]),
            # I/O 모양: 요청 1건 평균 크기와 병합 비율 (작은 무작위 I/O 인지 큰 순차 I/O 인지)
            "r_kb": (sum(r.get("rsec", 0) for r in rows) * 0.5 / trio) if trio else None,
            "w_kb": (sum(r.get("wsec", 0) for r in rows) * 0.5 / twio) if twio else None,
            "r_merge_pct": (100.0 * sum(r.get("rmerge", 0) for r in rows) / (trio + sum(r.get("rmerge", 0) for r in rows))) if trio else None,
            "w_merge_pct": (100.0 * sum(r.get("wmerge", 0) for r in rows) / (twio + sum(r.get("wmerge", 0) for r in rows))) if twio else None,
            "flush_n": sum(r["fio"] for r in rows) if rows and all(r.get("fio") is not None for r in rows) else None,
            "flush_ms": (sum(r["ftk"] for r in rows) / float(sum(r["fio"] for r in rows)))
                        if rows and all(r.get("fio") is not None for r in rows) and sum(r["fio"] for r in rows) else None,
            "flush_ps": (sum(r["fio"] for r in rows) / float(sum(r["dt"] for r in rows)))
                        if rows and all(r.get("fio") is not None for r in rows) else None,
        }
    A = dstats(agg)
    dev_stats = {d: dstats(r) for d, r in per_dev.items()}
    log_stats = {d: dstats(r) for d, r in per_log.items()}

    # ── 부하 수준 ─────────────────────────────────────────────────────────
    low_load = (A["iops_p95"] or 0) < LOW_LOAD_IOPS and (A["mb_p95"] or 0) < LOW_LOAD_MBPS

    # ── 처리량이 일정한 상한에 막히는 패턴 ─────────────────────────────────
    # 클라우드 볼륨·인스턴스 한도, VM 디스크 IOPS 한도, vSAN 정책 IOPS 한도, SAN QoS, cgroup io.max 는 모두
    # "요청은 쌓이는데(대기 I/O 증가) IOPS 나 처리량은 같은 값에서 더 오르지 않는" 모양으로 보인다.
    # 상한 근처(최대의 95% 이상)에 머문 구간이 30~90% 이고, 그 구간의 대기 I/O 가 나머지보다 2배 이상 많을 때만 본다.
    # 일정한 부하가 계속 들어오는 경우(전 구간이 같은 값)는 한도가 아니라 부하가 일정한 것이므로 제외된다
    plateau = None
    busy_rows = [r for r in agg if (r["rio"] + r["wio"]) >= MIN_IOS_PER_INTERVAL]
    if len(busy_rows) >= 10:
        for key, label, unit in (("iops", "IOPS", ""), ("mb", "처리량", " MB/s")):
            vals = [(r["rs"] + r["ws"]) if key == "iops" else (r["rmb"] + r["wmb"]) for r in busy_rows]
            mx = max(vals)
            if mx <= 0:
                continue
            top = [i for i, v in enumerate(vals) if v >= 0.95 * mx]
            rest = [i for i in range(len(vals)) if i not in top]
            share = len(top) / float(len(vals))
            if 0.3 <= share <= 0.9 and rest:
                aq_top = avg([busy_rows[i]["aqu"] for i in top]) or 0
                aq_rest = avg([busy_rows[i]["aqu"] for i in rest]) or 0
                if aq_top >= 2.0 and aq_top >= 2.0 * max(aq_rest, 0.5):
                    plateau = (label, mx, unit, share, aq_top, aq_rest)
                    break
    if plateau:
        label, mx, unit, share, aq_top, aq_rest = plateau
        causes = {
            "vmware": "VM 디스크의 IOPS 한도(Storage I/O Control), vSAN 스토리지 정책의 IOPS 한도, 데이터스토어 어레이의 QoS",
            "baremetal": ("스토리지 어레이의 볼륨 QoS, HBA·경로 대역폭" if attach == "san" else
                          "장치 자체의 최대 성능, cgroup I/O 제한, RAID 컨트롤러 처리 한계"),
        }.get(kind, "클라우드 볼륨의 IOPS·처리량 한도, 인스턴스 유형의 스토리지 대역폭 한도, 하이퍼바이저의 디스크 I/O 제한"
              if attach == "cloud" else "하이퍼바이저의 디스크 I/O 제한, 스토리지 백엔드 QoS")
        add("caution", "포화", OUT, "{} 일정한 상한에서 더 오르지 않음. 한도(QoS·볼륨 한도)에 걸린 패턴".format("IOPS가" if label == "IOPS" else "처리량이"),
            "{} 최대 {} 근처에 머문 구간 {:.0f}% · 그 구간 대기 I/O 평균 {} (나머지 구간 {})".format(
                label, fmt(mx, 0 if not unit else 1, unit), share * 100, fmt(aq_top, 1), fmt(aq_rest, 1)),
            "요청은 쌓이는데 {} 같은 값에서 멈춰 있습니다. 장치가 느려진 것이 아니라 어딘가에서 상한을 걸고 있을 때 나타나는 모양입니다. "
            "전형적인 원인은 {}입니다.".format("IOPS는" if label == "IOPS" else "처리량은", causes),
            "{}에게 이 값({})이 설정된 한도와 같은지 확인 요청하세요. 한도라면 한도 상향이나 볼륨 분산이 해결책이고, 디스크 교체는 효과가 없습니다.".format(
                OUT, fmt(mx, 0 if not unit else 1, unit)),
            "[실무 기준] 상한 근처 구간 30~90%, 대기 I/O 2배 이상일 때 판정")

    # ═════════════ 1. 지연 ═════════════
    r_sev, w_sev = grade(A["r_await_p95"], th), grade(A["w_await_p95"], th)
    lat_ev = "읽기 p95 {} / 평균 {} · 쓰기 p95 {} / 평균 {} (I/O {}건 이상 구간만 집계: 읽기 {}구간, 쓰기 {}구간)".format(
        fmt(A["r_await_p95"], 2, "ms"), fmt(A["r_await_mean"], 2, "ms"),
        fmt(A["w_await_p95"], 2, "ms"), fmt(A["w_await_mean"], 2, "ms"),
        MIN_IOS_PER_INTERVAL, A["valid_r"], A["valid_w"])
    lat_sev = sev_max(r_sev, w_sev)
    if A["valid_r"] + A["valid_w"] == 0:
        add("info", "지연", "참고", "측정 구간에 디스크 I/O가 거의 없어 응답시간을 평가할 수 없음",
            lat_ev, "I/O가 적은 구간의 응답시간은 한두 건의 느린 요청에 좌우되어 신뢰할 수 없습니다.",
            "인덱싱·검색 피크 시간대에 다시 측정하세요 (-d 600 이상 권장).", src_lat)
        lat_sev = "na"
    elif lat_sev in ("ok",):
        add("ok", "지연", "참고", "디스크 응답시간 정상 범위", lat_ev,
            {"vmware": "Guest가 본 응답시간(await)은 {}·하이퍼바이저·가상 SCSI를 모두 거친 결과라 ES가 실제로 겪는 지연과 같습니다.".format(VMB),
             "baremetal": "OS가 본 응답시간(await)은 block layer 대기와 장치 처리 시간을 합친 값이라 ES가 실제로 겪는 지연과 같습니다."}.get(kind,
             "OS가 본 응답시간(await)은 하이퍼바이저와 스토리지 백엔드를 모두 거친 결과라 ES가 실제로 겪는 지연과 같습니다."),
            "조치 불필요. 피크 시간대 재측정으로 여유를 확인하세요.", src_lat)
    else:
        add(lat_sev, "지연", "원인 분리 필요",
            "디스크 응답시간이 {} 기준 {} 수준".format(
                {"allflash": "All-Flash", "hybrid": "Hybrid"}.get(storage, STORAGE_LABEL.get(storage, storage)), SEV_LABEL[lat_sev]),
            lat_ev,
            "쓰기 지연은 translog fsync와 segment flush를 늦춰 인덱싱 지연으로, 읽기 지연은 page cache에 없는 segment 조회를 늦춰 검색 지연으로 바로 이어집니다.",
            "아래 '병목 위치' 판정을 먼저 확인하세요. {} 큐가 원인이면 서버에서, 아니면 {} 쪽에서 풀어야 합니다.".format(
                "Guest" if kind in ("vmware", "vm", "unknown") else "OS", OUT.replace(" 관리자", "").replace(" 담당자", "")),
            src_lat)

    # 병목 위치: 큐 사용률과 응답시간의 조합
    qd = [num(topo.attr.get(d, {}).get("device/queue_depth")) for d in phys]
    qd = [q for q in qd if q]
    qd_total = sum(qd) if qd else None
    qratio = (A["aqu_p95"] / qd_total) if (qd_total and A["aqu_p95"] is not None) else None
    # inflight(장치에 넘겨져 처리 중인 I/O)는 queue_depth와 직접 비교 가능한 값이라 교차 확인에 쓴다.
    # aqu-sz 는 블록 계층 큐(nr_requests)에서 대기 중인 요청까지 포함하므로 queue_depth 를 넘을 수 있다.
    iratio = (A["inflight_p95"] / qd_total) if (qd_total and A["inflight_p95"] is not None) else None
    q_ev = "평균 대기 I/O(aqu-sz) p95 {}{} / 디바이스 queue_depth 합계 {}".format(
        fmt(A["aqu_p95"], 1),
        " · 장치 처리 중(inflight) p95 {}".format(fmt(A["inflight_p95"], 1)) if A["inflight_p95"] is not None else "",
        int(qd_total) if qd_total else "미확인")
    QSRC = ("[VMware 공식] KB 2053145. PVSCSI 기본 큐 64(device)/254(adapter), ring_pages 8→32 및 cmd_per_lun 254 권장. "
            "게스트 내부 지연과 VM/VMDK 레벨 지연의 차이가 큐 깊이 낮은 컨트롤러의 큐 고갈에서 비롯될 수 있다는 서술은 "
            "Broadcom 'Troubleshooting vSAN Performance'. 큐 사용률 구간(40%·80%)은 실무 기준")
    if kind == "vmware" and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
        if qratio is None:
            add("caution", "지연", "원인 분리 필요", "병목 위치 판정 보류: queue_depth를 읽지 못함",
                q_ev, "queue_depth를 모르면 지연이 VM 안의 큐에서 생긴 것인지 밖에서 생긴 것인지 가를 수 없습니다.",
                "/sys/block/<장치>/device/queue_depth 를 읽을 수 있는 권한(root)으로 재측정하세요. "
                "그 전까지는 VM 안·밖 양쪽을 함께 확인해야 합니다.", QSRC)
        elif qratio >= 0.8 or (iratio is not None and iratio >= 0.8):
            add("warn", "포화", "서버 담당자",
                "병목 위치: Guest 쪽 큐가 가득 참 (큐 사용률 {:.0f}%)".format(qratio * 100),
                q_ev,
                "가상 디스크가 동시에 받을 수 있는 I/O 수가 한계에 닿아, 요청이 VM 안에서 줄을 서고 있습니다. 백엔드가 빨라도 이 구간은 느려집니다.",
                "ES data용 VMDK를 여러 개로 나눠 별도 PVSCSI 컨트롤러에 붙이고 LVM stripe로 묶는 방법이 가장 효과적입니다. "
                "그다음 PVSCSI queue depth 상향(cmd_per_lun=254, ring_pages=32, 재부팅 필요)을 검토하세요.", QSRC)
        elif qratio >= 0.4:
            # 큐도 깊고 지연도 높다 → 한쪽으로 단정할 수 없는 구간
            add("warn" if lat_sev in ("warn", "crit") else "caution", "지연", "원인 분리 필요",
                "병목 위치: 큐도 깊고 지연도 높음. VM 안과 밖을 함께 확인 (큐 사용률 {:.0f}%)".format(qratio * 100),
                q_ev,
                "큐가 절반 이상 차 있으면서 응답시간도 높습니다. 백엔드가 느려서 요청이 밀려 큐가 쌓인 것일 수도 있고, "
                "큐가 좁아서 대기가 길어진 것일 수도 있어 한쪽으로 단정할 수 없습니다. 두 원인은 함께 나타나는 경우가 많습니다.",
                "VMware 관리자에게 같은 시각의 esxtop DAVG(백엔드)와 KAVG(커널·큐 대기) 분리 확인을 요청하세요. "
                "DAVG가 크면 VM 바깥, KAVG가 크면 큐 쪽입니다. 장치 레벨 기대치는 Broadcom KB 424485 기준으로 "
                "NVMe 0.5ms 미만, SAS/SATA SSD 1ms 내외, HDD 10~20ms 입니다. "
                "동시에 Guest에서는 VMDK 분할 + 별도 PVSCSI 컨트롤러로 큐를 넓히는 방안을 검토합니다.", QSRC)
        else:
            add("warn" if lat_sev in ("warn", "crit") else "caution", "지연", "VMware 관리자",
                "병목 위치: {} 가능성 높음".format(WHERE_OUT),
                q_ev + " → 큐 사용률 {:.0f}%".format(qratio * 100),
                "VM 안에서 기다리는 요청이 적은데도 한 건 한 건이 느리다는 뜻입니다. " + vs(
                    "vSAN resync, 캐시 계층 포화, 같은 호스트 다른 VM의 I/O 경합, vSAN 네트워크 지연이 전형적인 원인입니다. ",
                    "데이터스토어가 있는 스토리지 어레이의 부하, SAN·NFS 경로 지연, 같은 데이터스토어를 쓰는 다른 VM의 I/O 경합, "
                    "Storage I/O Control·디스크 IOPS 한도가 전형적인 원인입니다. ",
                    "vSAN이면 resync·캐시 계층 포화·vSAN 네트워크 지연, SAN·NFS 데이터스토어면 스토리지 어레이 부하·경로 지연이, "
                    "공통으로는 같은 호스트·데이터스토어를 쓰는 다른 VM의 I/O 경합이 전형적인 원인입니다. ")
                + "Guest 설정 변경으로는 개선되지 않습니다.",
                "측정 시각과 이 리포트를 VMware 관리자에게 전달하고 esxtop의 DAVG/KAVG/GAVG, " + vs(
                    "vSAN 성능 서비스의 VM·디스크 그룹 지연, resync 진행 여부를",
                    "데이터스토어와 스토리지 어레이 볼륨의 응답시간, 경로 상태를",
                    "이 VM의 데이터스토어 종류(vSAN 또는 SAN·NFS)와 그에 맞는 지연 지표(vSAN 성능 서비스 또는 어레이 볼륨 응답시간)를")
                + " 같은 시각으로 확인 요청하세요. 장치 레벨 기대치는 Broadcom KB 424485 기준으로 "
                "NVMe 0.5ms 미만, SAS/SATA SSD 1ms 내외, HDD 10~20ms 입니다. 이 범위를 넘으면 백엔드 쪽을 먼저 봅니다.", QSRC)
    # 쓰기만 느림 → vSAN 쓰기 경로 힌트
    write_only = bool(A["w_await_p95"] and A["r_await_p95"] and A["valid_w"] >= 3 and A["valid_r"] >= 3
                      and A["w_await_p95"] >= th["caution"] and A["w_await_p95"] > 3 * A["r_await_p95"])
    if kind == "vmware" and write_only:
        vsan_why = ("vSAN은 쓰기를 복제본 전부에서 확인받아야 끝납니다(RAID-1 FTT=1이면 호스트 2대). 그래서 쓰기만 느리면 vSAN 네트워크, "
                    "쓰기 버퍼 destage, RAID-5/6 정책의 read-modify-write를 의심할 수 있습니다. Guest에서는 vSAN 네트워크를 직접 볼 수 없어 추정입니다.")
        ds_why = ("읽기는 어레이 캐시가 받아 주지만 쓰기는 어레이가 기록을 확인해야 끝납니다. 쓰기만 느리면 어레이 쓰기 캐시 포화, "
                  "동기 복제, RAID 5/6 볼륨의 read-modify-write, SAN·NFS 경로 지연을 의심할 수 있습니다.")
        add("caution", "지연", "VMware 관리자", vs("쓰기만 유독 느림. vSAN 쓰기 경로 확인 필요", "쓰기만 유독 느림. 스토리지 쓰기 경로 확인 필요",
                                                  "쓰기만 유독 느림. 스토리지 쓰기 경로 확인 필요"),
            "쓰기 p95 {} vs 읽기 p95 {}".format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
            vs(vsan_why, ds_why, "vSAN 데이터스토어라면: " + vsan_why + " SAN·NFS 데이터스토어라면: " + ds_why),
            vs("VMware 관리자에게 vSAN 네트워크 지연·재전송, 쓰기 버퍼 사용률, 스토리지 정책(RAID/FTT)을 확인 요청하세요.",
               "VMware·스토리지 관리자에게 어레이 쓰기 캐시 상태, 볼륨 RAID 레벨·복제 설정, 경로 지연을 확인 요청하세요.",
               "VMware 관리자에게 데이터스토어 종류를 먼저 확인하고, vSAN이면 vSAN 네트워크·쓰기 버퍼·스토리지 정책을, "
               "SAN·NFS면 어레이 쓰기 캐시·볼륨 복제 설정을 확인 요청하세요."),
            vs("[VMware 공식] Broadcom vSAN 문서 (쓰기 경로·복제 동작)", "스토리지 쓰기 경로 일반 동작",
               "[VMware 공식] Broadcom vSAN 문서 (쓰기 경로·복제 동작), 스토리지 쓰기 경로 일반 동작"))

    # ── vSAN 이 아닌 플랫폼의 병목 위치 ─────────────────────────────────────
    # 같은 원리(대기 I/O ÷ queue_depth)를 쓰되, "바깥"이 무엇인지와 담당자가 다르다.
    #  bare-metal 로컬 : 큐가 차면 장치가 동시 처리 한계(포화), 큐가 비었는데 느리면 장치 자체 이상
    #  bare-metal SAN  : 큐가 차면 HBA LUN 큐, 비었는데 느리면 어레이·패브릭
    #  기타 VM·클라우드 : vSAN 과 같은 구도지만 백엔드를 특정하지 않는다
    GSRC = ("큐 사용률 구간(40%·80%)은 실무 기준. queue_depth 는 /sys/block/<장치>/device/queue_depth, "
            "장치 기대 지연은 Broadcom KB 424485 (NVMe 0.5ms 미만, 엔터프라이즈 SSD 1ms 이하, HDD 10~20ms)")
    no_qd_dev = [d for d in phys if not topo.attr.get(d, {}).get("device/queue_depth")]
    if kind != "vmware" and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
        hi_sev = "warn" if lat_sev in ("warn", "crit") else "caution"
        if qratio is None and no_qd_dev and meta.get("is_root") != "0":
            # NVMe, virtio-blk 은 queue_depth 개념이 SCSI 와 달라 파일이 없다. 권한 문제가 아니다
            if kind == "baremetal" and "network" in attaches:
                add(hi_sev, "지연", OUT, "병목 위치: 네트워크 블록 스토리지(Ceph RBD 등) 쪽 가능성",
                    q_ev + " · 대상 장치: {}".format(", ".join(no_qd_dev)),
                    "RBD·NBD 같은 네트워크 블록 장치는 요청 하나하나가 네트워크를 건너 스토리지 클러스터에서 처리됩니다. "
                    "서버에서 본 지연의 대부분은 스토리지 클러스터(OSD)의 처리 시간과 네트워크 왕복 시간입니다.",
                    "스토리지 관리자에게 같은 시각의 Ceph OSD commit·apply 지연, 복구(recovery·backfill) 진행 여부, 스토리지 네트워크 상태를 확인 요청하세요. "
                    "Elastic은 로컬 직결 스토리지가 일반적으로 더 빠르다고 설명합니다. 지연 요구가 높으면 로컬 PV 를 검토합니다.",
                    "[Elastic 공식] Tune for indexing/search speed (로컬 직결 스토리지 권장)")
            elif kind == "baremetal":
                add(hi_sev, "지연", "원인 분리 필요", "병목 위치: 큐 기준 판정 대신 장치 상태를 함께 확인",
                    q_ev + " · queue_depth 없는 장치: {}".format(", ".join(no_qd_dev)),
                    "NVMe는 큐가 수만 개 단위라 대기 I/O ÷ queue_depth 로 포화를 가를 수 없습니다. "
                    "대기 I/O(aqu-sz)와 %util이 함께 높으면 부하가 장치 능력을 넘은 것이고, 부하는 그대로인데 지연만 높으면 "
                    "온도 제한(thermal throttling), PCIe 링크 저하, 펌웨어, 여유 공간 부족에 따른 쓰기 성능 저하를 의심합니다.",
                    "아래 '하드웨어' 항목의 NVMe 온도·링크 판정과 커널 로그를 먼저 보세요. 부하 때문이라면 디스크를 늘려 stripe로 묶거나 "
                    "샤드를 다른 노드로 분산합니다. 장치 이상이 의심되면 --smart 로 다시 수집해 SMART 상태를 확인하세요.", GSRC)
            else:
                add(hi_sev, "지연", "원인 분리 필요", "병목 위치 판정 보류: 이 가상 디스크는 queue_depth를 제공하지 않음",
                    q_ev + " · 대상 장치: {}".format(", ".join(no_qd_dev)),
                    "virtio-blk나 가상 NVMe는 SCSI queue_depth 파일이 없어 VM 안의 큐에서 막혔는지 가를 수 없습니다.",
                    "{}에게 같은 시각의 호스트 쪽 디스크 지연(백엔드)을 확인 요청하세요. VM 안에서는 aqu-sz 가 계속 높고 %util 이 "
                    "100%에 붙어 있으면 가상 디스크를 늘려 stripe로 묶는 방안을 검토합니다.".format(OUT), src_lat)
        elif qratio is None:
            add("caution", "지연", "원인 분리 필요", "병목 위치 판정 보류: queue_depth를 읽지 못함",
                q_ev, "queue_depth를 모르면 지연이 {}에서 생긴 것인지 {}에서 생긴 것인지 가를 수 없습니다.".format(WHERE_IN, WHERE_OUT),
                "/sys/block/<장치>/device/queue_depth 를 읽을 수 있는 권한(root)으로 재측정하세요.", GSRC)
        elif qratio >= 0.8 or (iratio is not None and iratio >= 0.8):
            if kind == "baremetal" and attach == "san":
                add("warn", "포화", "서버 담당자", "병목 위치: 서버 쪽 LUN 큐가 가득 참 (큐 사용률 {:.0f}%)".format(qratio * 100), q_ev,
                    "LUN 하나가 동시에 받을 수 있는 I/O 수(HBA queue depth)가 한계에 닿아 요청이 서버 안에서 줄을 서고 있습니다. "
                    "어레이가 빨라도 이 구간은 느려집니다.",
                    "ES data용 LUN을 여러 개로 나눠 LVM stripe로 묶는 방법이 가장 효과적입니다. multipath 경로가 모두 active인지 확인하고, "
                    "HBA LUN queue depth 상향은 스토리지 벤더 권고 범위 안에서 스토리지 관리자와 함께 검토하세요.", GSRC)
            elif kind == "baremetal":
                add("warn", "포화", "서버 담당자", "병목 위치: 디스크가 동시 처리 한계에 도달 (큐 사용률 {:.0f}%)".format(qratio * 100), q_ev,
                    "장치가 한 번에 받을 수 있는 I/O가 가득 찬 상태로 요청이 계속 들어오고 있습니다. 장치 이상이라기보다 부하가 이 디스크 구성의 능력을 넘은 것입니다.",
                    "디스크를 늘려 RAID 0 또는 LVM stripe로 묶거나, 더 빠른 매체(HDD라면 SSD, SATA SSD라면 NVMe)를 검토하세요. "
                    "ES 쪽에서는 이 노드의 샤드 수와 인덱싱 몰림을 함께 확인합니다.",
                    "[Elastic 공식] Tune for indexing speed (SSD, RAID 0 stripe). 큐 사용률 구간은 실무 기준")
            else:
                add("warn", "포화", "서버 담당자", "병목 위치: VM 안의 큐가 가득 참 (큐 사용률 {:.0f}%)".format(qratio * 100), q_ev,
                    "가상 디스크가 동시에 받을 수 있는 I/O 수가 한계에 닿아 요청이 VM 안에서 줄을 서고 있습니다. 백엔드가 빨라도 이 구간은 느려집니다.",
                    "가상 디스크를 여러 개로 나눠 LVM stripe로 묶는 방법이 가장 효과적입니다. 가상 컨트롤러의 큐 설정은 하이퍼바이저마다 다르므로 {}와 함께 검토하세요.".format(OUT), GSRC)
        elif qratio >= 0.4:
            add(hi_sev, "지연", "원인 분리 필요",
                "병목 위치: 큐도 깊고 지연도 높음. {} 쪽과 {} 쪽을 함께 확인 (큐 사용률 {:.0f}%)".format(WHERE_IN, WHERE_OUT, qratio * 100), q_ev,
                "큐가 절반 이상 차 있으면서 응답시간도 높습니다. 장치 쪽이 느려서 요청이 밀린 것일 수도 있고, 큐가 좁아서 대기가 길어진 것일 수도 있어 한쪽으로 단정할 수 없습니다.",
                {"san": "스토리지 관리자에게 같은 시각의 어레이 쪽 응답시간(호스트 포트·볼륨 단위)을 요청해 서버에서 본 값과 비교하세요. "
                        "어레이 값이 낮으면 서버 쪽 큐·경로, 높으면 어레이 쪽입니다.",
                 "local": "커널 로그와 디스크 상태(SMART, RAID 컨트롤러 이벤트)를 확인하고, 이상이 없으면 부하 분산이나 디스크 증설로 큐를 넓히는 방안을 검토하세요."
                 }.get(attach, "{}에게 같은 시각의 호스트 쪽 디스크 지연을 요청해 VM 안에서 본 값과 비교하세요.".format(OUT)), GSRC)
        else:
            if kind == "baremetal" and attach == "san":
                ttl, why, act = ("병목 위치: 서버 바깥(스토리지 어레이·SAN 경로) 가능성 높음",
                    "서버 안에서 기다리는 요청이 적은데도 한 건 한 건이 느리다는 뜻입니다. 어레이 컨트롤러 부하, 같은 어레이를 쓰는 다른 서버, "
                    "SAN 스위치 포트 혼잡, 경로 하나의 장애가 전형적인 원인입니다. 서버 설정 변경으로는 개선되지 않습니다.",
                    "측정 시각과 이 리포트를 스토리지 관리자에게 전달하고, 같은 시각의 어레이 볼륨·호스트 포트 응답시간과 SAN 스위치 포트 오류를 확인 요청하세요.")
            elif kind == "baremetal":
                ttl, why, act = ("병목 위치: 디스크·컨트롤러 자체가 느림 (큐 여유 있음)",
                    "기다리는 요청이 적은데도 한 건 한 건이 느립니다. 부하가 아니라 장치 쪽 상태 문제일 가능성이 큽니다. "
                    "불량 섹터가 생긴 디스크, RAID 재구성·일관성 검사·patrol read, 배터리 학습 주기나 배터리 이상으로 컨트롤러 쓰기 캐시가 꺼진 경우가 전형적입니다.",
                    "커널 로그의 디스크·컨트롤러 오류를 먼저 확인하고, 하드웨어 담당자에게 RAID 컨트롤러 이벤트 로그와 캐시·배터리 상태, 디스크 SMART 상태를 "
                    "같은 시각 기준으로 확인 요청하세요. 장치 기대 지연은 NVMe 0.5ms 미만, 엔터프라이즈 SSD 1ms 이하, HDD 10~20ms 입니다.")
            else:
                ttl, why, act = ("병목 위치: VM 바깥(하이퍼바이저·스토리지 백엔드) 가능성 높음",
                    "VM 안에서 기다리는 요청이 적은데도 한 건 한 건이 느리다는 뜻입니다. 같은 호스트·스토리지를 쓰는 다른 VM과의 경합, "
                    "스토리지 백엔드 지연, 볼륨의 IOPS·처리량 한도가 전형적인 원인입니다. VM 설정 변경으로는 개선되지 않습니다.",
                    "측정 시각과 이 리포트를 {}에게 전달하고 같은 시각의 호스트·볼륨 단위 지연과 한도 도달 여부를 확인 요청하세요.".format(OUT))
            add(hi_sev, "지연", OUT, ttl, q_ev + " → 큐 사용률 {:.0f}%".format(qratio * 100), why, act, GSRC)
    def raid_cache_note():
        vds = [v for r in hwraid for v in r["vds"] if v.get("dev") in phys]
        if vds and any(v.get("wb") is False for v in vds):
            return "컨트롤러 조회 결과 ES data 논리 디스크가 write-through로 동작 중입니다. 아래 '하드웨어' 항목의 캐시·배터리 판정을 먼저 보세요."
        if vds and all(v.get("wb") for v in vds):
            return ("컨트롤러 조회 결과 쓰기 캐시는 write-back으로 정상입니다. 캐시 문제가 아니므로 RAID 레벨({})과 구성 디스크 상태, "
                    "쓰기 양 자체를 보세요.".format(", ".join(sorted(set(str(v.get("level")) for v in vds)))))
        return "하드웨어 담당자에게 컨트롤러 캐시 정책(현재 write-back인지), 배터리·캐시 모듈 상태, 논리 디스크 RAID 레벨을 확인 요청하세요."
    if kind != "vmware" and write_only:
        if kind == "baremetal" and attach == "local" and (storage == "hdd" or any(c["raid"] for c in devcls.values())):
            add("caution", "지연", OUT, "쓰기만 유독 느림. RAID 컨트롤러 쓰기 캐시 확인 필요",
                "쓰기 p95 {} vs 읽기 p95 {}".format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                "RAID 컨트롤러는 배터리(또는 flash) 보호 캐시로 쓰기를 먼저 받고 나중에 디스크에 씁니다. 배터리 학습 주기, 배터리 이상, "
                "정책 변경으로 캐시가 write-through로 바뀌면 fsync마다 디스크까지 가야 해서 쓰기만 크게 느려집니다. "
                "RAID 5/6은 쓰기마다 읽기-수정-쓰기가 생기는 것도 원인입니다.",
                raid_cache_note(),
                "RAID 컨트롤러 캐시 동작 (벤더 공통)" + (" · 컨트롤러 도구 조회 결과 반영" if hwraid else ". 컨트롤러 도구가 없어 캐시 상태는 추정"))
        elif kind == "baremetal" and attach == "local":
            add("caution", "지연", OUT, "쓰기만 유독 느림. SSD 쓰기 성능 저하 가능성",
                "쓰기 p95 {} vs 읽기 p95 {}".format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                "SSD는 여유 블록이 부족해지면 쓰기 전에 지우는 작업(garbage collection)이 겹쳐 쓰기 지연이 급격히 늘어납니다. "
                "Broadcom은 SSD 지연이 2~3ms를 계속 넘으면 이 'write cliff'에 가까워진 신호로 설명합니다.",
                "디스크 사용률과 TRIM(fstrim.timer) 동작 여부를 확인하고, 하드웨어 담당자에게 SSD 수명·여유 공간(SMART)을 확인 요청하세요.",
                "[VMware 공식] Broadcom KB 424485 (SSD write cliff)")
        else:
            add("caution", "지연", OUT, "쓰기만 유독 느림. 스토리지 쓰기 경로 확인 필요",
                "쓰기 p95 {} vs 읽기 p95 {}".format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                "읽기는 캐시가 받아 주지만 쓰기는 끝까지 기록돼야 끝납니다. 스토리지 쪽 복제, 쓰기 캐시 포화, 볼륨 처리량 한도가 쓰기에 먼저 나타납니다.",
                "{}에게 같은 시각의 쓰기 지연, 쓰기 캐시 상태, 볼륨 한도 도달 여부를 확인 요청하세요.".format(OUT),
                "스토리지 쓰기 경로 일반 동작")

    # ── 묶음 안에서 한 장치만 느린가 ────────────────────────────────────────
    # 여러 디스크를 RAID 0·LVM stripe·md 로 묶으면 합산 지표에서는 한 디스크의 지연이 희석된다.
    # 그런데 stripe 는 가장 느린 구성원 속도로 움직이므로 한 디스크만 느려도 전체가 느려진다.
    # multipath 면 같은 LUN 의 경로끼리 비교하게 되어 "경로 하나만 느림"을 잡는다.
    def worst_p95(st):
        return max([x for x in (st.get("r_await_p95"), st.get("w_await_p95")) if x is not None] or [None])
    busy_devs = {d: st for d, st in dev_stats.items() if (st.get("valid_r", 0) + st.get("valid_w", 0)) >= 3}
    if len(busy_devs) >= 2:
        vals = {d: worst_p95(st) for d, st in busy_devs.items() if worst_p95(st) is not None}
        for d, v in sorted(vals.items(), key=lambda x: -x[1])[:1]:
            others = [x for k, x in vals.items() if k != d]
            med = pctl(others, 0.5) if others else None
            if med and v >= th["caution"] and v >= 2.0 * med:
                paths = attach == "san" and any("multipath" in (devcls.get(k, {}).get("why") or "") for k in vals)
                ev_ = "{} p95 {} vs 나머지 {}개 중앙값 {}".format(d, fmt(v, 2, "ms"), len(others), fmt(med, 2, "ms"))
                if paths:
                    add("warn", "지연", "스토리지 관리자", "multipath 경로 중 하나만 느림 ({})".format(d), ev_,
                        "같은 LUN으로 가는 경로끼리 지연이 크게 다릅니다. 느린 경로의 HBA 포트, 케이블, 스위치 포트, 어레이 컨트롤러 포트 중 하나가 원인입니다.",
                        "multipath -ll 로 경로별 상태를 보고, 스토리지 관리자에게 해당 경로의 스위치·어레이 포트 오류 카운터 확인을 요청하세요.",
                        "경로 간 비교 (/proc/diskstats)")
                elif kind == "baremetal":
                    add("warn", "지연", OUT, "묶음 디스크 중 하나만 느림. 불량 디스크 후보 ({})".format(d), ev_,
                        "RAID 0·LVM stripe는 모든 디스크가 함께 움직여야 요청 하나가 끝나므로, 가장 느린 디스크 속도로 전체가 느려집니다. "
                        "한 디스크만 뚜렷하게 느리면 불량 섹터 재시도, 펌웨어 문제, 수명 말기가 전형적인 원인입니다. 합산 지표에서는 이 차이가 희석돼 보이지 않습니다.",
                        "해당 디스크의 SMART(--smart 로 재수집)와 커널 로그를 확인하고, 하드웨어 담당자와 교체 여부를 검토하세요.",
                        "디스크 간 비교 (/proc/diskstats)")
                else:
                    add("warn", "지연", OUT, "가상 디스크 중 하나만 느림 ({})".format(d), ev_,
                        "같은 VM의 가상 디스크끼리 지연이 크게 다르면, 그 디스크만 다른 데이터스토어·스토리지 정책·백엔드에 있을 가능성이 큽니다. "
                        "stripe 로 묶었다면 전체가 이 디스크 속도로 느려집니다.",
                        "{}에게 해당 가상 디스크의 위치(데이터스토어)와 스토리지 정책을 확인 요청하세요.".format(OUT),
                        "디스크 간 비교 (/proc/diskstats)")
                # 합산 지표가 정상이어도 stripe 전체는 이 디스크 속도로 움직이므로 지연 차원에 반영한다
                lat_sev = sev_max(lat_sev, grade(v, th))

    # ── 측정 환경 ───────────────────────────────────────────────────────────
    mapped = bool(path_map) and all(pm.get("via") == "mountinfo" for pm in path_map)
    if plat["container"] and mapped:
        add("info", "측정 환경", "참고", "컨테이너 안에서 실행됨. data 장치는 mount 정보로 확인",
            "실행 환경: {} · data 장치: {}".format(plat["container"], ", ".join("{} → {}".format(pm["path"], pm["kname"]) for pm in path_map)),
            "data 경로가 올라간 블록 장치는 mountinfo 로 정확히 찾았습니다. 다만 /proc/diskstats 는 호스트 전체 값이라 같은 디스크를 쓰는 "
            "다른 컨테이너의 I/O가 함께 들어 있고, ES 프로세스 정보는 권한에 따라 빠질 수 있습니다.",
            "가능하면 노드(호스트)에서 root 로 실행하세요. 호스트에서 실행해도 컨테이너 안 ES 를 자동으로 찾습니다.",
            "/proc/<pid>/mountinfo, systemd-detect-virt -c")
    elif plat["container"]:
        add("caution", "측정 환경", "참고", "컨테이너 안에서 실행됨. 호스트에서 다시 실행 권장",
            "실행 환경: {} · 하부 플랫폼: {}".format(plat["container"], HV_LABEL.get(plat["hv"], plat["hv"]) if plat["hv"] else "미확인"),
            "컨테이너 안에서도 /proc/diskstats 는 호스트 전체 디스크 값이라, 다른 컨테이너의 I/O까지 섞여 있습니다. "
            "sysctl·block 장치 설정은 호스트 값이 보이지만 ES 프로세스와 data 경로는 컨테이너 기준이라 경로와 장치를 잘못 연결할 수 있습니다.",
            "ES 가 컨테이너(ECK, Docker)로 떠 있다면 이 도구는 컨테이너가 아니라 그 노드(호스트)에서 root 로 실행하세요. "
            "ES data 경로는 호스트에서 본 volume 경로(-p)로 지정합니다.",
            "systemd-detect-virt -c")
    if meta.get("es_in_container") == "1":
        add("info", "측정 환경", "참고", "ES 가 컨테이너(Docker·Kubernetes) 안에서 실행 중",
            "data 장치: " + (", ".join("{} → {} ({})".format(pm["path"], pm["kname"] or "?", pm.get("via")) for pm in path_map) or "미확인"),
            "호스트에서 실행해 컨테이너 안 ES 의 mount 정보로 data 장치를 찾았습니다. Kubernetes 라면 이 장치가 로컬 PV 인지 "
            "네트워크 스토리지(CSI: EBS, Ceph RBD, iSCSI 등)인지에 따라 판정 기준이 달라지며, 위 장치 종류 판정에 반영했습니다.",
            "Kubernetes 에서는 shard allocation awareness 를 노드가 아니라 가용 영역(topology.kubernetes.io/zone) 기준으로 두는 것이 일반적입니다.",
            "/proc/<pid>/mountinfo, [Elastic 공식] Shard allocation awareness")
    if kind == "unknown":
        add("info", "측정 환경", "참고", "플랫폼을 확정하지 못해 공통 기준으로 판정",
            " · ".join(plat["evidence"]) or "systemd-detect-virt 결과와 DMI 정보 없음",
            "가상화 여부를 확인할 근거가 부족합니다. bare-metal 로 단정하면 매체별 기준이, VM 으로 단정하면 VM 기준이 적용돼 판정이 달라집니다.",
            "--platform baremetal|vmware|vm 으로 지정해 다시 분석하세요 (번들만 있으면 PC에서 es_disk_render.py --platform 으로 가능).",
            "systemd-detect-virt, /sys/class/dmi/id, /proc/cpuinfo")
    if media_note:
        add("info", "측정 환경", "참고", "ES data 디스크의 매체가 섞여 있음", media_note + " · " + ", ".join(
            "{}={}".format(d, STORAGE_LABEL.get(c["media"], c["media"])) for d, c in sorted(devcls.items())),
            "빠른 매체 기준을 느린 디스크에 대면 정상인데도 경고가 나고, 반대로 하면 빠른 디스크의 이상을 놓칩니다. 이 리포트는 오탐을 피하는 쪽을 택했습니다.",
            "디스크별 지연은 '디바이스별 상세' 표에서 각자 매체 기준으로 확인하세요. 한 ES 노드의 data 는 같은 매체로 맞추는 것이 좋습니다.",
            "매체 판정 (rotational, NVMe)")

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
            add(s, "포화", "원인 분리 필요", "I/O 대기 비율은 높지만 응답시간은 빠름. 디스크보다 I/O 양이 많은 상태",
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
    STO = r'(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9]|megaraid|mpt[23]sas|hpsa|smartpqi|aacraid|qla2xxx|lpfc)'
    cats = {
        "I/O 오류": r'I/O error|blk_update_request|Buffer I/O error|critical medium|Medium Error|rejecting I/O',
        "SCSI abort/reset": STO + r'.*\b(abort\w*|reset)\b',
        "hung task (120초 이상 멈춤)": r'hung_task|blocked for more than',
        "파일시스템 오류·읽기전용 전환": r'XFS .*(error|shutdown|corruption)|EXT4-fs error|remount.*read-only',
        "타임아웃": STO + r'.*(timed out|timing out|timeout)',
        "컨트롤러·PCIe 오류": r'controller is down|AER:.*error|(megaraid|mpt[23]sas|hpsa|smartpqi|aacraid).*(FATAL|fault|firmware)|Controller cache pinned',
        "RAID·경로 장애": r'md/raid.*(Disk failure|not operational)|multipath.*(Failing path|remaining active paths: 0)',
        # RAID 컨트롤러가 커널 로그로 보내는 이벤트. 벤더 도구 없이도 배터리·논리 디스크·구성 디스크 이상을 볼 수 있다
        # (megaraid_sas 는 기본 설정에서 CRITICAL 이상 이벤트를 커널 로그에 남긴다)
        "RAID 컨트롤러 이벤트": r'megaraid_sas.*/0x[0-9a-f]+/(FATAL|CRIT|DEAD|WARN)|(megaraid|mpt[23]sas|hpsa|smartpqi|aacraid).*'
                             r'(battery|bbu|cachevault|degraded|offline|lockup|predictive|rebuild)',
        "LVM thin·snapshot 이상": r'thin.*(out of data space|out-of-data-space|read-only mode)|snapshots: Invalidating',
    }
    cnt = {k: sum(1 for l in klog if re.search(v, l, re.I)) for k, v in cats.items()}
    err_sev = "ok"
    if cnt["I/O 오류"] or cnt["파일시스템 오류·읽기전용 전환"]:
        err_sev = "crit"
    elif (cnt["hung task (120초 이상 멈춤)"] or cnt["SCSI abort/reset"] or cnt["컨트롤러·PCIe 오류"] or cnt["RAID·경로 장애"]
          or cnt["RAID 컨트롤러 이벤트"] or cnt["LVM thin·snapshot 이상"]):
        err_sev = "warn"
    elif cnt["타임아웃"]:
        err_sev = "caution"
    if err_sev != "ok":
        ERR_WHY = {
            "vmware": vs("vSAN 경로가 잠시 멈추면(호스트 장애, resync 폭주, 네트워크 단절) ",
                         "데이터스토어 경로가 잠시 멈추면(어레이 컨트롤러 전환, SAN·NFS 경로 끊김, APD·PDL) ",
                         "데이터스토어 경로가 잠시 멈추면(vSAN 호스트 장애·resync 폭주, SAN·NFS 경로 끊김) ")
                      + "Guest에서는 SCSI abort/reset, hung task, 심하면 파일시스템 읽기전용 전환으로 나타납니다. ",
            "local": "디스크 불량 섹터, 컨트롤러 펌웨어 문제, RAID 재구성 중 지연이 SCSI abort/reset, Medium Error, hung task로 나타납니다. "
                     "Medium Error·I/O error는 디스크 교체 신호일 수 있습니다. ",
            "san": "어레이 컨트롤러 전환, SAN 경로 끊김, 스위치 포트 오류가 경로 소실(multipath), SCSI abort/reset, hung task로 나타납니다. ",
        }
        where = "vmware" if kind == "vmware" else ("san" if (kind == "baremetal" and attach == "san") else ("local" if kind == "baremetal" else ""))
        add(err_sev, "오류", OUT, "커널 로그에 디스크 관련 오류 기록",
            " · ".join("{} {}건".format(k, v) for k, v in cnt.items() if v),
            ERR_WHY.get(where, "스토리지 백엔드가 잠시 멈추면 OS에서는 SCSI abort/reset, hung task, 심하면 파일시스템 읽기전용 전환으로 나타납니다. ")
            + "짧은 측정 구간에서 안 보인 과거 사고의 흔적입니다.",
            {"vmware": "리포트 부록의 로그 원문 시각을 VMware 관리자에게 전달해 같은 시각의 {} 이벤트를 확인하세요. ".format(VMB),
             "local": "리포트 부록의 로그 원문 시각을 하드웨어 담당자에게 전달해 같은 시각의 RAID 컨트롤러 이벤트 로그, 디스크 SMART, BMC(iDRAC·iLO·XCC) 로그를 확인하세요. ",
             "san": "리포트 부록의 로그 원문 시각을 스토리지 관리자에게 전달해 같은 시각의 어레이 이벤트와 SAN 스위치 포트 로그를 확인하세요. "
             }.get(where, "리포트 부록의 로그 원문 시각을 {}에게 전달해 같은 시각의 호스트·스토리지 이벤트를 확인하세요. ".format(OUT))
            + "I/O 오류·읽기전용 전환은 ES 데이터 무결성 점검이 필요합니다.",
            "커널 로그 (journalctl -k / dmesg), 최근 7일")
    else:
        add("ok", "오류", "참고", "최근 커널 로그에 디스크 오류 없음", "검사 패턴: I/O error, SCSI abort/reset, hung task, FS error, timeout, 컨트롤러·PCIe 오류, RAID·경로 장애",
            vs("vSAN 순간 정지의 흔적이 없다는 뜻입니다.", "데이터스토어 경로가 멈춘 흔적이 없다는 뜻입니다.", "스토리지 경로가 멈춘 흔적이 없다는 뜻입니다.")
            if kind == "vmware" else "스토리지 경로가 멈췄거나 장치 오류가 난 흔적이 없다는 뜻입니다.",
            "조치 불필요.", "journalctl -k / dmesg")

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
            ("검색 fetch 평균", fmt(per(f_t, f_n), 1, " ms"), "_source 읽기. 디스크 영향이 큼"),
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
            "swapoff -a 후 /etc/fstab의 swap 항목 주석 처리(가장 확실), 또는 bootstrap.memory_lock: true 설정." + (" VMware balloon 여부도 함께 확인하세요." if is_vmware else ""),
            "[Elastic 공식] Disable swapping")
    if swap_on and mlock is not True:
        s = "caution" if (swappiness is not None and swappiness <= 1) else "warn"
        mem_sevs.append(s)
        add(s, "메모리·캐시", "서버 담당자", "swap이 켜져 있고 memory_lock도 꺼져 있음",
            "swap 장치 {}개 · vm.swappiness {} · mlockall {}".format(len(swaps), sysctl.get("vm.swappiness"), "미확인" if mlock is None else mlock),
            "지금 swap이 쓰이지 않아도 메모리 압박{} 순간에 ES가 swap으로 밀릴 수 있는 상태입니다.".format("(balloon 포함)" if kind in ("vmware", "vm") else ""),
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
            "검색 지연이 문제라면 이 비율과 ES major fault 추이를 함께 보세요.", "[Elastic 공식] Tune for search speed > Give memory to the filesystem cache")
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
    # readahead 가 큰 원인은 대개 tuned profile 이나 udev 규칙이다. 둘 다 이미 수집하고 있으니 원인 후보로 붙인다.
    tuned_prof = rd(S, "tuned").strip().split(":")[-1].strip()
    udev_ra = "read_ahead" in rd(S, "udev_rules")
    tuned_hint = ""
    if tuned_prof and tuned_prof.lower() not in ("", "none", "no current active profile"):
        tuned_hint += " 활성 tuned profile은 '{}' 입니다. tuned의 disk 플러그인이 readahead를 바꿀 수 있으니 값이 되돌아오면 profile을 함께 확인하세요.".format(tuned_prof)
    if udev_ra:
        tuned_hint += " /etc/udev/rules.d 에 read_ahead를 설정하는 규칙이 이미 있습니다."

    # readahead: Elastic 공식 권고 128KiB
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
            "blockdev --setra 256 /dev/<장치> (512B 섹터 단위 → 128KiB) 로 즉시 적용 가능, udev 규칙으로 영구화. LVM이면 dm 장치에도 적용하세요."
            + tuned_hint,
            "[Elastic 공식] Tune for search speed. LVM, software RAID, dm-crypt에서 readahead가 수 MiB로 커질 수 있으며 128KiB 권장 (blockdev --setra 256)")
    # scheduler
    # Red Hat 권고: 가상 게스트 mq-deadline/none, 고성능 SSD·NVMe none/kyber, 기존 HDD mq-deadline/bfq
    def sched_rec(d):
        if kind != "baremetal":
            return ("mq-deadline", "none"), "가상 디스크"
        c = devcls.get(d, {})
        if c.get("attach") == "san":
            return ("mq-deadline", "none"), "SAN LUN"
        if c.get("media") == "hdd":
            return ("mq-deadline", "bfq"), "HDD"
        return ("none", "kyber", "mq-deadline"), "NVMe" if c.get("media") == "nvme" else "SSD"
    sch_bad, sch_kind = [], set()
    for d in phys:
        sch = topo.attr.get(d, {}).get("queue/scheduler", "")
        m = re.search(r'\[(\S+)\]', sch)
        cur = m.group(1) if m else sch.strip()
        ok_set, what = sched_rec(d)
        # 판정은 확실히 불리한 경우만: 가상 디스크·SAN·SSD·NVMe 에 cfq/bfq. HDD 의 bfq 는 Red Hat 권고 범위
        if cur in ("cfq", "bfq") and what != "HDD":
            sch_bad.append("{}={}".format(d, cur)); sch_kind.add(what)
    if sch_bad:
        cfg_sevs.append("caution")
        if kind == "baremetal":
            add("caution", "설정", "서버 담당자", "I/O 스케줄러가 장치 종류에 맞지 않음", ", ".join(sch_bad) + " · 장치 종류 " + ", ".join(sorted(sch_kind)),
                "cfq/bfq는 프로세스 간 공평 분배에 CPU와 시간을 씁니다. 빠른 SSD·NVMe에서는 그 비용이 장치 지연보다 커질 수 있습니다. "
                "Red Hat은 고성능 SSD·NVMe에 none 또는 kyber, 기존 HDD에 mq-deadline 또는 bfq를 권고합니다.",
                "SSD·NVMe는 none (echo none > /sys/block/<장치>/queue/scheduler), HDD는 mq-deadline 으로 변경하고 udev 규칙으로 영구화하세요.",
                "[Red Hat 공식] Setting the disk scheduler > Disk schedulers for different use cases")
        else:
            add("caution", "설정", "서버 담당자", "I/O 스케줄러가 가상 디스크에 불리함", ", ".join(sch_bad),
                "cfq/bfq는 프로세스 간 공평 분배에 시간을 씁니다. 스케줄링은 {}가 이미 하므로 VM에서는 가볍게 두는 편이 낫습니다. none과 mq-deadline의 차이는 작습니다.".format(
                    vs("vSAN", "하이퍼바이저·스토리지 어레이", "하이퍼바이저·스토리지") if is_vmware else "하이퍼바이저·스토리지 백엔드"),
                "mq-deadline 또는 none으로 변경 (echo mq-deadline > /sys/block/<장치>/queue/scheduler, udev 규칙으로 영구화).",
                "Red Hat 'Monitoring and managing system status and performance' > Setting the disk scheduler")
    # max_map_count
    mmc = num(sysctl.get("vm.max_map_count"))
    if mmc is not None and mmc < 262144:
        cfg_sevs.append("crit")
        add("crit", "설정", "서버 담당자", "vm.max_map_count가 ES 최소 요구치 미만", "현재 {}".format(int(mmc)),
            "ES는 segment를 mmap으로 엽니다. 매핑 개수 한도가 부족하면 운영 모드에서 기동이 거부되거나 mmap 실패가 납니다.",
            "sysctl -w vm.max_map_count=1048576 + /etc/sysctl.d/ 에 영구 설정.", "[Elastic 공식] Bootstrap checks > Maximum map count (최소 262144, 권장 1048576)")
    elif mmc is not None and mmc < 1048576:
        cfg_sevs.append("info")
        add("info", "설정", "서버 담당자", "vm.max_map_count 최소치는 충족, 권장치 미만", "현재 {} (권장 1048576)".format(int(mmc)),
            "샤드·segment가 많은 노드일수록 매핑 수가 늘어납니다.", "여유 있게 1048576으로 올려 두는 것을 권장합니다.",
            "Elastic 문서 'Bootstrap checks > Maximum map count'")
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
                "xfs 또는 ext4 로컬 볼륨으로 이전하세요.", "[Elastic 공식] Hardware. 로컬 스토리지 권장, 원격 파일시스템 회피")
        if "strictatime" in opts:
            cfg_sevs.append("caution")
            add("caution", "설정", "서버 담당자", "strictatime 마운트. 읽을 때마다 메타데이터 쓰기", m["opts"],
                "파일을 읽을 때마다 접근 시각을 기록합니다. 기본값 relatime은 이 비용이 거의 없습니다.", "noatime 또는 기본 relatime으로 변경.", "mount(8)")
        if "discard" in opts:
            cfg_sevs.append("caution")
            add("caution", "설정", "서버 담당자", "online discard 마운트 옵션 사용", m["opts"],
                "삭제 때마다 TRIM을 보내 쓰기 지연을 늘릴 수 있습니다." + ((" " + vs(
                    "vSAN에서 Guest TRIM은 클러스터 설정이 켜져 있어야 공간 회수로 이어집니다.",
                    "thin 데이터스토어에서 Guest UNMAP이 공간 회수로 이어지는지는 VMware 설정에 달려 있습니다.",
                    "Guest TRIM/UNMAP이 공간 회수로 이어지는지는 데이터스토어 설정(vSAN TRIM/UNMAP, thin VMDK)에 달려 있습니다.")) if is_vmware else ""),
                "discard를 빼고 fstrim.timer(주 1회)로 대체하세요." + (" Guest TRIM/UNMAP 사용 여부는 VMware 관리자에게 확인." if is_vmware else ""),
                "mount(8)" + (vs(", vSAN Guest TRIM/UNMAP 문서", ", VMware Space Reclamation 문서", ", vSAN TRIM/UNMAP·VMware Space Reclamation 문서") if is_vmware else ""))
    # 파티션 정렬
    for d in phys:
        for part, (parent, start) in topo.parts.items():
            if parent == d and start % 2048 != 0:
                cfg_sevs.append("caution")
                add("caution", "설정", "서버 담당자", "파티션 시작 위치가 1MiB 정렬이 아님", "{} start sector {}".format(part, start),
                    "정렬이 어긋나면 I/O 한 번이 하부 블록 두 개에 걸쳐 불필요한 추가 I/O가 생깁니다.",
                    "신규 구성 시 1MiB(2048 섹터) 정렬로 파티션을 만드세요. 운영 중 변경은 데이터 이전이 필요합니다.", "parted/fdisk 정렬 가이드")
    # SCSI timeout: vSAN failover 대비
    # 180초 권고는 VMware(open-vm-tools) 기준. bare-metal 로컬 디스크는 커널 기본 30초가 정상이고,
    # SAN 은 multipath 벤더 권고를 따르므로 여기서 판정하지 않는다
    to_bad = ["{}={}s".format(d, topo.attr[d]["device/timeout"]) for d in phys
              if is_vmware and num(topo.attr.get(d, {}).get("device/timeout"), 999) < 60]
    if to_bad:
        cfg_sevs.append("warn")
        add("warn", "설정", "서버 담당자", vs("SCSI 명령 타임아웃이 짧음. vSAN이 잠깐 멈추면 I/O 오류 위험",
                                               "SCSI 명령 타임아웃이 짧음. 스토리지가 잠깐 멈추면 I/O 오류 위험",
                                               "SCSI 명령 타임아웃이 짧음. 스토리지가 잠깐 멈추면 I/O 오류 위험"), ", ".join(to_bad),
            vs("vSAN 호스트 장애", "어레이 컨트롤러 전환", "vSAN 호스트 장애, 어레이 컨트롤러 전환") + "나 경로 전환 중에는 I/O가 수십 초 멈출 수 있습니다. 타임아웃이 짧으면 Guest가 이를 오류로 처리해 파일시스템이 읽기전용으로 바뀔 수 있습니다.",
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
        if len(p) >= 6 and any(pm["mount"] and p[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
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
        if is_vmware:
            add("info", "설정", "VMware 관리자", "OS와 ES data가 같은 가상 디스크를 사용", ", ".join(share_dev),
                "로그 쓰기, 패키지 작업 등 OS I/O가 ES I/O와 같은 큐를 나눠 씁니다.", "ES data 전용 VMDK 분리를 권장합니다.",
                vs("VMware DB-on-vSAN 구성 권고", "VMware Performance Best Practices for vSphere", "VMware Performance Best Practices for vSphere"))
        else:
            add("info", "설정", "서버 담당자" if kind == "baremetal" else OUT,
                "OS와 ES data가 같은 {}를 사용".format("디스크" if kind == "baremetal" else "가상 디스크"), ", ".join(share_dev),
                "로그 쓰기, 패키지 작업 등 OS I/O가 ES I/O와 같은 장치를 나눠 씁니다.",
                "ES data 전용 {}를 분리하는 것을 권장합니다.".format("디스크(또는 RAID 논리 디스크)" if kind == "baremetal" else "가상 디스크"),
                "[실무 기준] OS I/O 와 ES I/O 분리")
    elif is_vmware:
        hosts = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
        rhosts = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
        if hosts and set(hosts) <= set(rhosts):
            add("info", "설정", "VMware 관리자", "OS 디스크와 data 디스크가 같은 가상 SCSI 컨트롤러에 연결", "controller {}".format(", ".join(hosts)),
                "컨트롤러 하나의 큐를 함께 씁니다. 부하가 높을 때 컨트롤러를 나누면 큐가 분산됩니다.",
                "ES data VMDK를 별도 PVSCSI 컨트롤러(SCSI 1:x 등)에 연결하는 것을 권장합니다.", "VMware 'Performance Best Practices for vSphere' > PVSCSI")
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
    # iostats 비활성. 이러면 측정 자체가 무의미
    for d in phys:
        if topo.attr.get(d, {}).get("queue/iostats") == "0":
            cfg_sevs.append("warn")
            add("warn", "설정", "서버 담당자", "블록 장치 I/O 통계 수집이 꺼져 있음 ({})".format(d),
                "/sys/block/{}/queue/iostats = 0".format(d),
                "커널이 이 장치의 I/O 통계를 쌓지 않습니다. iostat·sar를 포함해 어떤 도구도 이 디스크의 응답시간을 볼 수 없습니다. 이 리포트의 해당 장치 수치도 신뢰할 수 없습니다.",
                "iostats를 1로 되돌린 뒤 재측정하세요 (성능 영향은 무시할 수준).", "Linux block layer sysfs")
    # writeback throttling: 커널이 쓰기를 의도적으로 억제
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
                if len(p_) > 2 and re.match(r'^\d+:$', p_[0]) and re.search(r'(pvscsi|virtio\d+-req|nvme|ahci|mpt|megasas|megaraid|hpsa|smartpqi|aacraid|qla2xxx|lpfc)', line, re.I):
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
                cpu_w = "CPU" if kind == "baremetal" else "vCPU"
                add("caution", "설정", "서버 담당자", "스토리지 인터럽트가 {} 한 개에 몰림".format(cpu_w),
                    "상위 {}가 전체 스토리지 인터럽트의 {:.0f}% 처리 ({} {}개 중)".format(cpu_w, share, cpu_w, len(tot)),
                    "I/O 완료 처리가 한 코어에 집중되면 그 코어가 한계일 때 전체 IOPS가 거기서 막힙니다. 디스크는 여유가 있는데 더 안 나오는 상황이 됩니다.",
                    "irqbalance 동작 여부를 확인하세요. " + ("PVSCSI·vmxnet3는 다중 큐를 지원하므로 큐 수와 인터럽트 분산 설정을 VMware 관리자와 함께 점검합니다." if is_vmware else
                    "NVMe와 최신 RAID·HBA 드라이버는 다중 큐(MSI-X)를 지원하므로 큐 수가 CPU 수만큼 잡혔는지 /proc/interrupts 에서 확인합니다." if kind == "baremetal" else
                    "가상 컨트롤러의 다중 큐 설정을 {}와 함께 점검합니다.".format(OUT)),
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
                "Elastic 문서 'Bootstrap checks > Maximum map count'")
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

    # ── 디스크와 직결되는 인덱스 설정 (명시적으로 바꾼 인덱스만 응답에 들어온다) ──
    idx_set = rjson(S, "es_idx_settings.json") or {}
    def idx_vals(key):
        """key 를 명시적으로 설정한 인덱스를 {인덱스: 값} 으로"""
        out = {}
        for name, blk in idx_set.items():
            v = dig(blk, "settings", key)
            if v is not None:
                out[name] = str(v)
        return out

    # translog durability: 디스크 지연이 인덱싱 지연으로 이어지는 경로를 설명하는 핵심 설정
    dur_async = {k: v for k, v in idx_vals("index.translog.durability").items() if str(v).lower() == "async"}
    if dur_async:
        add("info", "ES 설정", "참고", "일부 인덱스가 translog를 비동기로 fsync 중 (durability: async)",
            "{}개 인덱스: {}".format(len(dur_async), ", ".join(sorted(dur_async)[:6])),
            "기본값 request는 bulk 요청마다 fsync를 합니다. async는 sync_interval(기본 5초)마다 묶어서 하므로 "
            "디스크 쓰기 지연의 영향을 훨씬 덜 받습니다. 대신 장애 시 마지막 commit 이후 확인된 쓰기가 사라질 수 있습니다.",
            "의도한 설정이면 그대로 두세요. 디스크 쓰기 지연 때문에 임시로 바꾼 것이라면 데이터 유실 범위를 확인하고 "
            "스토리지를 개선한 뒤 request로 되돌리는 쪽을 검토하세요.",
            "[Elastic 공식] Translog settings (durability: request가 기본, async는 sync_interval 단위)")
    else:
        add("info", "ES 설정", "참고", "translog durability가 기본값(request). 쓰기 요청마다 fsync",
            "durability를 async로 바꾼 인덱스 없음" if idx_set else "인덱스 설정 미수집",
            "기본 설정에서는 bulk 요청 하나가 끝나려면 translog fsync가 끝나야 합니다. 그래서 디스크 쓰기 지연이 "
            "그대로 인덱싱 응답 시간이 됩니다. 이 리포트가 쓰기 지연을 중요하게 보는 이유입니다.",
            "조치 불필요. 쓰기 지연이 문제인데 스토리지를 바로 개선할 수 없는 상황이라면 async가 선택지이지만 "
            "데이터 유실 범위를 먼저 합의해야 합니다.",
            "[Elastic 공식] Translog settings")

    # merge scheduler: Elastic은 spinning platter 에 max_thread_count=1 을 권고
    # VMware 가상 디스크는 백엔드가 all-flash 여도 rotational=1 로 보고하는 경우가 많아 vSAN 에서는
    # rotational 값만으로 판정하지 않고 -s hybrid 선언을 기준으로 삼는다.
    # bare-metal 로컬 디스크는 rotational 을 믿을 수 있으므로 매체 판정(hdd)을 그대로 쓴다.
    # RAID 논리 디스크는 컨트롤러가 rotational 을 제대로 넘기지 않는 경우가 있어 추정으로 표시한다.
    mtc = idx_vals("index.merge.scheduler.max_thread_count")
    rot = [d for d in phys if topo.attr.get(d, {}).get("queue/rotational") == "1"]
    spinning = storage in ("hybrid", "hdd")
    if spinning:
        not_one = [k for k, v in mtc.items() if str(v) != "1"]
        if not mtc or not_one:
            cfg_sevs.append("caution")
            if storage == "hybrid":
                ttl = "Hybrid vSAN인데 merge 스레드 수가 Elastic 권고(1)가 아님"
                tail = "All-Flash인데 -s hybrid로 실행했다면 -s allflash로 다시 측정하세요."
            else:
                ttl = "HDD인데 merge 스레드 수가 Elastic 권고(1)가 아님"
                tail = ("HDD 판정은 {}. 실제로는 SSD라면 -s ssd 로 다시 분석하세요.".format(
                    "RAID 논리 디스크의 rotational 값으로 추정한 것입니다" if unsure else "커널이 보고한 rotational=1 기준입니다"))
            add("caution", "ES 설정", "ES 설정", ttl,
                "max_thread_count를 1로 설정한 인덱스 {}개{} · {}가 rotational로 보고한 장치: {}".format(
                    len([k for k, v in mtc.items() if str(v) == "1"]),
                    " (1이 아닌 인덱스: " + ", ".join(sorted(not_one)[:4]) + ")" if not_one else "",
                    "Guest" if kind in ("vmware", "vm") else "커널", ", ".join(rot) or "없음"),
                "기본값은 프로세서 수의 절반입니다. SSD에는 맞지만 회전 디스크에서는 동시 merge가 헤드를 흩어 놓아 "
                "오히려 느려집니다. Elastic은 이 경우 1로 낮추라고 명시합니다.",
                "index.merge.scheduler.max_thread_count를 1로 낮추는 것을 검토하세요. "
                "인덱스 단위 동적 설정이라 재시작은 필요 없습니다. " + tail,
                "[Elastic 공식] Merge settings (기본값은 프로세서 수의 절반, 회전 디스크면 1로 낮출 것)")
    elif mtc:
        add("info", "ES 설정", "참고", "merge 스레드 수를 기본값과 다르게 설정한 인덱스 있음",
            ", ".join("{}={}".format(k, v) for k, v in sorted(mtc.items())[:6]),
            "기본값은 프로세서 수의 절반입니다. SSD·NVMe·All-Flash에서는 기본값이 적절합니다.",
            "의도한 설정인지 확인하세요.", "[Elastic 공식] Merge settings")

    # store type / preload
    st_type = idx_vals("index.store.type")
    if st_type:
        add("info", "ES 설정", "참고", "index.store.type을 명시적으로 지정한 인덱스 있음",
            ", ".join("{}={}".format(k, v) for k, v in sorted(st_type.items())[:6]),
            "기본값 hybridfs는 파일 종류에 따라 mmap과 nio를 골라 씁니다. niofs로 바꾸면 mmap을 쓰지 않아 "
            "max_map_count 부담은 줄지만 읽기 성능이 떨어질 수 있습니다.",
            "의도한 설정인지 확인하세요. 특별한 이유가 없으면 기본값이 낫습니다.",
            "[Elastic 공식] Store (기본 hybridfs)")

    # ── 장치 계층 전체(파티션·LVM·md·dm)를 따라 내려가며 쓰는 도우미 ────────────
    def chain(k, depth=0):
        if not k or depth > 8:
            return []
        out = [k]
        if k in topo.parts:
            out += chain(topo.parts[k][0], depth + 1)
        for s_ in topo.slaves.get(k, []):
            out += chain(s_, depth + 1)
        return out
    data_chain = sorted(set(x for pm in path_map for x in chain(pm["kname"])))
    def phys_of_path(pth):
        m_ = mount_for(pth, mounts)
        k_ = topo.kname(m_["src"]) if m_ else None
        return set(topo.physical(k_)) if k_ else set()

    # flush: fsync 가 장치 캐시를 비우라고 보내는 요청. 이게 느리면 translog fsync 가 곧바로 느려진다
    if A.get("flush_ms") is not None and (A.get("flush_ps") or 0) >= 1:
        fms = A["flush_ms"]
        if fms >= th["caution"]:
            s_ = "warn" if fms >= th["warn"] else "caution"
            cfg_sevs.append(s_)
            add(s_, "지연", "원인 분리 필요" if kind != "baremetal" else OUT, "flush(장치 캐시 비우기)가 느림. fsync 가 그만큼 늦어짐",
                "flush 초당 {} · 평균 {}".format(fmt(A["flush_ps"], 1), fmt(fms, 2, "ms")),
                "ES 는 bulk 요청마다 translog 를 fsync 하고(기본 durability: request), 파일시스템은 fsync 때 장치에 flush 를 보냅니다. "
                "flush 가 느리면 인덱싱 응답이 그대로 느려집니다. 전원 차단 보호(PLP)가 없는 SSD, 배터리 없는 RAID 캐시, "
                "쓰기 캐시를 끝까지 비워야 하는 원격 스토리지에서 흔합니다.",
                ("{}에게 장치의 쓰기 캐시 보호 방식(전원 차단 보호가 있는 엔터프라이즈 SSD인지, RAID 캐시가 배터리로 보호되는지)을 확인 요청하세요. "
                 "쓰기 지연 문제를 translog durability: async 로 덮는 것은 데이터 유실 범위를 먼저 합의한 경우에만 검토합니다.").format(OUT),
                "Linux Documentation/admin-guide/iostats.rst (flush 요청 수·시간 필드), [Elastic 공식] Translog settings")

    # ES 가 아닌 프로세스의 디스크 사용 (옆집 부하)
    def procio(txt):
        io, comm = {}, {}
        for l in txt.splitlines():
            m_ = re.match(r'^/proc/(\d+)/io:(read_bytes|write_bytes):\s*(\d+)', l)
            if m_:
                io.setdefault(m_.group(1), {})[m_.group(2)] = int(m_.group(3)); continue
            m_ = re.match(r'^/proc/(\d+)/comm:(.*)$', l)
            if m_:
                comm[m_.group(1)] = m_.group(2).strip()
        return io, comm
    pio0, _c0 = procio(rd(S, "procio_start"))
    pio1, pcomm = procio(rd(S, "procio_end"))
    PROC = []
    for pid_, v in pio1.items():
        a_ = pio0.get(pid_)
        if not a_:
            continue
        r_ = max(0, v.get("read_bytes", 0) - a_.get("read_bytes", 0))
        w_ = max(0, v.get("write_bytes", 0) - a_.get("write_bytes", 0))
        if r_ + w_ > 0:
            PROC.append({"pid": pid_, "comm": pcomm.get(pid_, "?"), "r_mb": r_ / 1048576.0, "w_mb": w_ / 1048576.0,
                         "es": pid_ == es_pid})
    PROC.sort(key=lambda x: -(x["r_mb"] + x["w_mb"]))
    tot_mb = sum(x["r_mb"] + x["w_mb"] for x in PROC)
    es_mb = sum(x["r_mb"] + x["w_mb"] for x in PROC if x["es"])
    others = [x for x in PROC if not x["es"]]
    if PROC and tot_mb >= 50 and es_pid and (tot_mb - es_mb) / tot_mb >= 0.3 and not low_load:
        cfg_sevs.append("caution")
        add("caution", "포화", "서버 담당자", "ES 가 아닌 프로세스가 디스크를 많이 씀",
            "측정 구간 전체 프로세스 디스크 I/O {} 중 ES {:.0f}% · 상위: {}".format(
                fmt(tot_mb, 0, "MB"), 100.0 * es_mb / tot_mb,
                ", ".join("{}(pid {}) {}".format(x["comm"], x["pid"], fmt(x["r_mb"] + x["w_mb"], 0, "MB")) for x in others[:4])),
            "같은 서버의 다른 프로세스(백업, 로그 수집기, 다른 DB, 보안 에이전트 등)가 디스크를 나눠 쓰면 ES 가 느려져도 원인은 ES 밖에 있습니다. "
            "이 값은 프로세스 단위라 어느 디스크를 썼는지까지는 구분하지 않습니다.",
            "상위 프로세스가 ES data 디스크를 쓰는지 확인하고, 쓴다면 다른 디스크로 옮기거나 실행 시간을 ES 피크 밖으로 조정하세요. "
            "Elastic 은 ES 가 서버 자원을 단독으로 쓰는 구성을 권장합니다.",
            "/proc/<pid>/io (read_bytes, write_bytes), [Elastic 공식] Important system configuration")

    # inode
    for line in rd(S, "df_i").splitlines()[1:]:
        p_ = line.split()
        if len(p_) >= 6 and any(pm["mount"] and p_[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
            iu = num(p_[4].rstrip("%"))
            if iu is not None and iu >= 80:
                s_ = "warn" if iu >= 90 else "caution"
                cfg_sevs.append(s_)
                add(s_, "설정", "서버 담당자", "ES data 파일시스템의 inode 사용률이 높음", "{} inode {}%".format(p_[5], int(iu)),
                    "inode 가 다 차면 디스크 공간이 남아 있어도 새 파일(segment, translog)을 만들지 못해 인덱싱이 실패합니다.",
                    "샤드·segment 수가 과한지 점검하고(작은 샤드가 많은 경우), 필요하면 inode 가 넉넉한 파일시스템으로 옮기세요. "
                    "XFS 는 inode 를 동적으로 할당하므로 이 문제가 드뭅니다.", "df -i")

    # 마운트 옵션: 데이터 안전·성능에 직접 닿는 것
    for pm in path_map:
        m_ = pm["mount"]
        if not m_:
            continue
        o = m_["opts"].split(",")
        if "nobarrier" in o or "barrier=0" in o:
            cfg_sevs.append("warn")
            add("warn", "설정", "서버 담당자", "쓰기 순서 보장(barrier)을 끈 마운트", "{} {}".format(m_["mnt"], m_["opts"]),
                "barrier 를 끄면 fsync 가 장치 캐시를 비우지 않아 빨라지지만, 전원이 끊기면 ES 가 기록했다고 확인한 데이터가 사라지거나 "
                "파일시스템이 깨질 수 있습니다. 배터리로 보호되는 캐시가 아니면 위험합니다.",
                "nobarrier/barrier=0 을 빼고 다시 마운트하세요. 최신 커널의 XFS 는 이 옵션을 지원하지 않습니다.", "mount(8), ext4(5)")
        if "sync" in o or "dirsync" in o:
            cfg_sevs.append("warn")
            add("warn", "설정", "서버 담당자", "동기 쓰기(sync) 마운트", "{} {}".format(m_["mnt"], m_["opts"]),
                "모든 쓰기가 디스크 기록을 기다린 뒤에 끝납니다. ES 는 필요한 곳에서 스스로 fsync 하므로 마운트를 sync 로 둘 이유가 없고, 쓰기 성능이 크게 떨어집니다.",
                "sync·dirsync 옵션을 빼고 다시 마운트하세요.", "mount(8)")
        if "data=journal" in o:
            cfg_sevs.append("info")
            add("info", "설정", "서버 담당자", "ext4 data=journal 마운트. 데이터를 두 번 씀", "{} {}".format(m_["mnt"], m_["opts"]),
                "파일 내용까지 저널에 먼저 쓴 뒤 제자리에 다시 씁니다. 쓰기량이 두 배가 됩니다.", "기본값(data=ordered)을 검토하세요.", "ext4(5)")

    # LVM thin pool / snapshot / crypt / cache
    tbl_lines = rd(S, "dmsetup_table").splitlines()
    chain_names = set(topo.attr.get(k, {}).get("dm/name", "") for k in data_chain if k.startswith("dm-"))
    dm_targets = {}
    for l in tbl_lines:
        if ":" in l:
            nm, rest = l.split(":", 1)
            f_ = rest.split()
            if len(f_) >= 3:
                dm_targets.setdefault(nm.strip(), set()).add(f_[2])
    data_targets = set()
    for nm in chain_names:
        data_targets |= dm_targets.get(nm, set())
    DM = {"targets": sorted(data_targets), "pools": []}
    if "thin" in data_targets:
        for l in rd(S, "dmsetup_status").splitlines():
            m_ = re.match(r'^(\S+):\s+\d+\s+\d+\s+thin-pool\s+\d+\s+(\d+)/(\d+)\s+(\d+)/(\d+)\s+.*?\b(rw|ro|out_of_data_space|needs_check|Fail)\b', l)
            if m_:
                meta_pct = 100.0 * int(m_.group(2)) / max(1, int(m_.group(3)))
                data_pct = 100.0 * int(m_.group(4)) / max(1, int(m_.group(5)))
                DM["pools"].append((m_.group(1), data_pct, meta_pct, m_.group(6)))
        for nm, dp, mp, mode in DM["pools"]:
            s_ = ("crit" if (mode in ("ro", "out_of_data_space", "Fail") or dp >= 95) else "warn" if (dp >= 85 or mp >= 80)
                  else "caution" if dp >= 70 else "info")
            cfg_sevs.append(s_)
            add(s_, "설정", "서버 담당자", "ES data 가 LVM thin pool 위에 있음 (데이터 {:.0f}% 사용)".format(dp),
                "pool {} · 데이터 {:.0f}% · 메타데이터 {:.0f}% · 상태 {}".format(nm, dp, mp, mode),
                "thin pool 은 공간을 쓰는 만큼 나중에 할당합니다. pool 이 가득 차면 ES data 파일시스템의 여유 공간과 상관없이 쓰기가 멈추거나 "
                "I/O 오류가 납니다. ES 의 disk watermark 는 파일시스템 여유만 보므로 이 상황을 미리 막지 못합니다. 처음 쓰는 블록마다 할당 비용도 듭니다.",
                "pool 사용률을 상시 감시하고 여유를 두세요(lvextend 로 pool 확장). 가능하면 ES data 는 thick(일반) LV 로 두는 편이 안전합니다.",
                "lvmthin(7), dmsetup status")
    if "snapshot-origin" in data_targets or any("snapshot" == t for t in data_targets):
        cfg_sevs.append("warn")
        add("warn", "설정", "서버 담당자", "ES data 볼륨에 LVM snapshot 이 걸려 있음",
            "device-mapper 대상: {}".format(", ".join(sorted(data_targets))),
            "기존 LVM snapshot 은 원본 블록을 처음 바꿀 때마다 옛 데이터를 snapshot 영역에 복사합니다(copy-on-write). 쓰기가 여러 배로 늘어나고, "
            "snapshot 공간이 가득 차면 snapshot 이 무효가 됩니다.",
            "백업용으로 잠깐 만든 것이라면 백업 후 바로 지우세요(lvremove). ES 백업은 ES snapshot API 를 쓰는 편이 맞습니다.",
            "lvmsnapshot, [Elastic 공식] Snapshot and restore")
    if "crypt" in data_targets:
        cfg_sevs.append("info")
        add("info", "설정", "참고", "ES data 가 dm-crypt(LUKS) 암호화 볼륨 위에 있음", "device-mapper 대상: crypt",
            "모든 읽기·쓰기에 암호화 CPU 비용이 붙습니다. AES-NI 가 있는 CPU 에서는 대개 작지만, 고성능 NVMe 에서는 병목이 될 수 있습니다. "
            "dm-crypt 장치의 readahead 가 크게 잡히는 경우도 있습니다.",
            "보안 요구로 쓰는 것이면 그대로 두고, 디스크 지연이 문제일 때 CPU 사용률과 함께 보세요.", "cryptsetup(8)")
    if data_targets & {"cache", "writecache"}:
        cfg_sevs.append("info")
        add("info", "설정", "참고", "ES data 가 dm-cache·writecache 계층 위에 있음", "device-mapper 대상: " + ", ".join(sorted(data_targets & {"cache", "writecache"})),
            "빠른 장치를 캐시로 앞에 둔 구성입니다. 캐시에 맞는 동안은 빠르지만, 캐시를 넘치면 뒤쪽 느린 장치 속도가 드러납니다. 측정 시점에 따라 결과가 크게 달라질 수 있습니다.",
            "캐시 적중률을 lvs -o+cache_read_hits,cache_read_misses 로 확인하세요.", "lvmcache(7)")

    # 같은 디스크 공유: swap, snapshot 저장소(path.repo), ES 로그(path.logs)
    phys_set = set(phys)
    swap_hit = []
    for line in rd(S, "swaps").splitlines()[1:]:
        f_ = line.split()
        if not f_:
            continue
        if f_[0].startswith("/dev/"):
            sp = set(topo.physical(topo.kname(f_[0])))
        else:
            sp = phys_of_path(f_[0])
        if sp & phys_set:
            swap_hit.append(f_[0])
    if swap_hit and not dev_guess:
        cfg_sevs.append("caution")
        add("caution", "설정", "서버 담당자", "swap 이 ES data 디스크에 있음", ", ".join(swap_hit),
            "메모리가 부족해 swap 이 쓰이는 순간 ES data 디스크에 무작위 I/O 가 더해져, 메모리 문제와 디스크 문제가 한꺼번에 옵니다.",
            "Elastic 권고대로 swap 을 끄는 것이 가장 좋고, 남겨야 한다면 ES data 와 다른 디스크에 두세요.", "[Elastic 공식] Disable swapping")
    npath = dig(node_i, "settings", "path") or {}
    repos = npath.get("repo") or []
    if isinstance(repos, str):
        repos = [repos]
    logs_p = npath.get("logs")
    for l in rd(S, "es_yml").splitlines():
        m_ = re.match(r'^\s*path\.repo\s*:\s*(.+)$', l)
        if m_:
            repos += [x.strip().strip("'\"") for x in m_.group(1).strip("[] ").split(",") if x.strip()]
        m_ = re.match(r'^\s*path\.logs\s*:\s*(.+)$', l)
        if m_ and not logs_p:
            logs_p = m_.group(1).strip().strip("'\"")
    same_repo = [r_ for r_ in sorted(set(repos)) if phys_of_path(r_) & phys_set]
    if same_repo and not dev_guess:
        cfg_sevs.append("caution")
        add("caution", "설정", "ES 설정", "snapshot 저장소(path.repo)가 ES data 와 같은 디스크", ", ".join(same_repo),
            "snapshot 을 뜨는 동안 같은 디스크에서 읽고 쓰기가 겹칩니다. 더 큰 문제는 디스크가 고장 나면 원본과 백업을 함께 잃는다는 점입니다.",
            "snapshot 저장소는 다른 장비(NFS 서버, 오브젝트 스토리지 등)에 두세요.", "[Elastic 공식] Snapshot and restore > Shared file system repository")
    if logs_p and (phys_of_path(logs_p) & phys_set) and not dev_guess:
        cfg_sevs.append("info")
        add("info", "설정", "참고", "ES 로그(path.logs)가 ES data 와 같은 디스크", logs_p,
            "로그 쓰기는 양이 작지만 slowlog·GC 로그가 많거나 로그 회전 압축이 겹치면 data I/O 와 경합합니다.",
            "로그가 많은 환경이면 OS 디스크 등 다른 디스크로 옮기는 것을 검토하세요.", "Elasticsearch path settings")

    # 장치 상태와 오류 카운터 (SCSI), NVMe 컨트롤러 상태, PCIe AER
    DEVERR = {}
    for d in phys:
        a_ = topo.attr.get(d, {})
        st = (a_.get("device/state") or "").strip()
        ioerr = int(a_.get("device/ioerr_cnt", "0x0"), 16) if re.match(r'^0x[0-9a-fA-F]+$', a_.get("device/ioerr_cnt", "") or "") else None
        iotmo = int(a_.get("device/iotmo_cnt", "0x0"), 16) if re.match(r'^0x[0-9a-fA-F]+$', a_.get("device/iotmo_cnt", "") or "") else None
        DEVERR[d] = {"state": st, "ioerr": ioerr, "iotmo": iotmo, "aer": topo.aer.get(d)}
        if st and st not in ("running",):
            cfg_sevs.append("crit")
            add("crit", "오류", OUT, "ES data 장치 상태가 running 이 아님 ({} = {})".format(d, st), "/sys/block/{}/device/state = {}".format(d, st),
                "offline·blocked 상태의 장치는 I/O 를 받지 못합니다. 커널이 오류가 반복된 장치를 끊어 낸 경우입니다.",
                "커널 로그의 해당 장치 오류와 하드웨어 상태를 즉시 확인하세요.", "SCSI sysfs device/state")
        if iotmo:
            err_sev = sev_max(err_sev, "caution")
            add("caution", "오류", OUT, "장치 명령 타임아웃 기록 ({} {}회)".format(d, iotmo), "iotmo_cnt={} · ioerr_cnt={} (부팅 후 누적)".format(iotmo, ioerr),
                "장치가 정해진 시간 안에 응답하지 못한 적이 있습니다. 그 순간 I/O 가 수십 초 멈췄을 수 있습니다.",
                "커널 로그의 같은 장치 timeout·reset 기록과 시각을 맞춰 보세요.", "SCSI sysfs device/iotmo_cnt")
        elif ioerr and cnt.get("I/O 오류"):
            err_sev = sev_max(err_sev, "caution")
            add("caution", "오류", OUT, "장치 오류 카운터가 0 이 아님 ({} {}회)".format(d, ioerr), "ioerr_cnt={} (부팅 후 누적) · 커널 로그 I/O 오류 {}건".format(ioerr, cnt.get("I/O 오류")),
                "장치가 오류로 끝낸 명령이 있습니다. 커널 로그의 I/O 오류와 함께 나타나면 장치나 경로 문제일 가능성이 큽니다.",
                "해당 장치의 상태(SMART, RAID 컨트롤러, SAN 경로)를 확인하세요.", "SCSI sysfs device/ioerr_cnt")
        ae = topo.aer.get(d)
        if ae and ((ae.get("fatal") or 0) + (ae.get("nonfatal") or 0)) > 0:
            err_sev = sev_max(err_sev, "warn")
            add("warn", "오류", OUT, "스토리지 PCIe 장치에 AER 오류 기록 ({})".format(d),
                "{} · fatal {} · nonfatal {} · correctable {}".format(ae.get("pci"), ae.get("fatal"), ae.get("nonfatal"), ae.get("cor")),
                "PCIe 버스에서 복구가 필요한 오류가 났습니다. 그 순간 장치가 리셋되거나 I/O 가 멈췄을 수 있습니다. 슬롯, 라이저, 케이블, 펌웨어 문제가 흔한 원인입니다.",
                "BMC 로그와 커널 로그의 AER 메시지를 확인하고 하드웨어 담당자와 슬롯·펌웨어를 점검하세요.", "Linux PCI sysfs aer_dev_* (커널 4.17+)")
    for c_ in sorted(set(nvme_ctrl(d) for d in phys if nvme_ctrl(d))):
        stn = sto["nvme"].get(c_, {}).get("state")
        if stn and stn != "live":
            err_sev = sev_max(err_sev, "warn")
            add("warn", "오류", OUT, "NVMe 컨트롤러 상태가 live 가 아님 ({} = {})".format(c_, stn), "/sys/class/nvme/{}/state".format(c_),
                "resetting·connecting·dead 는 컨트롤러가 정상 동작하지 않는 상태입니다.", "커널 로그의 nvme 메시지를 확인하세요.", "Linux nvme sysfs")
    for md_ in [k for k in data_chain if k.startswith("md")]:
        mm = num(topo.attr.get(md_, {}).get("md/mismatch_cnt"))
        if mm:
            cfg_sevs.append("info")
            add("info", "설정", "참고", "소프트웨어 RAID {} 불일치 블록 기록".format(md_), "mismatch_cnt={} · sync_action={}".format(int(mm), topo.attr.get(md_, {}).get("md/sync_action")),
                "마지막 점검(check)에서 미러·패리티가 맞지 않는 블록이 발견됐습니다. RAID1·10 은 swap 등으로 생기는 경우도 있지만 RAID5·6 에서는 조사가 필요합니다.",
                "원인을 확인한 뒤 repair 를 검토하세요.", "Linux md(4) mismatch_cnt")

    # 커널 로그의 RAID 컨트롤러 이벤트 (벤더 도구 없이 보는 배터리·논리 디스크·구성 디스크 이상)
    raid_ev = [l for l in klog if re.search(cats["RAID 컨트롤러 이벤트"], l, re.I)]
    if raid_ev:
        s_ = "warn" if any(re.search(r'fail|degrad|offline|FATAL|DEAD|replace|predictive|pinned', l, re.I) for l in raid_ev) else "caution"
        err_sev = sev_max(err_sev, s_)
        add(s_, "오류", OUT, "커널 로그에 RAID 컨트롤러 이벤트 기록 (최근 7일 {}건)".format(len(raid_ev)),
            " / ".join(re.sub(r'^\S+\s+\S+\s+kernel:\s*', '', l)[:160] for l in raid_ev[-3:]),
            "RAID 컨트롤러는 배터리 이상, 논리 디스크 degraded, 디스크 고장·예측 고장, rebuild 같은 이벤트를 커널 로그로 알립니다. "
            "벤더 도구가 없어도 이 기록으로 컨트롤러 쪽 문제를 알 수 있습니다.",
            "리포트 부록의 원문 시각을 기준으로 하드웨어 담당자에게 컨트롤러 이벤트 로그 확인을 요청하세요.",
            "megaraid_sas·hpsa·smartpqi 커널 로그 (megaraid_sas 는 기본 CRITICAL 이상 이벤트 기록)")

    cfg_sev = sev_max(*cfg_sevs) if cfg_sevs else "ok"

    # ═════════════ 7. 플랫폼 자원 (VMware 자원 / 가상화 자원 / 하드웨어) ═════════════
    vm_sevs = []
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
                "ES VM은 메모리 예약 100%(Reserve all guest memory)를 권장합니다.", "VMware 'Performance Best Practices for vSphere' > memory")
        if memlim is not None and 0 < memlim < mem_total_mb * 0.95 and memlim < 4000000:
            vm_sevs.append("warn")
            add("warn", "VMware 자원", "VMware 관리자", "VM에 메모리 한도(limit)가 설정됨", "{} MB".format(memlim),
                "한도를 넘는 메모리는 항상 balloon·swap으로 처리됩니다.", "메모리 limit 해제를 요청하세요.", "VMware resource management 문서")
        if cpulim is not None and 0 < cpulim < 4000000:
            vm_sevs.append("caution")
            add("caution", "VMware 자원", "VMware 관리자", "VM에 CPU 한도(limit)가 설정됨", "{} MHz".format(cpulim),
                "I/O 완료 처리" + vs("와 vSAN 클라이언트 동작", "", "와 vSAN 클라이언트 동작") + "에도 CPU가 필요합니다.", "CPU limit 해제를 요청하세요.", "VMware resource management 문서")
    # ── 하드웨어 (bare-metal): 장치 자체의 상태 ─────────────────────────────
    HW = {"nvme": [], "md": [], "smart": [], "fc": [], "governor": None, "raid": []}
    if kind == "baremetal":
        # NVMe 온도: hwmon temp1_max 는 컨트롤러의 경고 온도(WCTEMP), temp1_crit 는 위험 온도(CCTEMP).
        # 경고 온도를 넘으면 컨트롤러가 스스로 성능을 낮추는(thermal throttling) 구간에 들어간다
        data_ctrls = sorted(set(nvme_ctrl(d) for d in phys if nvme_ctrl(d)))
        for c in data_ctrls:
            nv = sto["nvme"].get(c, {})
            t, tmax, tcrit = num(nv.get("temp")), num(nv.get("temp_max")), num(nv.get("temp_crit"))
            row = {"ctrl": c, "model": nv.get("model", ""), "fw": nv.get("firmware_rev", ""),
                   "temp": t / 1000.0 if t else None, "tmax": tmax / 1000.0 if tmax else None,
                   "tcrit": tcrit / 1000.0 if tcrit else None, "alarm": nv.get("temp_alarm"),
                   "ls": nv.get("link_speed", ""), "mls": nv.get("max_link_speed", ""),
                   "lw": nv.get("link_width", ""), "mlw": nv.get("max_link_width", "")}
            HW["nvme"].append(row)
            if row["alarm"] == "1" or (row["temp"] and row["tmax"] and row["temp"] >= row["tmax"]):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, "NVMe 온도가 경고 온도에 도달 ({})".format(c),
                    "현재 {:.0f}°C · 경고 온도 {} · 위험 온도 {}{}".format(row["temp"] or 0, fmt(row["tmax"], 0, "°C"), fmt(row["tcrit"], 0, "°C"),
                                                                 " · 온도 경고 비트 켜짐" if row["alarm"] == "1" else ""),
                    "NVMe는 경고 온도를 넘으면 장치를 보호하려고 스스로 성능을 낮춥니다. 이때는 부하가 그대로여도 지연이 오르고 처리량이 떨어집니다.",
                    "서버 팬 정책, 드라이브 베이 공기 흐름, 빈 슬롯 블랭크 장착 여부를 하드웨어 담당자와 확인하세요. BMC의 온도 이력도 함께 봅니다.",
                    "Linux nvme hwmon (temp1_max = WCTEMP, temp1_crit = CCTEMP), NVMe 규격 Composite Temperature")
            elif row["temp"] and row["tmax"] and row["temp"] >= row["tmax"] - 5:
                vm_sevs.append("caution")
                add("caution", RES_DIM, OUT, "NVMe 온도가 경고 온도에 근접 ({})".format(c),
                    "현재 {:.0f}°C · 경고 온도 {:.0f}°C".format(row["temp"], row["tmax"]),
                    "경고 온도까지 5°C 이내입니다. 부하가 더 오르면 성능 제한 구간에 들어갈 수 있습니다.",
                    "냉각 상태를 미리 점검하세요.", "Linux nvme hwmon (temp1_max = WCTEMP)")
            def gts(x):
                m_ = re.search(r'([\d.]+)\s*GT/s', x or "")
                return float(m_.group(1)) if m_ else None
            cs, ms_ = gts(row["ls"]), gts(row["mls"])
            cw, mw = num(row["lw"]), num(row["mlw"])
            if (cs and ms_ and cs < ms_) or (cw and mw and cw < mw):
                vm_sevs.append("caution")
                add("caution", RES_DIM, OUT, "NVMe PCIe 링크가 최대치보다 낮게 연결됨 ({})".format(c),
                    "현재 {} x{} / 최대 {} x{}".format(row["ls"] or "-", row["lw"] or "-", row["mls"] or "-", row["mlw"] or "-"),
                    "링크 속도나 폭이 줄면 장치가 낼 수 있는 최대 처리량이 그만큼 줄어듭니다. 슬롯·케이블·백플레인 문제나 BIOS 설정이 원인일 수 있습니다. "
                    "일부 플랫폼은 유휴 상태에서 링크 속도를 낮추므로 부하 중 값을 다시 확인해야 합니다.",
                    "부하가 있는 시간에 /sys/class/nvme/{}/device/current_link_speed 를 다시 확인하고, 계속 낮으면 슬롯 위치와 BIOS PCIe 설정을 하드웨어 담당자와 점검하세요.".format(c),
                    "Linux PCI sysfs (current_link_speed, max_link_speed)")
        # 소프트웨어 RAID
        md_text = rd(S, "mdstat")
        data_md = set(d for d in logical if d.startswith("md"))
        cur_md = None
        for line in md_text.splitlines():
            m_ = re.match(r'^(md\w+)\s*:\s*(\w+)\s+(\S+)?', line)
            if m_:
                cur_md = {"name": m_.group(1), "state": m_.group(2), "level": m_.group(3) or "", "status": "", "op": ""}
                HW["md"].append(cur_md); continue
            if cur_md is None:
                continue
            m_ = re.search(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]', line)
            if m_:
                cur_md["status"] = "{}/{} [{}]".format(m_.group(1), m_.group(2), m_.group(3))
                cur_md["degraded"] = "_" in m_.group(3)
            m_ = re.search(r'(resync|recovery|reshape|check|repair)\s*=\s*([\d.]+%)', line)
            if m_:
                cur_md["op"] = "{} {}".format(m_.group(1), m_.group(2))
        for md_ in HW["md"]:
            mine = md_["name"] in data_md
            if md_.get("degraded"):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, "소프트웨어 RAID {} 가 degraded 상태".format(md_["name"]),
                    "{} {} · {}{}".format(md_["name"], md_["level"], md_["status"], " · " + md_["op"] if md_["op"] else ""),
                    "구성 디스크 일부가 빠진 상태입니다. 남은 디스크로 읽기를 대신 계산하거나 이중화 없이 동작하므로 성능과 안전성이 함께 떨어집니다."
                    + (" ES data 가 이 장치에 있습니다." if mine else ""),
                    "빠진 디스크를 확인·교체하고 재구성하세요. 재구성 중에는 디스크 부하가 크게 늘어납니다.",
                    "/proc/mdstat")
            elif md_["op"]:
                s_ = "caution" if (mine and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]) else "info"
                vm_sevs.append(s_)
                add(s_, RES_DIM, "참고" if s_ == "info" else OUT, "소프트웨어 RAID {} 에서 {} 진행 중".format(md_["name"], md_["op"].split()[0]),
                    "{} {} · {}".format(md_["name"], md_["level"], md_["op"]),
                    "resync·recovery·check 는 디스크 전체를 읽고 쓰는 작업이라 이 시간대 측정값은 평상시보다 나쁘게 나옵니다."
                    + (" ES data 가 이 장치에 있습니다." if mine else ""),
                    "작업이 끝난 뒤 다시 측정하세요. 정기 check 라면 서비스 피크를 피하도록 시간(/etc/cron.d/raid-check 등)을 조정합니다.",
                    "/proc/mdstat")
        # CPU governor
        gov, gdrv = (virt.get("cpu_governor") or "").strip(), (virt.get("cpu_scaling_driver") or "").strip()
        HW["governor"] = (gov, gdrv)
        if gov in ("powersave", "conservative", "ondemand"):
            s_ = "info" if (gov == "powersave" and gdrv in ("intel_pstate", "amd-pstate", "amd-pstate-epp")) else "caution"
            vm_sevs.append(s_)
            add(s_, RES_DIM, "서버 담당자", "CPU 주파수 정책이 절전 쪽 ({})".format(gov),
                "scaling_governor={} · driver={} · tuned={}".format(gov, gdrv or "-", tuned_prof or "-"),
                "절전 정책에서는 CPU가 낮은 주파수와 깊은 절전 상태에 머물다 깨어나며, I/O 완료 처리와 ES 요청 처리가 그만큼 늦어집니다. "
                + ("intel_pstate·amd-pstate의 powersave 는 부하에 따라 주파수를 올리므로 영향이 작은 편입니다." if s_ == "info" else ""),
                "tuned-adm profile throughput-performance 를 적용하면 governor 가 performance 로 바뀝니다. BIOS 전원 정책도 함께 확인하세요.",
                "[Red Hat 공식] TuneD profiles (throughput-performance 는 절전 기능을 끔)")
        # FC 포트 상태
        HW["fc"] = sto["fc"]
        down = [f_ for f_ in sto["fc"] if f_["state"] and f_["state"].lower() not in ("online",)]
        if down and attach == "san":
            vm_sevs.append("warn")
            add("warn", RES_DIM, "스토리지 관리자", "FC HBA 포트 일부가 Online 이 아님",
                ", ".join("{} {} ({})".format(f_["host"], f_["state"], f_["speed"]) for f_ in down),
                "경로가 줄면 남은 경로로 I/O가 몰려 지연이 늘고, 남은 경로마저 끊기면 I/O 오류로 이어집니다.",
                "스위치 포트, 케이블, zoning 을 스토리지 관리자와 확인하고 multipath -ll 로 경로 상태를 점검하세요.",
                "/sys/class/fc_host/*/port_state")
        # 하드웨어 RAID 컨트롤러 (storcli·perccli / ssacli / arcconf 조회 결과)
        data_devs = set(phys)
        for r in hwraid:
            HW["raid"].append(r)
            for c in r["ctrl"]:
                st = (c.get("status") or "").strip()
                if st and st.lower() not in ("ok", "optimal", "okay"):
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, "RAID 컨트롤러 상태 이상 ({})".format(c["name"]), "Controller Status: {}".format(st),
                        "컨트롤러 자체가 정상 상태가 아니면 모든 논리 디스크의 I/O가 영향을 받습니다.",
                        "컨트롤러 이벤트 로그와 BMC 로그를 하드웨어 담당자와 확인하세요.", "{} 조회".format(r["tool"]))
                bt = (c.get("battery") or "").strip()
                if bt and not re.search(r'^(ok|optimal|ready|zmm optimal|not present|not installed|absent|zmm not installed)$', bt, re.I):
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, "RAID 컨트롤러 배터리·캐시 보호 모듈이 정상이 아님", "{}: {}".format(c["name"], bt),
                        "배터리(또는 CacheVault·ZMM)가 충전 중이거나 이상이면 컨트롤러는 데이터를 지키려고 쓰기 캐시를 write-through로 바꿉니다. "
                        "그러면 fsync마다 디스크까지 가야 해서 쓰기 지연이 크게 늘어납니다. 학습 주기(learn cycle) 중에도 잠시 이렇게 됩니다.",
                        "배터리·캐시 모듈 상태와 학습 주기 일정을 확인하고, 이상이면 교체를 검토하세요.", "{} 조회".format(r["tool"]))
                cs = (c.get("cache") or "").strip()
                if cs and cs.upper() != "OK":
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, "RAID 컨트롤러 캐시가 꺼져 있음", "{}: Cache Status {}".format(c["name"], cs),
                        "컨트롤러 캐시가 꺼지면 모든 쓰기가 디스크 속도로 처리됩니다.",
                        "캐시가 꺼진 원인(배터리 충전·이상, 캐시 모듈 오류)을 하드웨어 담당자와 확인하세요.", "{} 조회".format(r["tool"]))
            for v in r["vds"]:
                mine = v.get("dev") in data_devs
                where = "ES data" if mine else "다른 용도"
                st = v.get("state") or "-"
                if not v.get("ok") and v.get("state"):
                    s_ = ("crit" if re.search(r'fail|offline|OfLn', st, re.I) else "warn") if mine else "caution"
                    vm_sevs.append(s_)
                    add(s_, RES_DIM, OUT, "RAID 논리 디스크가 정상 상태가 아님 ({} {})".format(v.get("dev") or "VD " + str(v.get("id")), where),
                        "{} · 상태 {} · 구성 디스크 {}".format(v.get("level") or "-", st,
                                                        ", ".join("{} {}".format(p_["id"], p_["state"]) for p_ in v.get("pds", [])[:8]) or "-"),
                        "구성 디스크 일부가 빠졌거나 재구성 중입니다. 남은 디스크로 데이터를 다시 계산하며 동작하므로 읽기·쓰기가 모두 느려지고, "
                        "이중화가 없는 상태라 디스크가 하나 더 고장 나면 데이터를 잃습니다.",
                        "빠진 디스크를 교체하고 재구성 진행을 확인하세요. 재구성 중에는 디스크 부하가 크게 늘어 이 시간대 측정값이 나빠집니다.",
                        "{} 조회".format(r["tool"]))
                if mine and v.get("wb") is False:
                    hddish = (v.get("media") or storage) == "hdd"
                    s_ = "warn" if (hddish or SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]) else "info"
                    vm_sevs.append(s_)
                    ini = v.get("cache_init") or ""
                    fell = "back" in ini.lower()
                    add(s_, RES_DIM, OUT if s_ != "info" else "참고",
                        "ES data RAID 쓰기 캐시가 write-through로 동작 중" + (" (설정은 write-back)" if fell else ""),
                        "{} {} · 현재 캐시 {}{} · 쓰기 p95 {}".format(v.get("dev"), v.get("level") or "", v.get("cache_cur") or "-",
                                                               " · 설정 " + ini if ini else "", fmt(A["w_await_p95"], 2, "ms")),
                        ("설정은 write-back인데 지금은 write-through로 동작합니다. 배터리·캐시 모듈 이상이나 학습 주기 때문인 경우가 대부분입니다. " if fell else
                         "write-through에서는 fsync마다 디스크 기록이 끝나야 응답합니다. ")
                        + ("HDD에서는 쓰기 지연이 수 배로 늘어납니다." if hddish else
                           "SSD에서는 영향이 작은 편이라 벤더가 write-through를 권하기도 합니다(SSD 가속 경로)."),
                        "배터리·캐시 모듈 상태를 먼저 확인하고, 보호되는 캐시가 있다면 write-back 정책을 검토하세요. 정책 변경은 운영 영향이 있어 점검 시간에 합니다.",
                        "{} 조회. 캐시 정책 판단은 컨트롤러 도구 기준 (커널 write_cache 값은 쓰지 않음)".format(r["tool"]))
                lvl = (v.get("level") or "").upper().replace(" ", "")
                if mine and re.search(r'RAID(5|6|50|60)|^5$|^6', lvl + "|" + str(v.get("level"))):
                    vm_sevs.append("info")
                    add("info" if SEV_ORDER.get(w_sev, 0) < SEV_ORDER["caution"] else "caution", RES_DIM, "참고" if SEV_ORDER.get(w_sev, 0) < SEV_ORDER["caution"] else OUT,
                        "ES data 가 패리티 RAID({}) 위에 있음".format(v.get("level")),
                        "{} · 쓰기 p95 {}".format(v.get("dev"), fmt(A["w_await_p95"], 2, "ms")),
                        "RAID 5/6은 쓰기마다 패리티를 다시 계산하느라 읽기-수정-쓰기가 생깁니다. ES replica가 이미 이중화를 하므로 "
                        "Elastic은 성능 쪽으로 RAID 0 stripe를 예로 듭니다.",
                        "지금 쓰기 지연이 문제가 아니면 그대로 두어도 됩니다. 증설·재구축 때 RAID 0(또는 10)과 ES replica 조합을 검토하세요.",
                        "[Elastic 공식] Tune for indexing speed (RAID 0 stripe)")
            for desc, sv in r["pds_bad"]:
                vm_sevs.append(sv)
            if r["pds_bad"]:
                sv = sev_max(*[x[1] for x in r["pds_bad"]])
                add(sv, RES_DIM, OUT, "RAID 구성 디스크에 이상 징후",
                    " · ".join(x[0] for x in r["pds_bad"][:8]),
                    "predictive failure, SMART 경고, media error 는 디스크가 약해지고 있다는 신호입니다. 재시도 때문에 I/O가 느려지고, 결국 고장으로 이어질 수 있습니다.",
                    "해당 디스크의 교체 여부를 하드웨어 담당자와 검토하세요. 교체 뒤 재구성 중에는 부하가 늘어납니다.",
                    "{} 조회".format(r["tool"]))
            if r["bg"]:
                vm_sevs.append("info")
                add("caution" if SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"] else "info", RES_DIM, "참고",
                    "RAID 컨트롤러 백그라운드 작업 진행 중", " · ".join(r["bg"][:6]),
                    "rebuild, patrol read, consistency check 는 디스크 전체를 읽고 쓰는 작업이라 이 시간대 측정값은 평상시보다 나쁘게 나옵니다.",
                    "작업이 끝난 뒤 다시 측정하세요. 정기 작업이라면 서비스 피크를 피하도록 일정을 조정합니다.", "{} 조회".format(r["tool"]))
        for rdv in topo.raiddev:               # 커널 raid_class (mpt*sas IR 볼륨 등)
            st = (rdv.get("state") or "").lower()
            if st and st not in ("active", "optimal", "ok", "unknown"):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, "RAID 볼륨 상태 이상 ({})".format(rdv.get("dev") or rdv["name"]),
                    "level {} · state {} · resync {}".format(rdv.get("level"), rdv.get("state"), rdv.get("resync")),
                    "커널이 보고한 RAID 볼륨 상태가 정상이 아닙니다.", "컨트롤러 도구와 BMC 로그로 구성 디스크 상태를 확인하세요.",
                    "/sys/class/raid_devices")
        for h_, at in topo.hostattr.items():
            if str(at.get("fw_crash_state", "0")).strip() not in ("0", ""):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, "RAID 컨트롤러 펌웨어 크래시 기록 ({})".format(h_), "fw_crash_state={}".format(at["fw_crash_state"]),
                    "megaraid_sas 드라이버가 컨트롤러 펌웨어 크래시를 보고했습니다. 그 순간 I/O가 멈췄을 수 있습니다.",
                    "컨트롤러 이벤트 로그와 펌웨어 버전을 하드웨어 담당자와 확인하세요.", "megaraid_sas sysfs")
        raid_data = any(devcls.get(d, {}).get("raid") for d in phys)
        if raid_data and not hwraid and raid_absent:
            add("info", RES_DIM, "참고", "RAID 컨트롤러 도구가 없어 캐시·배터리·구성 디스크 상태는 판정하지 못함",
                "필요한 도구: {}".format(", ".join(raid_absent)),
                "ES data 가 RAID 논리 디스크 위에 있습니다. 컨트롤러 도구가 있으면 쓰기 캐시 정책, 배터리, 구성 디스크 상태, 재구성 진행까지 자동으로 봅니다.",
                "서버 벤더의 RAID 관리 도구(Broadcom·Dell: storcli 또는 perccli, HPE: ssacli, Microchip: arcconf)를 설치한 뒤 다시 수집하세요. "
                "조회만 하고 설정은 바꾸지 않습니다.", "RAID 컨트롤러 드라이버 " + ", ".join(sorted(set(c["drv"] for c in devcls.values() if c["raid"]))))
        if unsure and storage_auto and attach == "local":
            add("info", RES_DIM, "참고", "RAID 논리 디스크라 매체 종류를 추정으로 판정",
                ", ".join("{}: {}".format(d, devcls[d]["why"]) for d in unsure),
                "RAID 컨트롤러는 논리 디스크의 rotational 값을 실제 매체와 다르게 보고하는 경우가 있습니다. 판정 기준({})이 실제와 다르면 지연 판정이 크게 달라집니다.".format(STORAGE_LABEL[storage]),
                "실제 매체가 다르면 -s ssd 또는 -s hdd 로 다시 분석하세요 (번들만 있으면 python3 es_disk_render.py <번들> --storage ssd).",
                "커널 queue/rotational, SCSI host 드라이버")
    # SMART (--smart 로 수집한 경우). 플랫폼과 무관하게 읽지만 가상 디스크에서는 대개 의미가 없다
    smart_txt = rd(S, "smart")
    if "#SMARTCTL_ABSENT" in smart_txt and kind == "baremetal" and attach == "local" and \
            not all(devcls.get(d, {}).get("raid") for d in phys):
        add("info", RES_DIM, "참고", "smartctl 이 없어 디스크 SMART 상태는 판정하지 못함", "smartmontools 미설치",
            "bare-metal 에서는 디스크 자체의 건강 상태(재할당 섹터, 미정정 오류, NVMe critical warning)를 자동으로 봅니다.",
            "smartmontools 를 설치한 뒤 다시 수집하면 자동으로 포함됩니다. 조회만 하고 self-test 는 시작하지 않습니다.", "smartctl")
    if smart_txt and "#SMARTCTL_ABSENT" not in smart_txt:
        cur = None
        for line in smart_txt.splitlines():
            if line.startswith("#DEV "):
                cur = {"dev": line.split()[1], "health": "", "notes": [], "sev": "ok"}
                HW["smart"].append(cur); continue
            if cur is None:
                continue
            m_ = re.search(r'(overall-health self-assessment test result|SMART Health Status):\s*(\S+)', line)
            if m_:
                cur["health"] = m_.group(2)
                if m_.group(2).upper() not in ("PASSED", "OK"):
                    cur["sev"] = "crit"; cur["notes"].append("자가 진단 " + m_.group(2))
            m_ = re.match(r'^\s*\d+\s+(Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Reported_Uncorrect)\s+.*\s(\d+)\s*$', line)
            if m_ and int(m_.group(2)) > 0:
                cur["notes"].append("{} {}".format(m_.group(1), m_.group(2)))
                cur["sev"] = sev_max(cur["sev"], "caution" if m_.group(1) == "Reallocated_Sector_Ct" else "warn")
            m_ = re.match(r'^Critical Warning:\s*(0x[0-9a-fA-F]+)', line)
            if m_ and int(m_.group(1), 16) != 0:
                cur["notes"].append("NVMe Critical Warning " + m_.group(1)); cur["sev"] = sev_max(cur["sev"], "warn")
            m_ = re.match(r'^Percentage Used:\s*(\d+)%', line)
            if m_ and int(m_.group(1)) >= 90:
                cur["notes"].append("수명 사용률 {}%".format(m_.group(1))); cur["sev"] = sev_max(cur["sev"], "caution")
            m_ = re.match(r'^Media and Data Integrity Errors:\s*([\d,]+)', line)
            if m_ and int(m_.group(1).replace(",", "")) > 0:
                cur["notes"].append("Media and Data Integrity Errors " + m_.group(1)); cur["sev"] = sev_max(cur["sev"], "warn")
            m_ = re.search(r'(elements in grown defect list):\s*(\d+)', line)
            if m_ and int(m_.group(2)) > 0:
                cur["notes"].append("grown defect {}".format(m_.group(2))); cur["sev"] = sev_max(cur["sev"], "caution")
        bad = [x for x in HW["smart"] if x["sev"] != "ok"]
        if bad:
            sv = sev_max(*[x["sev"] for x in bad])
            vm_sevs.append(sv)
            add(sv, RES_DIM, OUT if kind == "baremetal" else "참고", "SMART 에 디스크 이상 징후",
                " · ".join("{}: {}".format(x["dev"], ", ".join(x["notes"])) for x in bad),
                "재할당·보류 섹터, 미정정 오류, NVMe critical warning 은 디스크가 약해지고 있다는 신호입니다. 해당 디스크의 I/O는 재시도 때문에 느려지고, 결국 장애로 이어질 수 있습니다.",
                "해당 디스크의 교체 여부를 하드웨어 담당자와 검토하세요. ES 쪽은 이 노드의 replica 가 다른 노드에 온전히 있는지(_cluster/health green) 먼저 확인합니다.",
                "smartctl -H -A (smartmontools)")

    steal = [r.get("steal") for r in sysr if r.get("steal") is not None]
    st95 = pctl(steal, .95)
    if st95 is not None and st95 >= 5:
        s = "warn" if st95 >= 10 else "caution"
        vm_sevs.append(s)
        add(s, RES_DIM, OUT, "CPU steal 발생. 호스트 CPU 경합", "p95 {} · 최대 {}".format(fmt(st95, 1, "%"), fmt(vmax(steal), 1, "%")),
            "VM이 실행하려 할 때 호스트가 CPU를 내주지 못한 시간입니다. " + (
                vs("vSAN은 호스트 CPU로 동작하므로 경합이 크면 I/O 처리도 늦어집니다.",
                   "I/O 완료 처리도 CPU가 있어야 진행되므로 경합이 크면 디스크 응답도 늦어집니다.",
                   "I/O 완료 처리도 CPU가 있어야 진행되고, vSAN이면 vSAN 자체도 호스트 CPU를 쓰므로 경합이 크면 디스크 응답이 늦어집니다.") if is_vmware else
                "I/O 완료 처리도 CPU가 있어야 진행되므로 경합이 크면 디스크 응답도 늦어집니다."),
            "호스트 과할당 여부, VM CPU ready 값을 {}에게 확인 요청하세요.".format(OUT), "Linux /proc/stat steal")
    vm_sev = sev_max(*vm_sevs) if vm_sevs else ("ok" if (is_vmware or kind == "baremetal") else "na")

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
            "ethtool -g로 RX ring 최대값 확인 후 상향 검토." + (" 호스트 쪽 vmxnet3 큐 상태는 VMware 관리자에게 확인." if is_vmware else
            " 스위치 포트 오류 카운터도 함께 확인." if kind == "baremetal" else " 호스트 쪽 가상 NIC 큐 상태는 {}에게 확인.".format(OUT)), "ethtool(8)")
    rt = [r.get("retrans_pct") for r in sysr if r.get("retrans_pct") is not None]
    if rt and pctl(rt, .95) >= 1.0:
        net_sev = sev_max(net_sev, "caution")
        add("caution", "네트워크", "원인 분리 필요", "TCP 재전송률이 높음", "p95 {}".format(fmt(pctl(rt, .95), 2, "%")),
            "ES 노드 간 통신 품질이 떨어져 복제본 응답이 늦어질 수 있습니다.", "네트워크 경로·NIC 설정 점검.", "/proc/net/snmp")
    if is_vmware and VMBK != "ds":
        add("info", "네트워크", "참고", "vSAN 네트워크는 Guest에서 보이지 않음" + ("" if VMBK == "vsan" else " (vSAN 데이터스토어인 경우)"),
            "Guest NIC에는 ES 트래픽만 흐름. vSAN 복제 트래픽은 ESXi vmkernel 포트로 흐름",
            "vSAN 네트워크 지연은 Guest에서 '쓰기 응답시간 증가'로만 간접 관측됩니다. 그래서 네트워크는 보조 지표로만 씁니다.",
            "쓰기 지연이 높으면 VMware 관리자에게 vSAN 네트워크(전용 대역, 25GbE 이상 권장, 재전송·지연)를 확인 요청하세요.", "[VMware 공식] Troubleshooting vSAN Performance. 2% 패킷 손실로 스토리지 성능 32% 저하, vSwitch 드롭 0.0001% 이하 권고")

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
                "해당 시각의 배치·스냅샷·백업{} 작업 이력을 대조하고, 같은 시간대에 이 도구로 재측정하세요.".format(
                    "·" + VMB if is_vmware else ("·RAID 점검(patrol read, consistency check)" if kind == "baremetal" else "")), "sysstat sar 이력")

    # ═════════════ 10. 벤치(선택) → 여유율 ═════════════
    # 수집기가 번들에 넣어 둔 벤치 결과가 있으면 --bench 없이도 쓴다
    if not bench_dir and os.path.isdir(os.path.join(base, "bench")):
        bench_dir = os.path.join(base, "bench")
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
        fs_ = b.get("fsync_4k") or {}
        sync_v = fs_.get("sync_p99") or fs_.get("sync_avg")
        if sync_v and sync_v >= th["caution"]:
            add("warn" if sync_v >= th["warn"] else "caution", "지연", OUT if kind == "baremetal" else "원인 분리 필요",
                "동기 쓰기(fsync) 한 건 지연이 기준보다 큼 (벤치 {} {})".format("p99" if fs_.get("sync_p99") else "평균", fmt(sync_v, 2, "ms")),
                "4KiB 동기 쓰기 {} {} · 판정 기준 주의 {}ms".format("p99" if fs_.get("sync_p99") else "평균", fmt(sync_v, 2, "ms"), th["caution"]),
                "ES 기본 설정은 bulk 요청마다 translog 를 fsync 합니다. 이 값이 크면 부하와 상관없이 인덱싱 요청 한 번의 최소 시간이 길어집니다.",
                "장치의 쓰기 캐시 보호(전원 차단 보호 SSD, 배터리 보호 RAID 캐시)와 스토리지 쓰기 경로를 확인하세요.",
                "es_disk_bench.sh (fio fdatasync 또는 dd oflag=dsync), [Elastic 공식] Translog settings")
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
    if schs and kind == "baremetal":
        recs = sorted(set("{} {}".format(sched_rec(d)[1], "/".join(sched_rec(d)[0][:2])) for d, _ in schs))
        bp("OS·블록 장치", "I/O 스케줄러", " · ".join(recs), ", ".join("{} {}".format(d, v) for d, v in schs),
           "caution" if sch_bad else "ok", "[Red Hat 공식] Setting the disk scheduler")
    elif schs:
        bp("OS·블록 장치", "I/O 스케줄러", "mq-deadline 또는 none", ", ".join("{} {}".format(d, v) for d, v in schs),
           "caution" if any(v in ("cfq", "bfq") for _, v in schs) else "ok", "[Red Hat 공식] Setting the disk scheduler")
    ios = [topo.attr.get(d, {}).get("queue/iostats") for d in phys]
    if any(ios):
        bp("OS·블록 장치", "I/O 통계 수집(iostats)", "1 (켜짐)", ", ".join(x or "-" for x in ios), "warn" if "0" in ios else "ok", "커널 block layer")
    tos = [num(topo.attr.get(d, {}).get("device/timeout")) for d in phys]
    tos = [t for t in tos if t is not None]
    if is_vmware:
        bp("OS·블록 장치", "SCSI 명령 타임아웃", "60초 이상 (open-vm-tools 180초)", ", ".join("{}s".format(int(t)) for t in tos) or "해당 없음",
           ("warn" if min(tos) < 60 else "ok") if tos else "na", "open-vm-tools udev 규칙")
    qds = [topo.attr.get(d, {}).get("device/queue_depth") for d in phys if topo.attr.get(d, {}).get("device/queue_depth")]
    if is_vmware:
        bp("OS·블록 장치", "가상 디스크 queue_depth", "기본값 유지, 큐 포화가 확인될 때만 상향(254)", ", ".join(qds) or "-",
           ("warn" if (qratio or 0) >= 0.8 else "ok") if qds else "na", "VMware KB 2053145")
    elif qds:
        bp("OS·블록 장치", "LUN queue_depth" if attach == "san" else "장치 queue_depth", "큐 사용률 80% 미만",
           "{} · 큐 사용률 p95 {}".format(", ".join(qds), fmt((qratio or 0) * 100, 0, "%") if qratio is not None else "-"),
           "warn" if (qratio or 0) >= 0.8 else "ok", "[실무 기준] 큐 사용률 구간")
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
        nb = "nobarrier" in o or "barrier=0" in o
        bp("파일시스템", "쓰기 순서 보장 barrier ({})".format(m_["mnt"]), "켜짐 (기본)", "꺼짐" if nb else "켜짐",
           "warn" if nb else "ok", "mount(8), ext4(5)")
        sy = "sync" in o or "dirsync" in o
        bp("파일시스템", "동기 마운트 ({})".format(m_["mnt"]), "끔 (기본)", "켜짐" if sy else "꺼짐", "warn" if sy else "ok", "mount(8)")
    fst = rd(S, "fstrim").split()
    bp("파일시스템", "fstrim.timer", vs("주기 실행 권장 (vSAN Guest TRIM 사용 시 의미)", "주기 실행 권장 (thin 데이터스토어 공간 회수 시 의미)",
                                        "주기 실행 권장 (vSAN TRIM 또는 thin 데이터스토어 공간 회수 시 의미)") if is_vmware else
       ("주기 실행 권장 (SSD·NVMe 쓰기 성능 유지)" if kind == "baremetal" else "주기 실행 권장 (가상 디스크 공간 회수)"),
       fst[0] if fst else "미확인", "info", "systemd fstrim.timer")
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
    bp("커널", "Transparent HugePage", "madvise 또는 never (Elastic 필수 아님)", thp_cur or "-", "info" if thp_cur == "always" else "ok", "[참고] DB 벤더 운영 관행. Elastic 공식 요구사항 아님")
    bp("커널", "dirty page 기준", "기본값 유지, 쓰기 지연 급등 시 바이트 단위 검토",
       "ratio {}/{} · bytes {}/{}".format(sysctl.get("vm.dirty_background_ratio", "-"), sysctl.get("vm.dirty_ratio", "-"),
                                         sysctl.get("vm.dirty_background_bytes", "-"), sysctl.get("vm.dirty_bytes", "-")), "info", "커널 문서")
    if kind == "baremetal":
        bp("OS·블록 장치", "tuned profile", "throughput-performance (물리 서버 권고). readahead·dirty_ratio를 바꿀 수 있어 함께 확인",
           tuned_prof or "미확인",
           "ok" if tuned_prof.lower() in ("throughput-performance", "latency-performance", "network-throughput", "network-latency")
           else ("na" if not tuned_prof else "info"),
           "[Red Hat 공식] TuneD profiles (설치 시 컴퓨트 노드는 throughput-performance, VM은 virtual-guest 선택. "
           "throughput-performance는 절전 기능을 끔)")
    else:
        bp("OS·블록 장치", "tuned profile", "virtual-guest (VM 권고). readahead·dirty_ratio를 바꿀 수 있어 함께 확인",
           tuned_prof or "미확인",
           "ok" if tuned_prof.lower().endswith("virtual-guest") else ("na" if not tuned_prof else "info"),
           "[Red Hat 공식] TuneD profiles (virtual-guest는 throughput-performance 기반, swappiness를 낮추고 dirty_ratio를 올림)")
    bp("커널", "PSI(I/O 압박 지표)", "사용 가능 (RHEL 8은 psi=1)", sysctl.get("psi", "-"), "ok" if sysctl.get("psi") == "available" else "info", "커널 문서")
    bp("ES 프로세스", "파일 핸들 한도", "65535 이상", nofile or "-", ("ok" if (nofile == "unlimited" or num(nofile, 0) >= 65535) else "crit") if nofile else "na", "Elastic 공식")
    if heap_mb and mem_total_mb:
        bp("ES 프로세스", "JVM heap", "RAM의 50% 이하, 약 31GB 이하",
           "{:.1f}GB / RAM {:.1f}GB ({:.0f}%)".format(heap_mb / 1024, mem_total_mb / 1024, 100 * heap_mb / mem_total_mb),
           "caution" if (heap_mb / mem_total_mb > 0.5 or heap_mb > 31 * 1024) else "ok", "Elastic 공식")
    bp("ES 프로세스", "cgroup I/O 제한", "없음", "있음" if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else "없음",
       "warn" if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else "ok", "cgroup v2")
    bp("구성", "OS와 ES data 디스크 분리", "별도 디스크" if kind == "baremetal" else "별도 가상 디스크",
       "같은 디스크: " + ", ".join(share_dev) if share_dev else "분리됨",
       "info" if share_dev else "ok", "[VMware 공식] Performance Best Practices. 워크로드별 컨트롤러 분리" if is_vmware else "[실무 기준] OS I/O 와 ES I/O 분리")
    dh = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
    rh = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
    if dh and rh and is_vmware:
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
    if kind == "baremetal":
        cls_txt = ", ".join("{} {}{}".format(d, STORAGE_LABEL.get(c["media"], c["media"] or "-"),
                                             "" if c["sure"] else "(추정)") for d, c in sorted(devcls.items()))
        bp("하드웨어", "ES data 장치 종류", "SSD 권장 (NVMe 우선). 로컬 직결 스토리지", cls_txt + (" · " + attach.upper() if attach != "local" else ""),
           "info" if (storage == "hdd" or attach != "local") else "ok",
           "[Elastic 공식] Tune for indexing/search speed (SSD 권장, 로컬 직결 스토리지가 일반적으로 더 빠름)")
        gov_, gdrv_ = HW["governor"] or ("", "")
        if gov_:
            bp("하드웨어", "CPU governor", "performance (tuned throughput-performance)", "{} ({})".format(gov_, gdrv_ or "-"),
               "ok" if gov_ == "performance" else ("info" if gdrv_ in ("intel_pstate", "amd-pstate", "amd-pstate-epp") and gov_ == "powersave" else "caution"),
               "[Red Hat 공식] TuneD profiles")
        for r_ in HW["nvme"]:
            if r_["temp"] is not None:
                bp("하드웨어", "NVMe 온도 ({})".format(r_["ctrl"]), "경고 온도(WCTEMP) 미만", "{:.0f}°C / 경고 {}".format(r_["temp"], fmt(r_["tmax"], 0, "°C")),
                   "warn" if (r_["tmax"] and r_["temp"] >= r_["tmax"]) else ("caution" if (r_["tmax"] and r_["temp"] >= r_["tmax"] - 5) else "ok"),
                   "Linux nvme hwmon")
            if r_["ls"] and r_["mls"]:
                bp("하드웨어", "NVMe PCIe 링크 ({})".format(r_["ctrl"]), "최대 속도·폭으로 연결", "{} x{} / 최대 {} x{}".format(r_["ls"], r_["lw"], r_["mls"], r_["mlw"]),
                   "ok" if (r_["ls"] == r_["mls"] and r_["lw"] == r_["mlw"]) else "caution", "Linux PCI sysfs")
        for md_ in HW["md"]:
            bp("하드웨어", "소프트웨어 RAID {}".format(md_["name"]), "정상 (degraded·재구성 없음)",
               "{} {} {}".format(md_["level"], md_["status"], md_["op"]).strip(),
               "warn" if md_.get("degraded") else ("info" if md_["op"] else "ok"), "/proc/mdstat")
    for r in HW["raid"]:
        bat = ", ".join(c.get("battery") for c in r["ctrl"] if c.get("battery")) or "-"
        for v in r["vds"]:
            if v.get("dev") not in phys:
                continue
            st_ = "ok"
            if not v.get("ok"):
                st_ = "warn"
            elif v.get("wb") is False:
                st_ = "warn" if (v.get("media") or storage) == "hdd" else "info"
            bp("하드웨어", "RAID 논리 디스크 ({})".format(v["dev"]), "정상 상태, 보호되는 write-back 캐시 (SSD 는 write-through 도 가능)",
               "{} · {} · 캐시 {} · 배터리 {} · 구성 디스크 {}개".format(v.get("level") or "-", v.get("state") or "-", v.get("cache_cur") or "-",
                                                                bat, len(v.get("pds") or [])), st_, "{} 조회".format(r["tool"]))
    bp("구성", "ES data 아래 device-mapper 계층", "일반 LV·파티션 (thin pool·snapshot 없음)",
       ", ".join(DM["targets"]) or "없음",
       "warn" if ("snapshot-origin" in DM["targets"] or "snapshot" in DM["targets"]) else
       ("caution" if "thin" in DM["targets"] else ("info" if DM["targets"] and set(DM["targets"]) - {"linear", "striped"} else "ok")),
       "lvmthin(7), lvmsnapshot")
    bp("구성", "swap 위치", "ES data 와 다른 디스크 (또는 swap 없음)", ", ".join(swap_hit) + " (data 디스크)" if swap_hit else ("없음" if not swap_on else "다른 디스크"),
       "caution" if swap_hit else "ok", "[Elastic 공식] Disable swapping")
    if repos:
        bp("구성", "snapshot 저장소(path.repo) 위치", "ES data 와 다른 장비", ", ".join(sorted(set(repos))) + (" (data 디스크와 같음)" if same_repo else ""),
           "caution" if same_repo else "ok", "[Elastic 공식] Snapshot and restore")
    if HW["smart"]:
        bp("하드웨어", "SMART", "이상 없음", ", ".join("{} {}".format(x["dev"], x["health"] or "-") for x in HW["smart"][:8]),
           sev_max(*[x["sev"] for x in HW["smart"]]), "smartctl")
    for line in rd(S, "df").splitlines()[1:]:
        p_ = line.split()
        if len(p_) >= 6 and any(pm["mount"] and p_[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
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
    CL = analyze_cluster(cluster_dir, add, th, kind)
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
                    "이 노드에 샤드가 몰려 있음" + (", 디스크 부하와 함께 나타남" if loaded else ""),
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
                ev_ = "디스크 busy 70% 이상 {}개 노드 ({}) / 전체 {}개 · 이 노드 지연 {}".format(
                        len(high), ", ".join(high[:6]), len(busy), SEV_LABEL.get(lat_sev, lat_sev))
                if kind == "vmware":
                    add("warn", "클러스터", "VMware 관리자", "여러 노드가 동시에 디스크를 많이 쓰고 있음. 공용 스토리지 의심", ev_,
                        "노드 하나가 아니라 여러 노드가 같은 시간에 느려졌다면, 각 VM의 문제가 아니라 그 VM들이 공유하는 "
                        + vs("vSAN 데이터스토어", "데이터스토어·스토리지 어레이", "데이터스토어(vSAN 또는 SAN·NFS)")
                        + "나 호스트 쪽 원인일 가능성이 큽니다. ES 노드가 같은 스토리지를 쓰면 서로의 I/O가 같은 자원을 두고 경쟁합니다.",
                        "VMware 관리자에게 해당 시각의 " + vs(
                            "vSAN 클러스터 단위 지표(디스크 그룹 지연, 캐시 사용률, resync, 네트워크)를",
                            "데이터스토어·어레이 단위 지표(볼륨 응답시간, 컨트롤러 부하, 경로 상태)를",
                            "데이터스토어 단위 지표(vSAN이면 디스크 그룹 지연·resync, SAN·NFS면 어레이 볼륨 응답시간)를")
                        + " 요청하세요. ES 노드 VM들이 같은 호스트·데이터스토어에 몰려 있는지도 함께 확인합니다.",
                        "노드 간 동시성 비교 (_nodes/stats fs.io_stats)")
                elif kind == "baremetal" and attach == "san":
                    add("warn", "클러스터", "스토리지 관리자", "여러 노드가 동시에 디스크를 많이 쓰고 있음. 공용 스토리지 어레이 의심", ev_,
                        "노드마다 디스크가 따로라면 동시에 느려질 이유가 적습니다. ES 노드들이 같은 어레이를 쓰고 있다면 어레이 컨트롤러나 공용 SAN 경로가 원인일 가능성이 큽니다.",
                        "스토리지 관리자에게 같은 시각의 어레이 컨트롤러 부하, 볼륨별 응답시간, 스위치 포트 사용률을 요청하세요.",
                        "노드 간 동시성 비교 (_nodes/stats fs.io_stats)")
                elif kind == "baremetal":
                    add("warn", "클러스터", "원인 분리 필요", "여러 노드가 동시에 디스크를 많이 쓰고 있음. 인입량 급증 의심", ev_,
                        "로컬 디스크는 노드끼리 공유하지 않으므로, 여러 노드가 같은 시간에 바쁘다면 스토리지보다 인입량이나 클러스터 작업(복구, 대량 merge, 스냅샷)이 원인일 가능성이 큽니다.",
                        "같은 시각의 인입량, 샤드 복구·이동, force merge·스냅샷 작업을 확인하세요. 평소 부하라면 노드나 디스크 증설이 필요한 시점입니다.",
                        "노드 간 동시성 비교 (_nodes/stats fs.io_stats)")
                else:
                    add("warn", "클러스터", OUT, "여러 노드가 동시에 디스크를 많이 쓰고 있음. 공용 스토리지 의심", ev_,
                        "노드 하나가 아니라 여러 노드가 같은 시간에 느려졌다면 그 노드들이 공유하는 스토리지 백엔드나 호스트 쪽 원인일 가능성이 큽니다.",
                        "{}에게 같은 시각의 스토리지 백엔드 지표와 노드 VM 배치를 확인 요청하세요.".format(OUT),
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
        ("메모리·캐시", mem_sev), ("설정", cfg_sev), (RES_DIM, vm_sev), ("네트워크(보조)", net_sev), ("클러스터", cl_sev),
    ]
    disk_rt = sev_max(lat_sev, sat_sev, err_sev)          # 디스크 자체의 측정 결과
    runtime = sev_max(disk_rt, es_sev)
    latent = sev_max(mem_sev, cfg_sev, vm_sev)
    n_act = sum(1 for f in F if SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"])
    # "디스크는 정상" 이라고 말하려면 실제로 재서 정상이어야 한다.
    # lat_sev=="na" 는 I/O가 적어 못 잰 경우이므로 정상 판정 근거가 될 수 없다.
    disk_clean = lat_sev == "ok" and not low_load
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
        # 비교는 같은 data tier 안에서만. hot 노드를 warm/cold 노드와 섞으면 중앙값이 왜곡된다
        my_row = next((r for r in CL["data_rows"] if r["name"] == me_name), None)
        def _tier(r):
            for t in ("data_hot", "data_warm", "data_cold", "data_frozen", "data_content"):
                if t in (r["roles"] or ""):
                    return t
            return "data"
        peers = [r for r in CL["data_rows"] if my_row is None or _tier(r) == _tier(my_row)]
        busy = [r["busy"] for r in peers if r["busy"] is not None]
        if len(busy) >= 3:
            med = pctl(busy, 0.5)
            me = (my_row or {}).get("busy")
            if me is not None and med:
                many_high = sum(1 for b in busy if b >= 70) >= max(2, len(busy) // 2)
                if me > med * 1.5:
                    rel = "이 노드만 다른 노드보다 뚜렷하게 높습니다"
                elif many_high:
                    rel = "이 노드만의 문제가 아니라 여러 노드가 동시에 높습니다. 공용 스토리지를 함께 보세요"
                else:
                    rel = "다른 노드들과 비슷한 수준입니다"
                verdict = (verdict[0], verdict[1],
                           verdict[2] + " 클러스터 비교로는 디스크 사용 시간이 {} (이 노드 {:.0f}% / 중앙값 {:.0f}%).".format(rel, me, med))


    # 우선 조치: ① 병목 위치(누가 움직일지 결정) → ② 실제로 손댈 수 있는 항목을 심각도순
    top = [f for f in F if f.title.startswith("병목 위치") and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"]][:1]
    # 클러스터 교차 판정은 "누가 움직여야 하는가"를 바꾸므로 개별 설정 항목보다 앞에 둔다
    top += [f for f in F if f.dim == "클러스터" and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["warn"] and f not in top][:2 - len(top)]
    act_owner = (OUT, "서버 담당자", "ES 설정")
    top += sorted([f for f in F if f.owner in act_owner and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"] and f not in top],
                  key=lambda f: -SEV_ORDER.get(f.sev, 0))[:3 - len(top)]

    # 수집 오버헤드
    ov = rd(base, "self_overhead").split()
    def tsec(x):
        m = re.match(r'(\d+)m([\d.]+)s', x)
        return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0
    cpu_s = sum(tsec(x) for x in ov)
    # 이 진단이 서버에 실제로 준 부하. 리포트에 그대로 고지한다
    eslog_b = num(meta.get("read_eslog_bytes"), 0) or 0
    sar_b = num(meta.get("read_sar_bytes"), 0) or 0
    out_kb = num(meta.get("output_kb"), 0) or 0
    overhead = {"cpu_s": cpu_s, "dur": dur, "pct_core": (100.0 * cpu_s / dur) if dur else None,
                "out_kb": out_kb, "samples": meta.get("samples"),
                "read_eslog_mb": eslog_b / 1048576.0, "read_sar_mb": sar_b / 1048576.0,
                "read_total_mb": (eslog_b + sar_b) / 1048576.0,
                "write_mb": out_kb / 1024.0,
                "es_calls": int(num(meta.get("es_api_calls"), 0) or 0),
                "es_bytes_mb": (num(meta.get("es_api_bytes"), 0) or 0) / 1048576.0,
                "maps_ms": num(meta.get("maps_read_ms")),
                "skipped": (meta.get("skipped") or "").strip(),
                "light": meta.get("light_mode") == "1",
                "out_on_data_fs": meta.get("out_on_data_fs") == "1"}

    return {
        "meta": meta, "storage": storage, "th": th, "es_version": es_version, "is_vmware": is_vmware,
        "platform": kind, "plat": plat, "attach": attach, "devcls": devcls, "storage_auto": storage_auto,
        "media_note": media_note, "unsure": unsure, "HW": HW, "vmbk": VMBK, "raid_absent": raid_absent,
        "PROC": PROC, "DM": DM, "DEVERR": DEVERR, "out_owner": OUT, "res_dim": RES_DIM, "sto": sto, "src_lat": src_lat,
        "os": kv(rd(S, "os-release")).get("PRETTY_NAME", "").strip('"'), "kernel": (rd(S, "uname").split() + ["", "", ""])[2],
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
ul.note{margin:12px 0 0;padding-left:20px}ul.note li{margin:5px 0}
.kpi .v{word-break:keep-all}
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

OWNER_ORDER = ["원인 분리 필요", "서버 담당자", "VMware 관리자", "가상화·클라우드 관리자", "하드웨어 담당자", "스토리지 관리자", "ES 설정", "참고"]
OWNER_DESC = {
    "원인 분리 필요": "측정값 자체가 나쁜 항목입니다. 원인이 어디에 있는지 판정 근거를 함께 적었습니다.",
    "서버 담당자": "OS에서 바꿀 수 있는 항목입니다. 모두 운영 변경이므로 담당자 검토 후 적용하세요. 이 도구는 아무것도 바꾸지 않았습니다.",
    "VMware 관리자": "VM 바깥 설정이라 Guest에서는 바꿀 수 없습니다. 근거와 함께 요청하세요.",
    "가상화·클라우드 관리자": "VM 바깥(하이퍼바이저, 스토리지 백엔드, 클라우드 볼륨) 설정이라 OS에서는 바꿀 수 없습니다. 근거와 함께 요청하세요.",
    "하드웨어 담당자": "디스크, RAID 컨트롤러, BIOS·펌웨어 쪽 항목입니다. OS에서는 상태를 일부만 볼 수 있어 하드웨어 관리 도구(BMC, 컨트롤러 유틸리티)로 확인이 필요합니다.",
    "스토리지 관리자": "외부 스토리지 어레이와 SAN 경로 쪽 항목이라 서버에서는 바꿀 수 없습니다. 측정 시각과 근거를 함께 전달하세요.",
    "ES 설정": "Elasticsearch 설정·운영으로 풀어야 하는 항목입니다.",
    "참고": "정상 확인 또는 해석에 필요한 참고 정보입니다.",
}
COLOR = {"ok": "#1d7a4b", "info": "#2d5f9e", "caution": "#946300", "warn": "#bf4a10", "crit": "#b3261e", "na": "#8b93a1"}

BLIND_VMWARE = [
    ("vSAN 스토리지 정책 (RAID·FTT·stripe·IOPS 제한)", "OSA에서 RAID-5/6은 쓰기마다 읽기-수정-쓰기가 생겨 RAID-1보다 쓰기 지연이 큽니다. 정책의 IOPS 제한은 그 이상을 막습니다.", "VM 스토리지 정책 확인"),
    ("ES 복제본 × vSAN 복제의 쓰기 증폭", "ES 복제본 1 + vSAN FTT=1(RAID-1)이면 문서 하나가 물리적으로 4벌 기록됩니다. 가용성은 ES에서도 보장되므로 용량·쓰기 부하 설계 때 함께 고려해야 합니다.", "스토리지 정책 + ES 인덱스 replicas 비교"),
    ("VM 스냅샷 존재", "스냅샷이 남아 있으면 쓰기가 delta 파일로 가서 성능이 떨어집니다.", "vCenter 스냅샷 관리자"),
    ("같은 호스트의 ES 노드 배치", "ES primary와 replica가 같은 ESXi 호스트에 있으면 호스트 장애 한 번에 둘 다 잃습니다.", "DRS anti-affinity 규칙 + ES shard allocation awareness"),
    ("vSAN 네트워크·resync·캐시 사용률", "Guest에서는 쓰기 지연으로만 간접 관측됩니다.", "vSAN 성능 서비스, esxtop DAVG/KAVG"),
    ("실제 물리 디스크 상태·지연", "Guest의 SMART 조회는 가상 디스크라 의미가 없습니다.", "vSAN Skyline Health"),
    ("vNUMA 경계, CPU hot-add", "CPU hot-add를 켜면 vNUMA가 꺼져 메모리 접근이 느려질 수 있습니다.", "VM 고급 설정"),
]
BLIND_VMWARE_DS = [
    ("데이터스토어 종류와 뒤의 스토리지", "Guest 에서는 데이터스토어가 vSAN 인지, SAN(VMFS)·NFS 인지, 그 뒤 어레이가 어떤 매체인지 알 수 없습니다. 판정 기준과 조치 방향이 여기서 갈립니다.", "VM 설정의 데이터스토어, 스토리지 어레이 관리 화면"),
    ("스토리지 어레이 쪽 응답시간과 부하", "Guest 지연에는 어레이 처리 시간과 SAN·NFS 경로 지연이 모두 들어 있습니다.", "esxtop DAVG, 어레이 볼륨 응답시간"),
    ("Storage I/O Control·디스크 IOPS 한도", "한도에 닿으면 Guest 에서는 IOPS 가 평평하게 막히는 모양으로만 보입니다.", "VM 디스크 설정, 데이터스토어 SIOC 설정"),
    ("VM 스냅샷 존재", "스냅샷이 남아 있으면 쓰기가 delta 파일로 가서 성능이 떨어집니다.", "vCenter 스냅샷 관리자"),
    ("같은 호스트·데이터스토어의 ES 노드 배치", "ES primary와 replica가 같은 ESXi 호스트나 같은 데이터스토어에 있으면 장애 한 번에 둘 다 잃습니다.", "DRS anti-affinity 규칙 + ES shard allocation awareness"),
    ("경로 정책과 경로 상태", "ESXi 의 multipath 정책(Round Robin 등)과 경로 장애는 Guest 에서 보이지 않습니다.", "ESXi 스토리지 어댑터·경로 화면"),
]
BLIND_BAREMETAL = [
    ("RAID 컨트롤러 캐시 정책·배터리", "write-back 캐시가 배터리 이상이나 학습 주기로 write-through로 바뀌면 fsync마다 디스크까지 가야 해서 쓰기 지연이 크게 늘어납니다. OS에는 보이지 않습니다.", "컨트롤러 유틸리티(storcli·perccli·ssacli), BMC(iDRAC·iLO·XCC)"),
    ("RAID 레벨과 재구성·점검 작업", "RAID 5/6은 쓰기마다 읽기-수정-쓰기가 생깁니다. 재구성, 일관성 검사, patrol read 중에는 성능이 떨어집니다.", "컨트롤러 이벤트 로그와 작업 일정"),
    ("RAID 뒤 개별 디스크 상태", "RAID 논리 디스크 하나로만 보여 구성원 디스크의 SMART와 지연은 OS에서 볼 수 없습니다.", "컨트롤러 유틸리티의 물리 디스크 상태, predictive failure"),
    ("BIOS 전원 정책", "BIOS가 전원 절약 모드면 OS의 governor 설정과 별개로 CPU와 PCIe가 절전 상태로 들어갑니다.", "BIOS System Profile (Performance 권장 여부는 벤더 가이드 확인)"),
    ("ES 복제본과 RAID 이중화", "ES replica가 이미 이중화를 하므로 RAID 1/10과 겹치면 같은 문서를 여러 벌 씁니다. Elastic은 성능을 위해 RAID 0 stripe를 예로 듭니다.", "인덱스 replicas 설정과 RAID 레벨 비교"),
]
BLIND_SAN = [
    ("어레이 쪽 응답시간과 컨트롤러 부하", "서버에서 본 지연에는 SAN 경로와 어레이 처리 시간이 모두 들어 있어 어느 쪽인지 서버에서 가를 수 없습니다.", "어레이 관리 화면의 볼륨·호스트 포트별 응답시간"),
    ("같은 어레이를 쓰는 다른 서버", "다른 업무 서버의 배치 작업이 어레이를 차지하면 ES 쪽 지연이 이유 없이 오릅니다.", "어레이의 호스트별 IOPS·처리량"),
    ("볼륨 QoS 한도", "볼륨에 IOPS·처리량 한도가 걸려 있으면 서버에서는 수치가 평평하게 막히는 것으로만 보입니다.", "어레이 QoS 정책"),
    ("SAN 스위치 포트 오류·혼잡", "CRC 오류, 버퍼 부족은 서버에서 재시도와 지연으로만 나타납니다.", "스위치 포트 오류 카운터"),
    ("어레이 복제·스냅샷 일정", "동기 복제는 쓰기 지연을 늘리고, 스냅샷 작업 시간대에는 성능이 떨어집니다.", "어레이 복제·스냅샷 정책"),
]
BLIND_VM = [
    ("호스트 스토리지 백엔드와 캐시", "가상 디스크 뒤의 실제 스토리지 종류와 캐시 설정은 VM에서 보이지 않습니다.", "하이퍼바이저 관리 화면의 디스크 지연·캐시 모드"),
    ("볼륨 IOPS·처리량 한도", "클라우드 볼륨과 일부 하이퍼바이저는 디스크별·VM별 한도를 둡니다. 한도에 닿으면 수치가 평평하게 막힙니다.", "클라우드 콘솔의 볼륨 성능 지표(한도 도달 여부)"),
    ("같은 호스트의 다른 VM", "다른 VM의 I/O와 CPU 경합은 steal과 지연으로만 간접 관측됩니다.", "호스트 단위 성능 지표"),
    ("ES 노드 배치", "ES primary와 replica가 같은 물리 호스트(또는 같은 가용 영역)에 있으면 한 번의 장애로 둘 다 잃습니다.", "anti-affinity 규칙 + ES shard allocation awareness"),
]

def platform_label(R):
    p = R.get("plat") or {}
    kind = R.get("platform")
    if kind == "vmware":
        base = "VMware Guest"
    elif kind == "baremetal":
        base = "bare-metal" + {"san": " · SAN", "cloud": " · 클라우드 볼륨"}.get(R.get("attach"), "")
    elif kind == "vm":
        base = "{} Guest".format(HV_LABEL.get(p.get("hv"), p.get("hv") or "가상 머신"))
    else:
        base = "플랫폼 미확정"
    if p.get("cloud"):
        base += " · " + p["cloud"]
    if p.get("container"):
        base += " · 컨테이너 안에서 실행({})".format(p["container"])
    return base

def storage_basis(R):
    st = R["storage"]
    lab = {"allflash": "All-Flash vSAN", "hybrid": "Hybrid vSAN"}.get(st, STORAGE_LABEL.get(st, st))
    how = "자동 판정" if R.get("storage_auto") else "-s 지정"
    if R.get("platform") == "vmware" and R.get("storage_auto"):
        # 데이터스토어가 vSAN 인지 SAN·NFS 인지, vSAN 이면 All-Flash 인지 Hybrid 인지 Guest 에서는 알 수 없다
        how = ("기본값. Guest 에서는 데이터스토어 종류를 알 수 없음. vSAN 이면 --storage allflash 또는 hybrid, "
               "SAN·NFS 데이터스토어면 --storage vmfs 로 다시 분석하면 안내 문구가 그 환경에 맞춰짐")
    if R.get("platform") == "baremetal" and R.get("attach") == "san" and R.get("storage_auto"):
        how = "자동 판정, SAN 은 매체를 알 수 없어 SSD 기준. 어레이가 HDD면 -s hdd"
    elif R.get("unsure") and R.get("storage_auto"):
        how = "자동 판정, RAID 논리 디스크라 추정. 다르면 -s 로 지정"
    return "{} ({})".format(lab, how)

def render(R, out_path):
    A, th = R["A"], R["th"]
    kind = R.get("platform")
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
    h.append('<title>ES Disk I/O 진단 · {}</title><style>{}</style></head><body><div class="wrap">'.format(E(meta.get("host")), CSS))
    h.append('<h1>Elasticsearch 디스크 I/O 진단 리포트</h1>')
    h.append('<div class="meta">{} · {} · kernel {} · {} {} · RAM {:.0f}GB · ES {} · {} · 측정 {} ~ {} ({:.0f}초, {}초 간격)</div>'.format(
        E(meta.get("host")), E(R["os"]), E(R["kernel"]), "CPU" if kind == "baremetal" else "vCPU", R["ncpu"], R["mem_gb"], E(R["es_version"]),
        E(platform_label(R)),
        E(meta.get("start_wall", "")), E(meta.get("end_wall", "")[11:19]), R["dur"], E(meta.get("interval"))))

    h.append('<div class="verdict {}"><b>{}</b><span>{}</span></div>'.format(v[0], E(v[1]), E(v[2])))
    if (R.get("plat") or {}).get("container"):
        h.append('<p class="note" style="color:#bf4a10"><b>컨테이너 안에서 실행한 결과입니다.</b> 디스크 지표에 호스트의 다른 컨테이너 I/O가 섞여 있을 수 있습니다. 호스트에서 다시 실행하는 것을 권장합니다.</p>')
    h.append('<div class="dims">')
    for name, s in R["dims"]:
        h.append('<div class="dim"><div class="n">{}</div><div class="s s-{}">{}</div><div class="bar b-{}"></div></div>'.format(E(name), s, SEV_LABEL.get(s, s), s))
    h.append('</div>')
    h.append('<p class="note">판정 기준: {} · 응답시간 주의 {:g}ms / 경고 {:g}ms / 위험 {:g}ms · 조치가 필요한 항목 {}건 · 측정 대상 디스크 {}{}{}</p>'.format(
        E(storage_basis(R)), th["caution"], th["warn"], th["crit"], R["n_act"], E(", ".join(R["phys"]) or "-"),
        " (ES data 경로를 특정하지 못해 전체 디스크 기준)" if R["dev_guess"] else "",
        " · " + E(R["media_note"]) if R.get("media_note") else ""))

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
    shape = "I/O 모양: 요청 1건 평균 읽기 {} · 쓰기 {} · 병합 비율 읽기 {} · 쓰기 {}".format(
        fmt(A.get("r_kb"), 0, "KB"), fmt(A.get("w_kb"), 0, "KB"), fmt(A.get("r_merge_pct"), 0, "%"), fmt(A.get("w_merge_pct"), 0, "%"))
    if A.get("flush_ps") is not None:
        shape += " · flush 초당 {} (평균 {})".format(fmt(A["flush_ps"], 1), fmt(A.get("flush_ms"), 2, "ms"))
    h.append('<p class="note">{}. 요청이 작고(수 KB) 병합이 적으면 무작위 I/O, 크고 병합이 많으면 순차 I/O 입니다. '
             'flush 는 fsync 가 장치 캐시를 비우라고 보내는 요청이라 translog 비용과 직결됩니다.</p>'.format(E(shape)))

    # 차트
    h.append('<h2>시간대별 흐름</h2><p class="lead">응답시간이 튄 순간에 IOPS·대기 I/O·PSI가 함께 올랐는지 보면 원인을 가를 수 있습니다. 함께 오르면 부하 때문이고, 부하는 그대로인데 응답시간만 오르면 {} 문제일 가능성이 큽니다. 차트 위에 마우스를 올리면 값이 보입니다.</p>'.format(
        {"vmware": "VM 바깥", "baremetal": "스토리지 어레이·SAN 경로" if R.get("attach") == "san" else "디스크·컨트롤러 상태"}.get(kind, "VM 바깥")))
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
        h.append('<h2>최대 능력 대비 사용률</h2><p class="lead">es_disk_bench.sh로 잰 최대 능력{}과 이번 측정의 p95를 비교했습니다. {}</p>'.format(
            "(" + E(os.path.basename(meta.get("bench_src", ""))) + ")" if meta.get("bench_src") else "",
            ("vSAN 캐시 계층이나 스토리지 캐시에" if R.get("vmbk") != "ds" else "스토리지 어레이 캐시에") + " 벤치 파일이 들어가면 최대 능력이 실제보다 높게 나오므로 사용률은 낙관적인 값입니다." if kind == "vmware" else
            "RAID 컨트롤러·스토리지 캐시에 벤치 파일이 들어가면 최대 능력이 실제보다 높게 나오므로 사용률은 낙관적인 값입니다."))
        h.append('<table><tr><th>항목</th><th>관측 p95</th><th>측정 최대</th><th>사용률</th></tr>')
        for l, o, c, p, u in R["headroom"]:
            s = "warn" if p >= 80 else "caution" if p >= 60 else "ok"
            h.append('<tr><td>{}</td><td class="n">{}</td><td class="n">{}</td><td class="n s-{}"><b>{:.0f}%</b></td></tr>'.format(E(l), fmt(o, 1, " " + u), fmt(c, 1, " " + u), s, p))
        h.append('</table>')
        sync = (R["bench"].get("fsync_4k") or {})
        if sync.get("sync_p99") or sync.get("w_p99"):
            h.append('<p class="note">동기 쓰기(fdatasync) p99 {}. translog를 요청마다 fsync하는 기본 설정(durability: request)에서 인덱싱 요청 한 번이 최소로 기다리는 시간입니다.</p>'.format(
                fmt(sync.get("sync_p99") or sync.get("w_p99"), 2, " ms")))
        elif sync.get("sync_avg"):
            h.append('<p class="note">동기 쓰기(dd oflag=dsync, 4KiB) 평균 {}. translog를 요청마다 fsync하는 기본 설정에서 인덱싱 요청 한 번이 최소로 기다리는 시간에 해당합니다. '
                     'fio 가 없어 dd 로 쟀기 때문에 무작위 I/O 능력은 측정하지 않았습니다.</p>'.format(fmt(sync["sync_avg"], 2, " ms")))
    else:
        h.append('<h2>한계 추정</h2><p class="lead">부하 테스트 없이 계산할 수 있는 것은 "{} 큐 기준 상한"까지입니다. 동시에 처리할 수 있는 요청 수(queue_depth 합계)를 1건 평균 처리 시간으로 나눈 값입니다. '
                 '실제로는 부하가 늘면 처리 시간도 늘고 {} 먼저 막히므로, 이 값은 <b>넘을 수 없는 상한</b>이지 도달 가능한 값이 아닙니다.</p>'.format(
                     "장치" if kind == "baremetal" else "가상 디스크",
                     {"vmware": "vSAN 쪽이" if R.get("vmbk") == "vsan" else "스토리지 쪽이", "baremetal": "디스크 자체가"}.get(kind, "스토리지 백엔드가")))
        if R.get("q_ceiling"):
            use = 100.0 * (A["iops_p95"] or 0) / R["q_ceiling"]
            h.append('<table><tr><th>항목</th><th>값</th></tr><tr><td>{} 큐 기준 이론 상한</td><td class="n">{}</td></tr>'
                     '<tr><td>이번 측정 p95</td><td class="n">{}</td></tr><tr><td>상한 대비 사용</td><td class="n"><b>{:.0f}%</b></td></tr></table>'.format(
                         "OS" if kind == "baremetal" else "Guest", fmt(R["q_ceiling"], 0, " IOPS"), fmt(A["iops_p95"], 0, " IOPS"), use))
        else:
            h.append('<p class="note">측정 구간의 I/O가 적거나 queue_depth를 읽지 못해 계산하지 않았습니다.</p>')
        h.append('<p class="note">실제 한계는 서비스 투입 전이나 점검 시간에 <code>es_disk_bench.sh</code>로 재고 <code>--bench</code> 옵션으로 넣으면 "최대 능력 대비 사용률"이 이 자리에 표시됩니다.</p>')

    # 클러스터 관점
    CL = R.get("CL")
    if CL:
        h.append('<h2>클러스터 관점: 이 노드만인가, 전체인가</h2>')
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

    # 디스크를 쓴 프로세스
    PROC = R.get("PROC") or []
    if PROC:
        h.append('<h2>디스크를 쓴 프로세스 (측정 구간)</h2><p class="lead">측정 시작과 끝의 /proc/&lt;pid&gt;/io 차이입니다. '
                 'ES 가 아닌 프로세스가 위에 있으면 원인이 ES 밖에 있을 수 있습니다. 프로세스 단위 값이라 어느 디스크를 썼는지는 구분하지 않습니다.</p>')
        h.append('<table><tr><th>프로세스</th><th>pid</th><th>읽기</th><th>쓰기</th></tr>')
        for x in PROC[:10]:
            h.append('<tr{}><td>{}{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                ' style="background:#eef3fa"' if x["es"] else "", E(x["comm"]), " <span class='note'>(Elasticsearch)</span>" if x["es"] else "",
                E(x["pid"]), fmt(x["r_mb"], 1, " MB"), fmt(x["w_mb"], 1, " MB")))
        h.append('</table>')

    # 디바이스 상세
    h.append('<h2>디바이스별 상세</h2>')
    h.append('<div class="scroll"><table><tr><th>ES data 경로</th><th>마운트</th><th>FS · 옵션</th><th>논리 장치</th><th>물리 디스크</th></tr>')
    for pm in R["path_map"]:
        m = pm["mount"] or {}
        h.append('<tr><td>{}</td><td>{}</td><td>{} · <span class="note">{}</span></td><td>{}</td><td>{}</td></tr>'.format(
            E(pm["path"]), E(m.get("mnt", "-")), E(m.get("fs", "-")), E(m.get("opts", "-")), E(pm["kname"] or "-"), E(", ".join(pm["phys"]) or "-")))
    h.append('</table></div>')
    topo = R["topo"]
    def deverr_txt(d):
        e = (R.get("DEVERR") or {}).get(d) or {}
        parts = []
        if e.get("state"):
            parts.append(e["state"])
        if e.get("ioerr") is not None:
            parts.append("ioerr {}".format(e["ioerr"]))
        if e.get("iotmo") is not None:
            parts.append("timeout {}".format(e["iotmo"]))
        if e.get("aer"):
            a = e["aer"]
            parts.append("AER {}/{}/{}".format(a.get("fatal"), a.get("nonfatal"), a.get("cor")))
        return " · ".join(parts) or "-"
    h.append('<div class="scroll"><table><tr><th>디스크</th><th>컨트롤러</th><th>queue_depth</th><th>scheduler</th><th>readahead</th><th>timeout</th><th>섹터(논리/물리)</th>'
             '<th>읽기 p95</th><th>쓰기 p95</th><th>IOPS p95</th><th>aqu p95</th><th>inflight p95</th><th>%util p95</th><th>상태·오류</th></tr>')
    for d in R["phys"] + [x for x in R["logical"] if x not in R["phys"]]:
        a = topo.attr.get(topo.whole(d), {})
        st = R["dev_stats"].get(d) or R["log_stats"].get(d) or {}
        host = topo.scsihost.get(d, "")
        h.append('<tr><td><b>{}</b>{}</td><td>{}</td><td class="n">{}</td><td>{}</td><td class="n">{}KB</td><td class="n">{}s</td><td>{}/{}</td>'
                 '<td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                     E(d), " <span class='note'>(" + E(a.get("dm/name")) + ")</span>" if a.get("dm/name") else "",
                     E((host + " " + topo.hostdrv.get(host, "")).strip() or "-"), E(a.get("device/queue_depth", "-")),
                     E(a.get("queue/scheduler", "-")), E(a.get("queue/read_ahead_kb", "-")), E(a.get("device/timeout", "-")),
                     E(a.get("queue/logical_block_size", "-")), E(a.get("queue/physical_block_size", "-")),
                     fmt(st.get("r_await_p95"), 2, "ms"), fmt(st.get("w_await_p95"), 2, "ms"), fmt(st.get("iops_p95"), 0),
                     fmt(st.get("aqu_p95"), 2), fmt(st.get("inflight_p95"), 1), fmt(st.get("util_p95"), 0, "%")) .replace("</tr>", "<td class='note'>{}</td></tr>".format(E(deverr_txt(d)))))
    h.append('</table></div>')
    dc = R.get("devcls") or {}
    if dc:
        h.append('<div class="scroll"><table><tr><th>디스크</th><th>판정한 종류</th><th>연결</th><th>근거</th><th>제조사 · 모델</th><th>쓰기 캐시(커널 보고)</th></tr>')
        ATT = {"local": "로컬", "san": "SAN", "nvmeof": "NVMe-oF", "cloud": "클라우드 볼륨", "virtual": "가상 디스크"}
        for d in sorted(dc):
            c = dc[d]
            virt_disk = kind in ("vmware", "vm", "unknown") and c["attach"] == "local"
            h.append('<tr><td><b>{}</b></td><td>{}</td><td>{}</td><td class="note">{}</td><td>{}</td><td>{}</td></tr>'.format(
                E(d), "-" if virt_disk else E(STORAGE_LABEL.get(c["media"], c["media"] or "-") + ("" if c["sure"] else " (추정)")),
                E("가상 디스크" if virt_disk else ATT.get(c["attach"], c["attach"])), E(c["why"] or "-"),
                E((c["vendor"] + " " + c["model"]).strip() or "-"), E(topo.attr.get(d, {}).get("queue/write_cache", "-"))))
        h.append('</table></div>')
        if kind == "baremetal":
            h.append('<p class="note">쓰기 캐시 값은 커널이 장치에서 받은 보고입니다. RAID 컨트롤러는 배터리로 보호되는 캐시를 "write through"로 보고하기도 해서, '
                     '이 값만으로 컨트롤러 캐시 상태를 판단하지 않았습니다.</p>')
    h.append('<p class="note">%util은 참고용입니다. SSD·NVMe·RAID·공유 스토리지처럼 요청을 병렬로 처리하는 장치는 %util이 100%여도 여유가 있을 수 있어 판정에 쓰지 않았습니다. '
             'aqu p95는 블록 계층에서 대기 중인 요청까지 포함한 시간 평균이라 queue_depth를 넘을 수 있고, inflight p95는 장치에 넘겨져 처리 중인 I/O 수라 queue_depth와 직접 비교됩니다. '
             '병목 위치 판정은 두 값을 함께 봅니다.</p>')

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
    h.append('<h2>이 진단이 서버에 준 부하</h2>')
    h.append('<p class="lead">프로덕션에서 돌리는 도구이므로 이번 실행이 실제로 쓴 자원을 그대로 싣습니다. '
             '아래는 이 노드에서 측정된 값이며, 설정 변경은 한 건도 하지 않았습니다.</p>')
    h.append('<div class="kpis">')
    load_kp = [
        ("CPU 사용", fmt(ov["cpu_s"], 2, "초"), "측정 {:.0f}초 동안 · CPU 1개의 {}".format(ov["dur"] or 0, fmt(ov["pct_core"], 2, "%"))),
        ("디스크 읽기", fmt(ov["read_total_mb"], 1, " MB"), "ES 로그 {} · sar {}".format(
            fmt(ov["read_eslog_mb"], 1, "MB"), fmt(ov["read_sar_mb"], 1, "MB"))),
        ("디스크 쓰기", fmt(ov["write_mb"], 2, " MB"), "결과 파일 (측정 대상 외 경로 권장)"),
        ("ES 조회", "{}회".format(ov["es_calls"]) if ov["es_calls"] else "안 함",
         "전부 GET · 응답 합계 {}".format(fmt(ov["es_bytes_mb"], 2, "MB")) if ov["es_calls"] else "--no-es 또는 접속 실패"),
    ]
    for l, val, d in load_kp:
        h.append('<div class="kpi"><div class="l">{}</div><div class="v">{}</div><div class="d">{}</div></div>'.format(E(l), E(val), E(d)))
    h.append('</div>')
    notes = []
    notes.append('샘플링 구간에서 읽은 <code>/proc</code>·<code>/sys</code> 는 커널이 메모리에서 만들어 주는 값이라 디스크 I/O가 발생하지 않습니다. '
                 '위 디스크 읽기는 ES 서버 로그와 sar 기록을 읽은 양입니다.')
    if ov["maps_ms"] is not None:
        notes.append('ES 프로세스의 매핑 목록을 1회 읽었고 {}가 걸렸습니다. 이 동안 해당 프로세스의 mmap_lock 을 read 모드로 잡으므로 '
                     'ES 의 segment 열기·닫기(mmap/munmap)와만 짧게 경합합니다.'.format(fmt(ov["maps_ms"], 0, "ms")))
    if ov["light"]:
        notes.append('<b>--light 모드로 실행</b>되어 디스크를 읽는 부가 수집(ES 로그·커널 로그·sar·매핑 목록)을 모두 생략했습니다. '
                     '부하는 최소지만 그만큼 판정 근거도 줄어듭니다.')
    elif ov["skipped"]:
        notes.append('생략한 수집: <code>{}</code>'.format(E(ov["skipped"])))
    if ov["out_on_data_fs"]:
        notes.append('<b style="color:#bf4a10">결과 저장 위치가 ES data 와 같은 파일시스템입니다.</b> '
                     '측정 대상 디스크에 쓰기를 더했으므로 이번 수치에는 그만큼의 오염이 섞여 있습니다. 다음 실행은 <code>-o</code> 로 다른 디스크를 지정하세요.')
    notes.append('부하를 더 줄이려면 <code>--light</code>(디스크 읽기 1MB 미만), <code>--no-eslog</code>(로그 읽기만 생략), '
                 '<code>--no-index-stats</code>(인덱스 수에 비례하는 ES 조회 2건 생략)를 쓸 수 있습니다.')
    h.append('<ul class="note">' + "".join("<li>{}</li>".format(n) for n in notes) + '</ul>')

    h.append('<h2>무엇을 어떻게 쟀고, 무엇은 못 보는가</h2>')
    h.append('<table><tr><th>지표</th><th>출처</th><th>알 수 있는 것</th><th>판정에 쓰는 방식</th></tr>')
    rows = [
        ("응답시간 r_await / w_await", "/proc/diskstats (iostat과 같은 원본, 직접 계산)", "ES가 실제로 겪는 I/O 1건당 지연", "p95로 판정. I/O 적은 구간 제외"),
        ("대기 I/O aqu-sz ÷ queue_depth", "/proc/diskstats, /sys/block/*/device/queue_depth",
         {"vmware": "지연이 VM 안(큐)에서 생기는지 밖에서 생기는지",
          "baremetal": "지연이 부하 포화 때문인지 장치 자체가 느린 것인지" if R.get("attach") != "san" else "지연이 서버 쪽 LUN 큐에서 생기는지 어레이 쪽에서 생기는지"
          }.get(kind, "지연이 VM 안(큐)에서 생기는지 밖에서 생기는지"), "병목 위치 분리"),
        ("PSI io some/full", "/proc/pressure/io (커널 4.20+, RHEL8은 psi=1 필요)", "작업이 I/O 때문에 멈춘 시간 비율", "포화 판정"),
        ("ES 스레드 D 상태", "/proc/<pid>/task/*/stat", "ES 스레드가 디스크 때문에 멈춘 순간", "포화 판정"),
        ("iowait · procs_blocked", "/proc/stat", "추세 참고 (CPU가 바쁘면 낮게 나와 단독 판정 불가)", "참고만"),
        ("ES major fault · 물리 읽기/쓰기", "/proc/<pid>/stat, /proc/<pid>/io", "page cache에 없어 디스크를 읽은 양", "캐시 부족형 병목 판정"),
        ("swap · dirty page", "/proc/vmstat, /proc/meminfo", "메모리 압박이 디스크로 번지는지", "메모리 판정"),
    ]
    if kind == "vmware":
        rows += [("CPU steal · balloon · 메모리 예약/한도", "/proc/stat, vmware-toolbox-cmd stat", "호스트 자원 경합과 VM 자원 설정", "VMware 자원 판정"),
                 ("커널 로그", "journalctl -k / dmesg (최근 7일)", "vSAN 순간 정지 흔적 (abort/reset, hung task)", "오류 판정")]
    elif kind == "baremetal":
        rows += [("장치 종류·연결 방식", "/sys/block/*/queue/rotational, SCSI host 드라이버, dm-multipath, /sys/class/nvme", "NVMe·SSD·HDD, RAID 논리 디스크, SAN 여부", "판정 기준 선택"),
                 ("NVMe 온도·PCIe 링크", "/sys/class/nvme/*/hwmon, */device/current_link_*", "온도 제한, 링크 속도·폭 저하", "하드웨어 판정"),
                 ("소프트웨어 RAID", "/proc/mdstat", "resync·recovery 진행, degraded", "하드웨어 판정"),
                 ("CPU governor", "/sys/devices/system/cpu/cpu0/cpufreq", "절전 정책이 I/O 처리를 늦추는지", "하드웨어 판정"),
                 ("SMART", "smartctl -H -A (bare-metal 에서 자동, RAID 뒤 디스크 제외)", "디스크 자체 건강 상태", "하드웨어 판정"),
                 ("RAID 컨트롤러", "storcli·perccli / ssacli / arcconf 조회(show) 명령, /sys/class/raid_devices",
                  "논리 디스크 상태, 쓰기 캐시 정책, 배터리, 구성 디스크 매체·오류, rebuild·patrol read", "하드웨어 판정, 매체 판정"),
                 ("커널 로그", "journalctl -k / dmesg (최근 7일)", "Medium Error, 컨트롤러·PCIe 오류, 경로 소실, abort/reset", "오류 판정")]
    else:
        rows += [("CPU steal", "/proc/stat", "호스트 CPU 경합", "가상화 자원 판정"),
                 ("커널 로그", "journalctl -k / dmesg (최근 7일)", "스토리지 백엔드 정지 흔적 (abort/reset, hung task)", "오류 판정")]
    rows += [
        ("ES node stats 변화량", "_nodes/_local/stats (조회 API 2회)", "인덱싱 스로틀, 거절, flush·검색 지연", "ES 영향 판정"),
        ("설정값", "/sys/block, sysctl, mount, udev, ES 설정", "readahead·스케줄러·타임아웃·limits 등", "설정 판정"),
        ("과거 이력", "sysstat sar 파일 (이미 기록된 것)", "측정 창 밖의 피크", "주의 알림"),
        ("ES 서버 로그", "/var/log/elasticsearch/*.log (최근 7일, 관련 줄만)", "ES가 직접 남긴 스로틀·watermark·flush 실패 기록", "ES 영향 판정"),
        ("mmap 사용량", "/proc/<pid>/maps 줄 수 vs vm.max_map_count", "segment 매핑 한도 여유", "설정 판정"),
        ("스토리지 인터럽트 분포", "/proc/interrupts 시작·종료 스냅샷", "I/O 완료 처리가 {} 한 개에 몰리는지".format("CPU" if kind == "baremetal" else "vCPU"), "설정 판정"),
        ("노드 간 비교", "_nodes/stats fs.io_stats (측정과 같은 창으로 2회)", "이 노드만인지 클러스터 전체인지", "클러스터 판정"),
        ("인덱스별 분포", "_nodes/_local/stats/indices?level=indices + _ilm/explain", "이 노드 디스크를 쓰는 인덱스와 ILM phase", "쓰기 집중 판정"),
        ("샤드 배분", "_cat/allocation", "샤드가 이 노드에 몰렸는지", "교차 판정"),
    ]
    for r in rows:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in r) + '</tr>')
    h.append('</table>')
    h.append('<p><b>Elastic System/Linux integration 으로는 볼 수 없는 것</b><br>상시 수집 지표와 이 도구가 겹치지 않는 부분입니다. '
             '상시 모니터링을 대체하려는 것이 아니라, 경보가 울린 뒤 원인을 좁힐 때 필요한 항목들입니다.</p>')
    h.append('<table><tr><th>항목</th><th>상시 수집 지표</th><th>이 도구</th><th>왜 필요한가</th></tr>')
    gaps = [
        ("응답시간 백분위", "10~30초 평균값만 저장", "p95 + I/O 적은 구간 제외", "평균은 짧은 지연 급등을 지워 버립니다"),
        ("병목 위치 판정", "없음", "대기 I/O ÷ queue_depth 로 " + {"vmware": "VM 안/밖 구분", "baremetal": "포화/장치 이상 구분"}.get(kind, "VM 안/밖 구분"),
         "{}에게 넘길지 서버에서 풀지가 갈립니다".format(R.get("out_owner", "VMware 관리자"))),
        ("PSI (I/O 압박)", "Linux integration에 pressure 지표 일부", "io some/full 을 측정 구간 전체로 계산", "iowait보다 정확한 포화 지표"),
        ("ES 스레드 D 상태", "없음", "ES 스레드만 골라 카운트", "ES가 실제로 디스크에 멈춰 있었는지"),
        ("블록 장치 설정 전수", "없음", "readahead·scheduler·timeout·iostats·wbt 등", "설정 문제는 지표로 안 보이고 설정을 봐야 압니다"),
        ("LVM·파티션 토폴로지", "없음", "dm → 물리 디스크 역추적, 정렬 확인", "ES 경로가 실제로 어느 디스크인지"),
        ("커널 로그 상관", "없음 (로그는 별도 수집)", "SCSI abort/reset·hung task·컨트롤러 오류 분류",
         "vSAN 순간 정지의 흔적" if kind == "vmware" else "장치·경로 장애의 흔적"),
    ]
    if kind == "vmware":
        gaps.append(("VMware 자원", "없음", "balloon·host swap·예약·limit", "메모리 회수는 디스크 문제로 위장해 나타납니다"))
    elif kind == "baremetal":
        gaps.append(("스트라이프 구성원별 비교", "디스크별 지표는 있으나 비교 판정 없음", "같은 묶음 안에서 한 디스크만 느린지", "불량 디스크 하나가 전체 묶음을 늦춥니다"))
    gaps += [
        ("스토리지 IRQ 편중", "없음", "측정 구간 IRQ 분포", "{} 한 개에 몰리면 IOPS가 거기서 막힙니다".format("CPU" if kind == "baremetal" else "vCPU")),
        ("mmap 여유", "없음", "현재 매핑 수 / max_map_count", "한도에 닿으면 인덱싱이 실패합니다"),
        ("인덱스별 쓰기 분포", "인덱스 지표는 있으나 노드 로컬 관점 아님", "이 노드 샤드의 인덱스별 delta + ILM phase", "디스크를 쓰는 주체를 인덱스까지 좁힘"),
        ("Best practice 대조", "없음", "공식 문서 기준 전수 대조표", "경보가 아니라 사전 예방"),
    ]
    for g in gaps:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in g) + '</tr>')
    h.append('</table>')
    h.append('<p class="note">반대로 상시 수집 지표가 더 나은 영역도 분명합니다. 장기 추세, 여러 노드 동시 시계열, 경보 자동화는 Elastic 쪽이 맞습니다. '
             '이 도구는 그 경보가 울린 순간에 한 번 깊게 파는 용도입니다. 상시 경보 기준은 GUARDLINE.md 4장에 정리했습니다.</p>')
    if kind == "vmware":
        h.append('<p><b>Guest OS에서 원리상 볼 수 없는 것</b><br>아래 항목은 판정에 넣지 않았고, 필요하면 VMware 관리자에게 확인해야 합니다.</p>')
    elif kind == "baremetal":
        h.append('<p><b>OS에서 원리상 볼 수 없는 것</b><br>아래 항목은 판정에 넣지 않았고, 필요하면 {}에게 확인해야 합니다.</p>'.format(E(R.get("out_owner"))))
    else:
        h.append('<p><b>Guest OS에서 원리상 볼 수 없는 것</b><br>아래 항목은 판정에 넣지 않았고, 필요하면 {}에게 확인해야 합니다.</p>'.format(E(R.get("out_owner"))))
    h.append('<table><tr><th>항목</th><th>왜 중요한가</th><th>관리자 확인 방법</th></tr>')
    if kind == "vmware":
        blind = {"vsan": BLIND_VMWARE, "ds": BLIND_VMWARE_DS}.get(R.get("vmbk"), [BLIND_VMWARE_DS[0]] + BLIND_VMWARE)
    elif kind == "baremetal" and R.get("attach") == "san":
        blind = BLIND_SAN
    elif kind == "baremetal":
        blind = BLIND_BAREMETAL
        if (R.get("HW") or {}).get("raid"):
            # 컨트롤러 도구로 캐시·RAID 레벨·구성 디스크 상태를 읽었으면 "볼 수 없는 것"에서 뺀다
            blind = [b for b in blind if not b[0].startswith("RAID")]
    else:
        blind = BLIND_VM
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
    h.append('<tr><td><code>플랫폼 판정 근거</code></td><td>{}</td></tr>'.format(E(" · ".join((R.get("plat") or {}).get("evidence") or []) or "-")))
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
         '<h1>Elasticsearch 클러스터 · 디스크 관점 리포트</h1>',
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
    ap.add_argument("--storage", choices=["auto", "allflash", "hybrid", "nvme", "ssd", "hdd"])
    ap.add_argument("--platform", choices=["auto", "vmware", "baremetal", "vm"])
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
    R = analyze(base, None if a.storage == "auto" else a.storage, a.bench, a.cluster, a.platform)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.bundle)), "es_disk_report_{}.html".format(R["meta"].get("host", "node")))
    render(R, out)
    print(out)
    print("플랫폼: {} · 판정 기준: {}".format(platform_label(R), storage_basis(R)))
    print("판정: {} · 조치 필요 {}건".format(R["verdict"][1], R["n_act"]))

if __name__ == "__main__":
    main()
