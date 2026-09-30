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
                            [--storage auto|allflash|hybrid|nvme|ssd|hdd]
"""
import argparse, html, json, os, re, sys, tarfile, tempfile, datetime

TOOL_VERSION = "0.10.0"

# ─────────────────────────────────────────────────────────────────────────────
# Report language. All user-facing text lives in i18n/<lang>.txt (key = "text").
# The code only holds keys, so Korean and English reports share one set of rules.
# ─────────────────────────────────────────────────────────────────────────────
I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
LANGS = ("ko", "en")


class Msg(str):
    """A catalog string that remembers its key. format() keeps the key, so a finding's title
    still tells which rule produced it (used as the finding ID)."""
    key = None

    def format(self, *a, **k):
        m = Msg(str.format(self, *a, **k))
        m.key = self.key
        return m


def _unesc(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def load_catalog(lang):
    cat = {}
    path = os.path.join(I18N_DIR, lang + ".txt")
    if not os.path.isfile(path):
        return cat
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            k, sep, v = line.partition(" = ")
            v = v.strip()
            if sep and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                cat[k.strip()] = _unesc(v[1:-1])
    return cat


LANG, _CAT, _FALLBACK = "ko", {}, {}


def T(key):
    v = _CAT.get(key)
    if v is None:
        v = _FALLBACK.get(key, key)
    m = Msg(v)
    m.key = key
    return m


def detect_lang():
    """ko when the locale says Korean, otherwise en."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v:
            return "ko" if v.lower().startswith("ko") else "en"
    return "en"


def set_lang(lang):
    global LANG, _CAT, _FALLBACK
    LANG = lang if lang in LANGS else "ko"
    _FALLBACK = load_catalog("ko")
    _CAT = _FALLBACK if LANG == "ko" else load_catalog(LANG)
    _init_texts()


# ─────────────────────────────────────────────────────────────────────────────
# 기준값 (출처를 함께 표기. 리포트에도 그대로 노출)
# ─────────────────────────────────────────────────────────────────────────────
# OS에서 관측한 디스크 응답시간(ms). Elastic이 공식 수치를 제시하지는 않는다.
# - vSAN(allflash/hybrid): Broadcom KB 389082 의 vSAN 성능 화면 정상 범위(flash <5ms, hybrid <20ms)를 기준. ESA 도 flash 기준
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
MIN_IOS_PER_INTERVAL = 20      # 이보다 I/O가 적은 구간은 응답시간 통계에서 제외 (소수 I/O 노이즈 방지)
LOW_LOAD_IOPS = 50             # p95 IOPS 가 이보다 낮고
LOW_LOAD_MBPS = 5.0            # p95 처리량도 이보다 낮으면 "부하 부족 → 한계 판정 보류"

SEV_ORDER = {"ok": 0, "na": 0, "info": 1, "caution": 2, "warn": 3, "crit": 4}

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
    sys.exit(T("r.0017") + path)

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
CONTAINER_IDS = ("docker", "podman", "lxc", "lxc-libvirt", "systemd-nspawn", "openvz", "rkt", "wsl", "proot", "pouch", "container-other")
FC_DRV = ("qla2xxx", "lpfc", "bfa", "qedf", "bnx2fc", "fnic", "zfcp", "csiostor")
ISCSI_DRV = ("iscsi_tcp", "be2iscsi", "bnx2i", "qedi", "cxgb3i", "cxgb4i", "ib_iser")
RAID_DRV = ("megaraid_sas", "mpi3mr", "hpsa", "smartpqi", "aacraid", "arcmsr", "3w-9xxx", "3w-sas", "mpt2sas", "mpt3sas", "mptsas")
# mpt*sas 는 IT(HBA) 모드면 디스크를 그대로 넘기므로 모델명으로 RAID 논리 디스크인지 한 번 더 본다
RAID_MODEL = re.compile(r'PERC|LOGICAL VOLUME|MR9\d|MegaRAID|ServeRAID|RAID|Virtual Disk|AVAGO|SmartArray|ThinkSystem R', re.I)
SAN_VENDOR = re.compile(r'^(PURE|NETAPP|3PARdata|HITACHI|HP HSV|EMC|DGC|IBM\s+2145|IBM\s+2107|HUAWEI|Nimble|NEXSAN|FUJITSU|DataCore|COMPELNT|Dell EMC|XtremIO|INFINIDAT|LIO-ORG|TrueNAS)', re.I)
VM_DISK_VENDOR = re.compile(r'^(VMware|QEMU|Msft|Virtual|Google|Amazon|0x1af4|RHEV|Xen|NUTANIX)', re.I)
# 클라우드 볼륨(네트워크 블록)과 인스턴스 로컬 디스크를 모델명으로 구분한다
# AWS: EBS·instance store 는 NVMe 모델명. Azure: 원격 디스크 "MSFT NVMe Accelerator v1", 로컬 "Microsoft NVMe Direct Disk v1/v2",
# SCSI 는 Msft Virtual Disk. GCP: NVMe PD·Hyperdisk "nvme_card-pd", Local SSD "nvme_card" (다중 컨트롤러면 nvme_card0 ...)
CLOUD_BLOCK_MODEL = re.compile(r'Amazon Elastic Block Store|MSFT NVMe Accelerator|PersistentDisk|nvme_card-pd', re.I)
CLOUD_LOCAL_MODEL = re.compile(r'Amazon EC2 NVMe Instance Storage|Microsoft NVMe Direct Disk|nvme_card\d*$|EphemeralDisk', re.I)

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
            ev.append(T("r.0019").format((vendor + " " + prod).strip() or T("r.0020")))
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
        ev.append(T("r.0021").format(override))
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

# storcli2·perccli2(MegaRAID 96xx, PERC 12 이후)는 명령 문법은 storcli 와 같지만 JSON 키 이름과 값 표기가
# 공개 문서로 확정되지 않았다. 키·값을 storcli 표기로 맞춘 뒤 같은 파서로 읽는다.
# 모르는 키는 그대로 두므로, 끝까지 못 읽으면 "해석 불가"로 알리고 원문을 남긴다
_SC_KEY = {
    "controllers": "Controllers", "responsedata": "Response Data", "basics": "Basics",
    "model": "Model", "productname": "Model", "controllermodel": "Model",
    "status": "Status", "controllerstatus": "Controller Status",
    "bbuinfo": "BBU_Info", "bbu": "BBU_Info", "batteryinfo": "BBU_Info",
    "cachevaultinfo": "Cachevault_Info", "cachevault": "Cachevault_Info", "cvinfo": "Cachevault_Info",
    "energypackinfo": "Cachevault_Info", "energypack": "Cachevault_Info", "supercapinfo": "Cachevault_Info",
    "vdlist": "VD LIST", "virtualdrives": "VD LIST", "virtualdrivelist": "VD LIST", "virtualdriveslist": "VD LIST",
    "logicaldrives": "VD LIST", "logicaldrivelist": "VD LIST", "ldlist": "VD LIST",
    "pdlist": "PD LIST", "physicaldrives": "PD LIST", "physicaldrivelist": "PD LIST", "drivelist": "PD LIST",
    "drives": "PD LIST", "physicaldriveslist": "PD LIST",
    "dgvd": "DG/VD", "vd": "VD", "vdid": "VD", "virtualdrive": "VD", "virtualdriveid": "VD", "ld": "VD", "ldid": "VD",
    "dg": "DG", "drivegroup": "DG", "diskgroup": "DG", "dgid": "DG", "arrayid": "DG",
    "type": "TYPE", "raidlevel": "TYPE", "raidtype": "TYPE",
    "state": "State", "vdstate": "State", "drivestate": "State", "pdstate": "State",
    "cache": "Cache", "writecache": "Cache", "cachepolicy": "Cache", "writepolicy": "Cache", "currentwritepolicy": "Cache",
    "eidslt": "EID:Slt", "eidslot": "EID:Slt", "enclosureslot": "EID:Slt", "drive": "EID:Slt",
    "med": "Med", "media": "Med", "mediatype": "Med", "drivetype": "Med",
    "osdrivename": "OS Drive Name", "osdevicename": "OS Drive Name", "osname": "OS Drive Name", "devicename": "OS Drive Name",
    "writecacheinitialsetting": "Write Cache(initial setting)", "initialwritecache": "Write Cache(initial setting)",
    "mediaerrorcount": "Media Error Count", "predictivefailurecount": "Predictive Failure Count",
    "smartalertflaggedbydrive": "S.M.A.R.T alert flagged by drive", "smartalert": "S.M.A.R.T alert flagged by drive",
}
_VD_STATE = {"optimal": "Optl", "optl": "Optl", "partiallydegraded": "Pdgd", "pdgd": "Pdgd", "degraded": "Dgrd",
             "dgrd": "Dgrd", "offline": "OfLn", "ofln": "OfLn", "recovery": "Rec", "rebuild": "Rec"}
_PD_STATE = {"online": "Onln", "onln": "Onln", "offline": "Offln", "offln": "Offln", "unconfiguredgood": "UGood",
             "ugood": "UGood", "unconfiguredbad": "UBad", "ubad": "UBad", "rebuild": "Rbld", "rebuilding": "Rbld",
             "rbld": "Rbld", "failed": "Failed", "globalhotspare": "GHS", "dedicatedhotspare": "DHS", "jbod": "JBOD",
             "unconfiguredshielded": "UBad", "shielded": "UBad", "copyback": "Rbld"}

_SC_LIST = ("VD LIST", "PD LIST", "BBU_Info", "Cachevault_Info")
_SC_NEST = ("Controllers", "Response Data", "Basics", "Status")

def _nk(k):
    return re.sub(r'[^a-z0-9]', '', str(k).lower())

def _canon_storcli(o, ctx=None):
    """storcli2 계열 JSON 을 storcli 표기로. ctx: 'vd' 또는 'pd' (상태 값 약어가 서로 다르다)"""
    if isinstance(o, list):
        return [_canon_storcli(x, ctx) for x in o]
    if not isinstance(o, dict):
        return o
    out, aliased = {}, set()
    for k, v in o.items():
        ks = str(k)
        m = re.match(r'^(?:pds|drives|physical[ _]drives|pd[ _]list)[ _]for[ _](?:vd|virtual[ _]drive|ld)[ _]?(\d+)$', ks, re.I)
        if m:
            out["PDs for VD " + m.group(1)] = _canon_storcli(v, "pd"); continue
        m = re.match(r'^(?:vd|virtual[ _]drive|ld)[ _]?(\d+)[ _]properties$', ks, re.I)
        if m:
            out["VD{} Properties".format(m.group(1))] = _canon_storcli(v, "vd"); continue
        m = re.match(r'^(?:drive[ _])?(/c\d+(?:/e\d+)?/s\d+)(?:[ _]-)?[ _]detailed[ _]information$', ks, re.I)
        if m:
            out["Drive {} - Detailed Information".format(m.group(1))] = _canon_storcli(v, "pd"); continue
        if re.match(r'^/c\d+/v\d+$', ks):
            out[ks] = _canon_storcli(v, "vd"); continue
        ck = _SC_KEY.get(_nk(ks), ks)
        # 목록 키로 바꾸는 것은 값이 목록일 때만 ("Virtual Drives": 2 같은 개수 필드와 구분)
        if ck in _SC_LIST and ck != ks and not isinstance(v, (list, dict)):
            ck = ks
        elif ck not in _SC_LIST and ck not in _SC_NEST and ck != ks and isinstance(v, (list, dict)):
            ck = ks          # 값 하나여야 하는 키(State, Med 등)는 값이 목록·객체면 바꾸지 않는다
        sub = "vd" if ck == "VD LIST" else ("pd" if ck == "PD LIST" else ctx)
        cv = _canon_storcli(v, sub)
        if isinstance(cv, str):
            n = _nk(cv)
            if ck == "State" and ctx == "vd":
                cv = _VD_STATE.get(n, cv)
            elif ck == "State" and ctx == "pd":
                cv = _PD_STATE.get(n, cv)
            elif ck == "Med":
                cv = "SSD" if re.search(r'ssd|solidstate|nvme|flash', n) else ("HDD" if re.search(r'hdd|harddisk|rotational|spinning|sas$|sata$', n) and "ssd" not in n else cv)
            elif ck == "Cache" and not re.search(r'\b(A?WB|WT)\b', cv):
                cv = re.sub(r'(?i)always\s*write\s*-?\s*back', 'AWB', cv)
                cv = re.sub(r'(?i)write\s*-?\s*back', 'WB', cv)
                cv = re.sub(r'(?i)write\s*-?\s*through', 'WT', cv)
        # 원래 이름(storcli 표기)이 별칭보다 우선
        if ck not in out or (ck == ks and ck in aliased):
            out[ck] = cv
            if ck != ks:
                aliased.add(ck)
            else:
                aliased.discard(ck)
    if ctx == "vd" and "DG/VD" not in out and "VD" in out:
        out["DG/VD"] = "{}/{}".format(out.get("DG", ""), out["VD"])
    return out


def parse_storcli(text):
    R = {"tool": "storcli", "ctrl": [], "vds": [], "pds_bad": [], "bg": [], "pd_all": []}
    vd_map, pd_by_dg = {}, {}
    for cmd, body in _cmd_blocks(text):
        i = body.find("{")
        try:
            j = json.JSONDecoder().raw_decode(body[i:])[0] if i >= 0 else None   # 뒤에 붙은 텍스트는 무시
        except ValueError:
            j = None
        if not j:
            continue
        j = _canon_storcli(j)
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
                                R["pds_bad"].append((T("r.0022").format(m.group(1), int(pf), T("r.0023") if sm else T("r.0024")), "warn"))
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
            R["bg"].append(T("r.0025").format(p_.get("EID:Slt")))
        elif st in ("Offln", "UBad", "Failed", "F"):
            R["pds_bad"].append((T("r.0026").format(p_.get("EID:Slt"), st), "warn"))
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
                R["pds_bad"].append((T("r.0027").format(p_["id"], p_["state"]), "warn"))
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
                R["pds_bad"].append((T("r.0028").format(v), "warn"))
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
        if "#CMD" not in t:
            for l in t.splitlines():
                if l.startswith("#TOOL_ABSENT"):
                    absent.append(l.split(None, 1)[1].strip())
            continue
        try:
            r = fn(t)
        except Exception:
            r = None
        if r is None or (not r.get("vds") and fname == "raid_storcli"):
            # storcli2·perccli2 JSON 은 키 이름이 공개 문서로 확정되지 않았다. 해석을 못 하면 원문만 번들에 남긴다
            absent.append("#UNPARSED " + ("storcli2" if "#TOOL storcli2" in t else fname.replace("raid_", "")))
            continue
        # OS 장치 이름이 없으면 SCSI 주소로 잇는다.
        # megaraid_sas: channel 0·1 은 물리 디스크, 2 이상이 논리 디스크. VD 번호 = (channel-2)*128 + target
        # aacraid: channel 0, target = LD 번호. mpi3mr 는 channel 이 고정되지 않아 OS Drive Name 으로만 잇는다
        for v in r["vds"]:
            if v.get("dev"):
                v["dev"] = v["dev"].replace("/dev/", "")
                continue
            for d, h in topo.hctl.items():
                try:
                    H, C, T, L = (int(x) for x in h.split(":"))
                except ValueError:
                    continue
                drv = topo.hostdrv.get("host%d" % H)
                if r["tool"] == "storcli" and drv == "megaraid_sas" and C >= 2 and (C - 2) * 128 + T == int(v.get("id") or -1):
                    v["dev"] = d
                elif r["tool"] == "arcconf" and drv == "aacraid" and C == 0 and T == int(v.get("id") or -1):
                    v["dev"] = d
        res.append(r)
    return res, absent

def flat_settings(cs):
    """_cluster/settings 응답을 그룹별 "a.b.c": 값 형태로. flat_settings=true 응답과 중첩 응답을 모두 받는다.
    (flat_settings=true 에서는 filter_path 가 점이 든 키 이름과 맞지 않아 빈 응답이 오므로 수집은 중첩으로 한다)"""
    out = {}
    def walk(o, pre, dst):
        for k, v in (o or {}).items():
            key = "{}.{}".format(pre, k) if pre else str(k)
            if isinstance(v, dict):
                walk(v, key, dst)
            else:
                dst[key] = v
    for grp in ("defaults", "persistent", "transient"):
        g = (cs or {}).get(grp)
        if isinstance(g, dict):
            out[grp] = {}
            walk(g, "", out[grp])
    return out


def wm_high(groups, total_bytes):
    """실제로 적용되는 high watermark 사용률(%)과 표시 문구.
    ES 8.5+ 는 비율을 직접 지정하지 않았으면 max_headroom(high 기본 150GB)도 함께 적용해, 큰 디스크에서는
    여유 공간이 150GB 로 줄어드는 시점이 기준이 된다 (둘 중 늦게 오는 쪽)"""
    groups = groups or {}
    merged, explicit = {}, set()
    for grp in ("defaults", "persistent", "transient"):
        for k, v in (groups.get(grp) or {}).items():
            merged[k] = v
            if grp != "defaults":
                explicit.add(k)
    key = "cluster.routing.allocation.disk.watermark.high"
    hi = str(merged.get(key, "90%"))
    if not hi.endswith("%"):
        return None, hi
    pct = num(hi.rstrip("%"))
    hr = merged.get(key + ".max_headroom")
    if pct is None or key in explicit or not hr or not total_bytes:
        return pct, hi
    hb = parse_bytes(hr)
    if not hb or hb <= 0:
        return pct, hi
    eff = max(pct, 100.0 * (total_bytes - hb) / total_bytes)
    if eff > pct + 0.5:
        return eff, T("r.0029").format(hi, hr, eff)
    return pct, hi


def parse_bytes(v):
    m = re.match(r'^\s*([\d.]+)\s*([kmgtp]?b?)\s*$', str(v or ""), re.I)
    if not m:
        return None
    mul = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3,
           "t": 1024 ** 4, "tb": 1024 ** 4, "p": 1024 ** 5, "pb": 1024 ** 5}[m.group(2).lower()]
    return float(m.group(1)) * mul


def parse_ebs_stats(text):
    """nvme amzn stats / ebsnvme stats 결과. 장치별 한도 초과 누적 시간(us).
    출력 형식이 도구·버전마다 다를 수 있어 JSON(한 줄이든 여러 줄이든)과 텍스트를 모두 읽는다.
    텍스트는 'EBS Volume Performance Exceeded (us)' 같은 제목 아래 'IOPS: n' 줄, 또는 한 줄에
    'ebs_volume_performance_exceeded_iops : n' 처럼 이름과 값이 같이 오는 형식을 본다"""
    out, blocks, dev = {}, {}, None
    for l in (text or "").splitlines():
        if l.startswith("#DEV "):
            dev = l.split()[1]; blocks.setdefault(dev, []); continue
        if dev is not None:
            blocks[dev].append(l)
    def metric(low):
        if "iops" in low:
            return "iops"
        if re.search(r'throughput|(^|[^a-z])tp([^a-z]|$)', low):
            return "tp"
        return None
    def scope(low, default=None):
        return "inst" if "instance" in low else ("vol" if "volume" in low else default)
    for dev, lines in blocks.items():
        body, got = "\n".join(lines), {}
        i = body.find("{")
        if i >= 0:
            try:
                j = json.JSONDecoder().raw_decode(body[i:])[0]
                def walk(o, pre=""):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            walk(v, pre + "_" + str(k).lower())
                    elif isinstance(o, (int, float)) and "exceeded" in pre:
                        sc, mt = scope(pre, "vol"), metric(pre)
                        if mt:
                            got[sc + "_" + mt] = int(o)
                walk(j)
            except ValueError:
                pass
        if not got:
            sect = None
            for l in lines:
                low = l.strip().lower()
                m = re.search(r'[:=]\s*(\d+)\s*(us|µs)?\s*,?\s*$', low)
                if "exceeded" in low:
                    sc, mt = scope(low, sect), metric(low)
                    if m and mt and sc:
                        got[sc + "_" + mt] = int(m.group(1))
                    elif not m:
                        sect = sc or "vol"
                    continue
                m2 = re.match(r'^(iops|throughput|tp)\s*[:=]\s*(\d+)', low)
                if m2 and sect:
                    got[sect + ("_iops" if m2.group(1) == "iops" else "_tp")] = int(m2.group(2))
                elif low and not m2 and not low.startswith(("read", "write")) and ":" not in low:
                    sect = None       # 다른 제목이 나오면 구역이 끝난 것
        if got:
            out[dev] = got
    return out


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
                info["attach"], info["media"], info["why"] = "cloud", "ssd", T("r.0030").format(info["model"])
            elif CLOUD_LOCAL_MODEL.search(info["model"] or ""):
                info["media"], info["why"] = "nvme", T("r.0031").format(info["model"])
            else:
                info["media"], info["why"] = "nvme", "NVMe"
        elif re.match(r'^(rbd|nbd)\d+', d):
            info["attach"], info["media"], info["sure"] = "network", "ssd", False
            info["why"] = T("r.0032") if d.startswith("rbd") else T("r.0033")
        elif re.match(r'^xvd', d) and cloud:
            info["attach"], info["media"], info["why"] = "cloud", "ssd", T("r.0034")
        elif cloud and (CLOUD_BLOCK_MODEL.search(model) or (vendor.lower() == "msft" and "virtual disk" in model.lower())):
            info["attach"], info["media"], info["why"] = "cloud", "ssd", T("r.0035").format(vendor, model).strip()
        elif drv in FC_DRV or drv in ISCSI_DRV or SAN_VENDOR.search(vendor):
            info["attach"], info["media"], info["sure"] = "san", "ssd", False
            info["why"] = "FC HBA" if drv in FC_DRV else ("iSCSI" if drv in ISCSI_DRV else T("r.0036") + vendor)
        else:
            if drv in RAID_DRV and (drv not in ("mpt2sas", "mpt3sas", "mptsas") or RAID_MODEL.search(model)):
                info["raid"] = True
            elif RAID_MODEL.search(model) and not VM_DISK_VENDOR.search(vendor):
                info["raid"] = True
            if a.get("device/raid_level"):            # hpsa·smartpqi 는 sysfs 에 RAID 레벨을 내준다
                info["raid"] = True
            info["media"] = "hdd" if info["rot"] == "1" else "ssd"
            info["sure"] = not info["raid"]
            info["why"] = (T("r.0037").format(drv or model, info["rot"]) if info["raid"]
                           else "rotational={}".format(info["rot"]))
            if d in raid_vd:
                tool, v = raid_vd[d]
                info["raid"] = True
                info["raid_level"] = v.get("level")
                if v.get("media"):
                    info["media"], info["sure"] = v["media"], True
                info["why"] = T("r.0038").format(
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
        if not p or p[0].startswith(("Average", "\ud3c9\uade0")):   # sar summary line (en, ko locale). Data, not UI text
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
            "used_pct": (100.0 * (tot - avail) / tot) if (tot and avail is not None) else None, "tot_b": tot,
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
                scope = "" if len(tiers) == 1 else T("r.0040").format(tname)
                add("caution", T("r.0041"), T("r.0042"), T("r.0043").format(label, top["name"]),
                    T("r.0044").format(
                        top["name"], fmt(top[key], 1, unit), fmt(med, 1, unit), scope, len(vals)),
                    T("r.0045"),
                    T("r.0046"), T("r.0047"))
    outlier("busy", T("r.0048"), "%", 40)
    outlier("wmb", T("r.0049"), " MB/s", 30)
    outlier("store_gb", T("r.0050"), " GB", 100, factor=1.6)

    # ── 용량·watermark ───────────────────────────────────────────────────
    settings = {}
    cs = flat_settings(j("cluster_settings.json") or {})
    for grp in ("defaults", "persistent", "transient"):
        settings.update(cs.get(grp) or {})
    over = []
    for r in data_rows:
        lim, lab = wm_high(cs, r.get("tot_b"))
        if r["used_pct"] is not None and lim and r["used_pct"] >= lim - 5:
            over.append((r, lim, lab))
    if over:
        sev = "crit" if any(r["used_pct"] >= lim for r, lim, _ in over) else "caution"
        add(sev, T("r.0041"), T("r.own_es"), T("r.0052"),
            ", ".join("{} {:.0f}% (high {})".format(r["name"], r["used_pct"], lab) for r, lim, lab in over[:6]),
            T("r.0053"),
            T("r.0054"),
            T("r.0055"))

    # ── 클러스터발 디스크 부하 (측정 오염 요인) ────────────────────────
    rec = j("cat_recovery.json") or []
    health = j("health.json") or {}
    snap = j("snapshot_status.json") or {}
    acts = []
    if isinstance(rec, list) and rec:
        acts.append(T("r.0056").format(len(rec)))
    if (health.get("relocating_shards") or 0) > 0:
        acts.append(T("r.0057").format(health["relocating_shards"]))
    if (health.get("initializing_shards") or 0) > 0:
        acts.append(T("r.0058").format(health["initializing_shards"]))
    if snap.get("snapshots"):
        acts.append(T("r.0059").format(len(snap["snapshots"])))
    pend = j("pending_tasks.json")
    if isinstance(pend, list) and len(pend) > 5:
        acts.append(T("r.0060").format(len(pend)))
    if acts:
        rmax = settings.get("indices.recovery.max_bytes_per_sec", "40mb")
        add("info", T("r.0041"), T("r.0013"), T("r.0061"),
            " · ".join(acts) + " · indices.recovery.max_bytes_per_sec={}".format(rmax),
            T("r.0062"),
            T("r.0063"),
            "_cat/recovery, _cluster/health, _snapshot/_status")

    # ── 전 노드 공통 신호 ────────────────────────────────────────────────
    thr_nodes = [r["name"] for r in data_rows if (r["throttle"] or 0) > 0]
    if len(thr_nodes) >= 2:
        add("warn", T("r.0041"), T("r.0042"), T("r.0064"),
            T("r.0065").format(len(thr_nodes), ", ".join(thr_nodes[:8])),
            {"vmware": T("r.0066"),
             "baremetal": T("r.0067")
             }.get(kind, T("r.0068")),
            {"vmware": T("r.0069"),
             "baremetal": T("r.0070")
             }.get(kind, T("r.0071")),
            "_nodes/stats indices.indexing.throttle_time")
    aw = settings.get("cluster.routing.allocation.awareness.attributes")
    if not aw and len(data_rows) >= 3:
        if kind == "vmware":
            add("info", T("r.0041"), T("r.0072"), T("r.0073"),
                T("r.0074").format(len(data_rows)),
                T("r.0075"),
                T("r.0076"),
                T("r.0077"))
        elif kind == "baremetal":
            add("info", T("r.0041"), T("r.own_es"), T("r.0073"),
                T("r.0074").format(len(data_rows)),
                T("r.0078"),
                T("r.0079"),
                T("r.0077"))
        else:
            add("info", T("r.0041"), T("r.own_es"), T("r.0073"),
                T("r.0074").format(len(data_rows)),
                T("r.0080"),
                T("r.0081"),
                T("r.0077"))
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
    # level=indices 응답은 nodes.<id>.indices.indices.<인덱스> 로 한 단계 더 들어간다 (ES NodeIndicesStats).
    # 예전 수집기 번들은 filter_path 가 한 단계 모자라 빈 응답이었다
    def per_index(g):
        top = g.get("indices") or {}
        inner = top.get("indices")
        return inner if isinstance(inner, dict) else top
    ia, ib = per_index(ga[0]), per_index(gb[0])
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

def analyze(base, storage_override=None, cluster_dir=None, platform_override=None):
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
    csettings = flat_settings(rjson(S, "es_cluster_settings.json"))
    health = rjson(S, "es_health.json")
    node_i = list((nodeinfo or {}).get("nodes", {}).values())[0] if (nodeinfo or {}).get("nodes") else {}
    n0 = list((st0 or {}).get("nodes", {}).values())[0] if (st0 or {}).get("nodes") else None
    n1 = list((st1 or {}).get("nodes", {}).values())[0] if (st1 or {}).get("nodes") else None
    es_version = dig(es_root or {}, "version", "number") or dig(node_i, "version") or T("r.0082")

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
            media_note = T("r.0083").format(STORAGE_LABEL[storage])
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
    VMB = {"vsan": "vSAN", "ds": T("r.0084"), "unknown": T("r.0085")}[VMBK]
    def vs(vsan_txt, ds_txt, unknown_txt=None):
        """VMware 데이터스토어 종류에 맞는 문구"""
        return {"vsan": vsan_txt, "ds": ds_txt}.get(VMBK, unknown_txt if unknown_txt is not None else vsan_txt)
    unsure = [d for d, c in devcls.items() if not c["sure"]]
    is_vmware = kind == "vmware"
    # "OS 바깥"을 맡는 담당자와 자원 차원 이름
    if kind == "vmware":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0072"), T("r.0086"), T("r.0087"), T("r.0088").format(VMB)
    elif kind == "baremetal" and attach == "san" and "network" in attaches:
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0089"), T("r.0090"), T("r.0091"), T("r.0092")
    elif kind == "baremetal" and attach == "san":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0089"), T("r.0090"), T("r.0093"), T("r.0094")
    elif kind == "baremetal" and attach == "local":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0095"), T("r.0090"), T("r.0096"), T("r.0097")
    elif attach == "cloud":
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0098"), T("r.0099"), T("r.0087"), T("r.0100")
    else:
        OUT, RES_DIM, WHERE_IN, WHERE_OUT = T("r.0098"), T("r.0099"), T("r.0087"), T("r.0101")

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
        for key, label, unit in (("iops", "IOPS", ""), ("mb", T("r.0102"), " MB/s")):
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
            "vmware": T("r.0103"),
            "baremetal": (T("r.0104") if attach == "san" else
                          T("r.0105")),
        }.get(kind, T("r.0106")
              if attach == "cloud" else T("r.0107"))
        add("caution", T("r.0108"), OUT, T("r.0109").format(T("r.0110") if label == "IOPS" else T("r.0111")),
            T("r.0112").format(
                label, fmt(mx, 0 if not unit else 1, unit), share * 100, fmt(aq_top, 1), fmt(aq_rest, 1)),
            T("r.0113").format(T("r.0114") if label == "IOPS" else T("r.0115"), causes),
            T("r.0116").format(
                OUT, fmt(mx, 0 if not unit else 1, unit)),
            T("r.0117"))

    # ── AWS EBS: 볼륨·인스턴스 한도를 넘긴 시간 (Nitro NVMe 로그 페이지, 누적 us) ─────────
    ebs0, ebs1 = parse_ebs_stats(rd(S, "ebs_stats_start")), parse_ebs_stats(rd(S, "ebs_stats_end"))
    win = (snaps[-1]["t"] - snaps[0]["t"]) if len(snaps) >= 2 else 0
    EBS, ebs_sevs = {}, []
    for d_ in phys:
        a_, b_ = ebs0.get(d_), ebs1.get(d_)
        if not a_ or not b_ or win <= 0:
            continue
        EBS[d_] = {k: max(b_[k] - a_.get(k, 0), 0) / 1e6 for k in b_ if k in a_}
    if EBS:
        for scope, lab, fix in (("vol", T("r.0118"), T("r.0119")),
                                ("inst", T("r.0120"), T("r.0121"))):
            hit = [(d_, v.get(scope + "_iops", 0), v.get(scope + "_tp", 0)) for d_, v in EBS.items()
                   if max(v.get(scope + "_iops", 0), v.get(scope + "_tp", 0)) >= 0.01 * win]
            if hit:
                worst = max(max(h[1], h[2]) for h in hit) / win
                ebs_sevs.append("warn" if worst >= 0.1 else "caution")
                add(ebs_sevs[-1], T("r.0108"), T("r.0098"),
                    T("r.0122").format(lab),
                    ", ".join(T("r.0123").format(h[0], h[1], h[2]) for h in hit)
                    + T("r.0124").format(win),
                    T("r.0125").format(lab),
                    fix + ".", T("r.0126"))

    # ═════════════ 1. 지연 ═════════════
    r_sev, w_sev = grade(A["r_await_p95"], th), grade(A["w_await_p95"], th)
    lat_ev = T("r.0127").format(
        fmt(A["r_await_p95"], 2, "ms"), fmt(A["r_await_mean"], 2, "ms"),
        fmt(A["w_await_p95"], 2, "ms"), fmt(A["w_await_mean"], 2, "ms"),
        MIN_IOS_PER_INTERVAL, A["valid_r"], A["valid_w"])
    lat_sev = sev_max(r_sev, w_sev)
    if A["valid_r"] + A["valid_w"] == 0:
        add("info", T("r.0128"), T("r.0013"), T("r.0129"),
            lat_ev, T("r.0130"),
            T("r.0131"), src_lat)
        lat_sev = "na"
    elif lat_sev in ("ok",):
        add("ok", T("r.0128"), T("r.0013"), T("r.0132"), lat_ev,
            {"vmware": T("r.0133").format(VMB),
             "baremetal": T("r.0134")}.get(kind,
             T("r.0135")),
            T("r.0136"), src_lat)
    else:
        add(lat_sev, T("r.0128"), T("r.0042"),
            T("r.0137").format(
                {"allflash": "All-Flash", "hybrid": "Hybrid"}.get(storage, STORAGE_LABEL.get(storage, storage)), SEV_LABEL[lat_sev]),
            lat_ev,
            T("r.0138"),
            T("r.0139").format(
                "Guest" if kind in ("vmware", "vm", "unknown") else "OS", OUT.replace(T("r.0140"), "").replace(T("r.0141"), "")),
            src_lat)

    # 병목 위치: 큐 사용률과 응답시간의 조합
    qd = [num(topo.attr.get(d, {}).get("device/queue_depth")) for d in phys]
    qd = [q for q in qd if q]
    qd_total = sum(qd) if qd else None
    qratio = (A["aqu_p95"] / qd_total) if (qd_total and A["aqu_p95"] is not None) else None
    # inflight(장치에 넘겨져 처리 중인 I/O)는 queue_depth와 직접 비교 가능한 값이라 교차 확인에 쓴다.
    # aqu-sz 는 블록 계층 큐(nr_requests)에서 대기 중인 요청까지 포함하므로 queue_depth 를 넘을 수 있다.
    iratio = (A["inflight_p95"] / qd_total) if (qd_total and A["inflight_p95"] is not None) else None
    q_ev = T("r.0142").format(
        fmt(A["aqu_p95"], 1),
        T("r.0143").format(fmt(A["inflight_p95"], 1)) if A["inflight_p95"] is not None else "",
        int(qd_total) if qd_total else T("r.0082"))
    QSRC = (T("r.0144"))
    if kind == "vmware" and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
        if qratio is None:
            add("caution", T("r.0128"), T("r.0042"), T("r.0145"),
                q_ev, T("r.0146"),
                T("r.0147"), QSRC)
        elif qratio >= 0.8 or (iratio is not None and iratio >= 0.8):
            add("warn", T("r.0108"), T("r.0148"),
                T("r.0149").format(qratio * 100),
                q_ev,
                T("r.0150"),
                T("r.0151"), QSRC)
        elif qratio >= 0.4:
            # 큐도 깊고 지연도 높다 → 한쪽으로 단정할 수 없는 구간
            add("warn" if lat_sev in ("warn", "crit") else "caution", T("r.0128"), T("r.0042"),
                T("r.0152").format(qratio * 100),
                q_ev,
                T("r.0153"),
                T("r.0154"), QSRC)
        else:
            add("warn" if lat_sev in ("warn", "crit") else "caution", T("r.0128"), T("r.0072"),
                T("r.0155").format(WHERE_OUT),
                q_ev + T("r.0156").format(qratio * 100),
                T("r.0157") + vs(
                    T("r.0158"),
                    T("r.0159"),
                    T("r.0160"))
                + T("r.0161"),
                T("r.0162") + vs(
                    T("r.0163"),
                    T("r.0164"),
                    T("r.0165"))
                + T("r.0166"), QSRC)
    # 쓰기만 느림 → vSAN 쓰기 경로 힌트
    write_only = bool(A["w_await_p95"] and A["r_await_p95"] and A["valid_w"] >= 3 and A["valid_r"] >= 3
                      and A["w_await_p95"] >= th["caution"] and A["w_await_p95"] > 3 * A["r_await_p95"])
    if kind == "vmware" and write_only:
        vsan_why = (T("r.0167"))
        ds_why = (T("r.0168"))
        add("caution", T("r.0128"), T("r.0072"), vs(T("r.0169"), T("r.0170"),
                                                  T("r.0170")),
            T("r.0171").format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
            vs(vsan_why, ds_why, T("r.0172") + vsan_why + T("r.0173") + ds_why),
            vs(T("r.0174"),
               T("r.0175"),
               T("r.0176")),
            vs(T("r.0177"), T("r.0178"),
               T("r.0179")))

    # ── vSAN 이 아닌 플랫폼의 병목 위치 ─────────────────────────────────────
    # 같은 원리(대기 I/O ÷ queue_depth)를 쓰되, "바깥"이 무엇인지와 담당자가 다르다.
    #  bare-metal 로컬 : 큐가 차면 장치가 동시 처리 한계(포화), 큐가 비었는데 느리면 장치 자체 이상
    #  bare-metal SAN  : 큐가 차면 HBA LUN 큐, 비었는데 느리면 어레이·패브릭
    #  기타 VM·클라우드 : vSAN 과 같은 구도지만 백엔드를 특정하지 않는다
    GSRC = (T("r.0180"))
    no_qd_dev = [d for d in phys if not topo.attr.get(d, {}).get("device/queue_depth")]
    if kind != "vmware" and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
        hi_sev = "warn" if lat_sev in ("warn", "crit") else "caution"
        if qratio is None and no_qd_dev and meta.get("is_root") != "0":
            # NVMe, virtio-blk 은 queue_depth 개념이 SCSI 와 달라 파일이 없다. 권한 문제가 아니다
            if kind == "baremetal" and "network" in attaches:
                add(hi_sev, T("r.0128"), OUT, T("r.0181"),
                    q_ev + T("r.0182").format(", ".join(no_qd_dev)),
                    T("r.0183"),
                    T("r.0184"),
                    T("r.0185"))
            elif kind == "baremetal":
                add(hi_sev, T("r.0128"), T("r.0042"), T("r.0186"),
                    q_ev + T("r.0187").format(", ".join(no_qd_dev)),
                    T("r.0188"),
                    T("r.0189"), GSRC)
            else:
                add(hi_sev, T("r.0128"), T("r.0042"), T("r.0190"),
                    q_ev + T("r.0182").format(", ".join(no_qd_dev)),
                    T("r.0191"),
                    T("r.0192").format(OUT), src_lat)
        elif qratio is None:
            add("caution", T("r.0128"), T("r.0042"), T("r.0145"),
                q_ev, T("r.0193").format(WHERE_IN, WHERE_OUT),
                T("r.0194"), GSRC)
        elif qratio >= 0.8 or (iratio is not None and iratio >= 0.8):
            if kind == "baremetal" and attach == "san":
                add("warn", T("r.0108"), T("r.0148"), T("r.0195").format(qratio * 100), q_ev,
                    T("r.0196"),
                    T("r.0197"), GSRC)
            elif kind == "baremetal":
                add("warn", T("r.0108"), T("r.0148"), T("r.0198").format(qratio * 100), q_ev,
                    T("r.0199"),
                    T("r.0200"),
                    T("r.0201"))
            else:
                add("warn", T("r.0108"), T("r.0148"), T("r.0202").format(qratio * 100), q_ev,
                    T("r.0203"),
                    T("r.0204").format(OUT), GSRC)
        elif qratio >= 0.4:
            add(hi_sev, T("r.0128"), T("r.0042"),
                T("r.0205").format(WHERE_IN, WHERE_OUT, qratio * 100), q_ev,
                T("r.0206"),
                {"san": T("r.0207"),
                 "local": T("r.0208")
                 }.get(attach, T("r.0209").format(OUT)), GSRC)
        else:
            if kind == "baremetal" and attach == "san":
                ttl, why, act = (T("r.0210"),
                    T("r.0211"),
                    T("r.0212"))
            elif kind == "baremetal":
                ttl, why, act = (T("r.0213"),
                    T("r.0214"),
                    T("r.0215"))
            else:
                ttl, why, act = (T("r.0216"),
                    T("r.0217"),
                    T("r.0218").format(OUT))
            add(hi_sev, T("r.0128"), OUT, ttl, q_ev + T("r.0156").format(qratio * 100), why, act, GSRC)
    def raid_cache_note():
        vds = [v for r in hwraid for v in r["vds"] if v.get("dev") in phys]
        if vds and any(v.get("wb") is False for v in vds):
            return T("r.0219")
        if vds and all(v.get("wb") for v in vds):
            return (T("r.0220").format(", ".join(sorted(set(str(v.get("level")) for v in vds)))))
        return T("r.0221")
    if kind != "vmware" and write_only:
        if kind == "baremetal" and attach == "local" and (storage == "hdd" or any(c["raid"] for c in devcls.values())):
            add("caution", T("r.0128"), OUT, T("r.0222"),
                T("r.0171").format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                T("r.0223"),
                raid_cache_note(),
                T("r.0224") + (T("r.0225") if hwraid else T("r.0226")))
        elif kind == "baremetal" and attach == "local":
            add("caution", T("r.0128"), OUT, T("r.0227"),
                T("r.0171").format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                T("r.0228"),
                T("r.0229"),
                T("r.0230"))
        else:
            add("caution", T("r.0128"), OUT, T("r.0170"),
                T("r.0171").format(fmt(A["w_await_p95"], 2, "ms"), fmt(A["r_await_p95"], 2, "ms")),
                T("r.0231"),
                T("r.0232").format(OUT),
                T("r.0178"))

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
                ev_ = T("r.0233").format(d, fmt(v, 2, "ms"), len(others), fmt(med, 2, "ms"))
                if paths:
                    add("warn", T("r.0128"), T("r.0089"), T("r.0234").format(d), ev_,
                        T("r.0235"),
                        T("r.0236"),
                        T("r.0237"))
                elif kind == "baremetal":
                    add("warn", T("r.0128"), OUT, T("r.0238").format(d), ev_,
                        T("r.0239"),
                        T("r.0240"),
                        T("r.0241"))
                else:
                    add("warn", T("r.0128"), OUT, T("r.0242").format(d), ev_,
                        T("r.0243"),
                        T("r.0244").format(OUT),
                        T("r.0241"))
                # 합산 지표가 정상이어도 stripe 전체는 이 디스크 속도로 움직이므로 지연 차원에 반영한다
                lat_sev = sev_max(lat_sev, grade(v, th))

    # ── 측정 환경 ───────────────────────────────────────────────────────────
    mapped = bool(path_map) and all(pm.get("via") == "mountinfo" for pm in path_map)
    if plat["container"] and mapped:
        add("info", T("r.0245"), T("r.0013"), T("r.0246"),
            T("r.0247").format(plat["container"], ", ".join("{} → {}".format(pm["path"], pm["kname"]) for pm in path_map)),
            T("r.0248"),
            T("r.0249"),
            "/proc/<pid>/mountinfo, systemd-detect-virt -c")
    elif plat["container"]:
        add("caution", T("r.0245"), T("r.0013"), T("r.0250"),
            T("r.0251").format(plat["container"], HV_LABEL.get(plat["hv"], plat["hv"]) if plat["hv"] else T("r.0082")),
            T("r.0252"),
            T("r.0253"),
            "systemd-detect-virt -c")
    # 0.10.0 초기 수집기는 mount namespace 만 달라도(systemd PrivateTmp) 컨테이너로 기록했다. cgroup 으로 한 번 더 본다
    cg_es = rd(S, "es_cgroup")
    in_cont = meta.get("es_in_container") == "1" and not (
        cg_es and re.search(r'system\.slice/[^/\s]*\.service', cg_es) and not re.search(r'docker|kubepods|containerd|libpod|lxc|crio', cg_es))
    if in_cont:
        add("info", T("r.0245"), T("r.0013"), T("r.0254"),
            T("r.0255") + (", ".join("{} → {} ({})".format(pm["path"], pm["kname"] or "?", pm.get("via")) for pm in path_map) or T("r.0082")),
            T("r.0256"),
            T("r.0257"),
            T("r.0258"))
    if kind == "unknown":
        add("info", T("r.0245"), T("r.0013"), T("r.0259"),
            " · ".join(plat["evidence"]) or T("r.0260"),
            T("r.0261"),
            T("r.0262"),
            "systemd-detect-virt, /sys/class/dmi/id, /proc/cpuinfo")
    if media_note:
        add("info", T("r.0245"), T("r.0013"), T("r.0263"), media_note + " · " + ", ".join(
            "{}={}".format(d, STORAGE_LABEL.get(c["media"], c["media"])) for d, c in sorted(devcls.items())),
            T("r.0264"),
            T("r.0265"),
            T("r.0266"))

    # ═════════════ 2. 포화 ═════════════
    psi_full = [r.get("psi_io_full") for r in sysr if r.get("psi_io_full") is not None]
    psi_some = [r.get("psi_io_some") for r in sysr if r.get("psi_io_some") is not None]
    iow = [r.get("iowait") for r in sysr if r.get("iowait") is not None]
    dst = [r.get("es_dstate") for r in sysr if r.get("es_dstate") is not None]
    blk = [r.get("blocked") for r in sysr if r.get("blocked") is not None]
    sat_sevs = list(ebs_sevs)       # AWS 가 직접 보고한 한도 초과는 실측 포화로 본다
    if psi_full:
        pf95 = pctl(psi_full, 0.95)
        s = "warn" if pf95 >= 20 else "caution" if pf95 >= 5 else "ok"
        psi_ev = T("r.0267").format(fmt(pctl(psi_some, .95), 1, "%"), fmt(pf95, 1, "%"), fmt(vmax(psi_full), 1, "%"))
        psi_src = T("r.0268")
        if s != "ok" and SEV_ORDER.get(lat_sev, 0) <= SEV_ORDER["ok"]:
            # 기다리는 시간은 많은데 한 건 한 건은 빠름 → 디스크가 느린 게 아니라 I/O 양이 많은 상태
            s = "caution" if s == "warn" else "info"
            sat_sevs.append(s)
            add(s, T("r.0108"), T("r.0042"), T("r.0269"),
                psi_ev + T("r.0270").format(fmt(A["r_await_p95"], 2, "ms"), fmt(A["w_await_p95"], 2, "ms")),
                T("r.0271"),
                T("r.0272"),
                psi_src)
        else:
            sat_sevs.append(s)
            add(s, T("r.0108"), T("r.0013") if s == "ok" else T("r.0042"),
                T("r.0273").format(fmt(pf95, 1, "%")), psi_ev,
                T("r.0274"),
                T("r.0275") if s != "ok" else T("r.0276"), psi_src)
    else:
        add("info", T("r.0108"), T("r.0013"), T("r.0277"),
            T("r.0278"),
            T("r.0279"),
            T("r.0280"),
            T("r.0281"))
    if dst:
        d95, dmx = pctl(dst, .95), vmax(dst)
        s = "warn" if d95 >= 8 else "caution" if d95 >= 3 else "ok"
        sat_sevs.append(s)
        if s != "ok":
            add(s, T("r.0108"), T("r.0042"), T("r.0282"),
                T("r.0283").format(fmt(d95, 0), fmt(dmx, 0)),
                T("r.0284"),
                T("r.0285"),
                T("r.0286"))
    iow_p95 = pctl(iow, .95)
    add("info", T("r.0108"), T("r.0013"), T("r.0287").format(fmt(iow_p95, 1, "%")),
        T("r.0288").format(fmt(avg(iow), 1, "%"), fmt(vmax(iow), 1, "%"), fmt(pctl(blk, .95), 0)),
        T("r.0289"),
        T("r.0290"), "Linux proc(5) /proc/stat")
    sat_sev = sev_max(*sat_sevs) if sat_sevs else "na"

    # ═════════════ 3. 오류 (커널 로그) ═════════════
    klog = rd(S, "klog_io").splitlines()
    STO = r'(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9]|megaraid|mpt[23]sas|hpsa|smartpqi|aacraid|qla2xxx|lpfc)'
    cats = {
        T("r.0291"): r'I/O error|blk_update_request|Buffer I/O error|critical medium|Medium Error|rejecting I/O',
        "SCSI abort/reset": STO + r'.*\b(abort\w*|reset)\b',
        T("r.0292"): r'hung_task|blocked for more than',
        T("r.0293"): r'XFS .*(error|shutdown|corruption)|EXT4-fs error|remount.*read-only',
        T("r.0294"): STO + r'.*(timed out|timing out|timeout)',
        T("r.0295"): r'controller is down|AER:.*error|(megaraid|mpt[23]sas|hpsa|smartpqi|aacraid).*(FATAL|fault|firmware)|Controller cache pinned',
        T("r.0296"): r'md/raid.*(Disk failure|not operational)|multipath.*(Failing path|remaining active paths: 0)',
        # RAID 컨트롤러가 커널 로그로 보내는 이벤트. 벤더 도구 없이도 배터리·논리 디스크·구성 디스크 이상을 볼 수 있다
        # (megaraid_sas 는 기본 설정에서 CRITICAL 이상 이벤트를 커널 로그에 남긴다)
        T("r.0297"): r'megaraid_sas.*/0x[0-9a-f]+/(FATAL|CRIT|DEAD|WARN)|(megaraid|mpt[23]sas|hpsa|smartpqi|aacraid).*'
                             r'(battery|bbu|cachevault|degraded|offline|lockup|predictive|rebuild)',
        T("r.0298"): r'thin.*(out of data space|out-of-data-space|read-only mode)|snapshots: Invalidating',
    }
    cnt = {k: sum(1 for l in klog if re.search(v, l, re.I)) for k, v in cats.items()}
    err_sev = "ok"
    if cnt[T("r.0291")] or cnt[T("r.0293")]:
        err_sev = "crit"
    elif (cnt[T("r.0292")] or cnt["SCSI abort/reset"] or cnt[T("r.0295")] or cnt[T("r.0296")]
          or cnt[T("r.0297")] or cnt[T("r.0298")]):
        err_sev = "warn"
    elif cnt[T("r.0294")]:
        err_sev = "caution"
    if err_sev != "ok":
        ERR_WHY = {
            "vmware": vs(T("r.0299"),
                         T("r.0300"),
                         T("r.0301"))
                      + T("r.0302"),
            "local": T("r.0303"),
            "san": T("r.0304"),
        }
        where = "vmware" if kind == "vmware" else ("san" if (kind == "baremetal" and attach == "san") else ("local" if kind == "baremetal" else ""))
        add(err_sev, T("r.0305"), OUT, T("r.0306"),
            " · ".join(T("r.0307").format(k, v) for k, v in cnt.items() if v),
            ERR_WHY.get(where, T("r.0308"))
            + T("r.0309"),
            {"vmware": T("r.0310").format(VMB),
             "local": T("r.0311"),
             "san": T("r.0312")
             }.get(where, T("r.0313").format(OUT))
            + T("r.0314"),
            T("r.0315"))
    else:
        add("ok", T("r.0305"), T("r.0013"), T("r.0316"), T("r.0317"),
            vs(T("r.0318"), T("r.0319"), T("r.0320"))
            if kind == "vmware" else T("r.0321"),
            T("r.0276"), "journalctl -k / dmesg")

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
            (T("r.0322"), fmt((idx_n or 0) / dt_es, 0, " docs/s"), ""),
            (T("r.0323"), fmt(per(idx_t, idx_n), 3, " ms"), ""),
            (T("r.0324"), fmt(thr, 0, " ms"), T("r.0325")),
            (T("r.0326"), fmt(per(q_t, q_n), 1, " ms"), T("r.0327")),
            (T("r.0328"), fmt(per(f_t, f_n), 1, " ms"), T("r.0329")),
            (T("r.0330"), fmt(per(fl_t, fl_n), 0, " ms"), ""),
            (T("r.0331"), fmt(per(rf_t, rf_n), 0, " ms"), ""),
            (T("r.0332"), fmt((100.0 * mg_thr / mg_t) if (mg_t and mg_thr is not None) else None, 0, "%"), T("r.0333")),
            (T("r.0334"), "{} / {}".format(fmt(wr_rej, 0), fmt(se_rej, 0)), T("r.0335")),
            (T("r.0336"), fmt(ip_rej, 0), ""),
        ]
        # merge 스레드 풀 (9.1+, 8.19+): 대기 중인 merge 가 시작·끝 두 시점 모두 쌓여 있으면 디스크가 merge 를 못 따라가는 신호
        mq0, mq1 = dig(n0, "thread_pool", "merge", "queue"), dig(n1, "thread_pool", "merge", "queue")
        mthr = dig(n1, "thread_pool", "merge", "threads")
        if isinstance(mq0, (int, float)) and isinstance(mq1, (int, float)):
            es_rows.append((T("r.0337"), "{} / {}".format(int(mq0), int(mq1)),
                            T("r.0338").format(fmt(mthr, 0))))
            if min(mq0, mq1) >= max(2, mthr or 0):
                add("info", T("r.0339"), T("r.0042"), T("r.0340"),
                    T("r.0341").format(int(mq0), int(mq1), fmt(mthr, 0)),
                    T("r.0342"),
                    T("r.0343"),
                    T("r.0344"))
        if thr:
            es_sev = sev_max(es_sev, "warn")
            add("warn", T("r.0339"), T("r.0042"), T("r.0345"),
                T("r.0346").format(fmt(thr, 0)),
                T("r.0347"),
                T("r.0348"),
                T("r.0349"))
        if (wr_rej or 0) > 0 or ip_rej > 0:
            es_sev = sev_max(es_sev, "warn")
            add("warn", T("r.0339"), T("r.0042"), T("r.0350"),
                "write rejected {} · indexing pressure rejected {}".format(fmt(wr_rej, 0), fmt(ip_rej, 0)),
                T("r.0351"),
                T("r.0352"), "Elasticsearch thread pool / indexing pressure")
        if (se_rej or 0) > 0:
            es_sev = sev_max(es_sev, "caution")
            add("caution", T("r.0339"), T("r.0042"), T("r.0353"), "search rejected {}".format(fmt(se_rej, 0)),
                T("r.0354"), T("r.0355"),
                "Elasticsearch thread pool")
        # 클러스터 상태가 측정을 오염시키는지
        if health and ((health.get("relocating_shards") or 0) + (health.get("initializing_shards") or 0)) > 0:
            add("info", T("r.0339"), T("r.0013"), T("r.0356"),
                "relocating {} · initializing {} · status {}".format(health.get("relocating_shards"), health.get("initializing_shards"), health.get("status")),
                T("r.0357"), T("r.0358"),
                "_cluster/health")
    else:
        add("info", T("r.0339"), T("r.0013"), T("r.0359"), T("r.0360"),
            T("r.0361"),
            T("r.0362"), "-")

    # vSAN OSA hybrid 는 VCF 9.0 에서 향후 중단 예정으로 공지됐다
    if storage == "hybrid":
        add("info", RES_DIM, T("r.0072"), T("r.0363"),
            T("r.0364"),
            T("r.0365"),
            T("r.0366"),
            T("r.0367"))

    # 벡터 검색의 direct IO: page cache 를 거치지 않고 디스크를 직접 읽는다 (9.1 tech preview)
    if re.search(r'-Dvector\.rescoring\.directio=true', rd(S, "es_cmdline")):
        add("info", T("r.0339"), T("r.0013"), T("r.0368"),
            T("r.0369"),
            T("r.0370"),
            T("r.0371"),
            T("r.0372"))

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
        add("warn", T("r.0373"), T("r.0148"), T("r.0374"), T("r.0375").format(fmt(swp_max, 0)),
            T("r.0376"),
            T("r.0377") + (T("r.0378") if is_vmware else ""),
            T("r.0379"))
    if swap_on and mlock is not True:
        s = "caution" if (swappiness is not None and swappiness <= 1) else "warn"
        mem_sevs.append(s)
        add(s, T("r.0373"), T("r.0148"), T("r.0380"),
            T("r.0381").format(len(swaps), sysctl.get("vm.swappiness"), T("r.0082") if mlock is None else mlock),
            T("r.0382").format(T("r.0383") if kind in ("vmware", "vm") else ""),
            T("r.0384"),
            T("r.0379"))
    heap = dig(node_i, "jvm", "mem", "heap_max_in_bytes") or dig(n1 or {}, "jvm", "mem", "heap_max_in_bytes")
    if heap is None:
        m = re.search(r'-Xmx(\d+)([gGmM])', rd(S, "es_cmdline"))
        if m:
            heap = int(m.group(1)) * (1024 ** 3 if m.group(2) in "gG" else 1024 ** 2)
    heap_mb = heap / 1048576.0 if heap else None
    # compressed oops 사용 여부는 ES 가 직접 알려 준다(true/false). 값이 없으면 30GB 를 경계로 본다
    # (Elastic: 대부분 26GB 까지 안전, 일부 시스템은 30GB 까지)
    coops = str(dig(node_i, "jvm", "using_compressed_ordinary_object_pointers") or "").lower()
    heap_big = (coops == "false") if coops in ("true", "false") else bool(heap_mb and heap_mb > 30 * 1024)
    store_b = dig(n1 or {}, "indices", "store", "size_in_bytes")
    if heap_mb and mem_total_mb:
        cache_mb = mem_total_mb - heap_mb
        hr = heap_mb / mem_total_mb
        if hr > 0.5 or heap_big:
            mem_sevs.append("caution")
            add("caution", T("r.0373"), T("r.own_es"), T("r.0385"),
                "heap {} / RAM {} ({:.0f}%)".format(fmt(heap_mb / 1024, 1, "GB"), fmt(mem_total_mb / 1024, 1, "GB"), hr * 100),
                T("r.0386")
                + (T("r.0387") if coops == "false" else ""),
                T("r.0388"),
                T("r.0389"))
        ratio_txt = ""
        if store_b:
            ratio = cache_mb * 1048576.0 / store_b
            ratio_txt = T("r.0390").format(ratio * 100)
        add("info", T("r.0373"), T("r.0013"), T("r.0391").format(fmt(cache_mb / 1024, 1, "GB")),
            "RAM {} − heap {}{}".format(fmt(mem_total_mb / 1024, 1, "GB"), fmt(heap_mb / 1024, 1, "GB"), ratio_txt),
            T("r.0392"),
            T("r.0393"), T("r.0394"))
    mj = [r.get("es_majflt") for r in sysr if r.get("es_majflt") is not None]
    if mj:
        mj95 = pctl(mj, .95)
        if mj95 and mj95 > 200 and SEV_ORDER.get(r_sev, 0) >= SEV_ORDER["caution"]:
            mem_sevs.append("caution")
            add("caution", T("r.0373"), T("r.0042"), T("r.0395"),
                T("r.0396").format(fmt(mj95, 0), SEV_LABEL[r_sev]),
                T("r.0397"),
                T("r.0398"), "/proc/<pid>/stat majflt")
    dirty = [r.get("dirty_mb") for r in sysr if r.get("dirty_mb") is not None]
    if dirty and mem_total_mb and vmax(dirty) > mem_total_mb * 0.10 and SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]:
        mem_sevs.append("caution")
        add("caution", T("r.0373"), T("r.0148"), T("r.0399"),
            T("r.0400").format(fmt(vmax(dirty), 0, "MB"), 100 * vmax(dirty) / mem_total_mb, SEV_LABEL[w_sev]),
            T("r.0401"),
            T("r.0402"),
            "Linux kernel Documentation/admin-guide/sysctl/vm.rst")
    mem_sev = sev_max(*mem_sevs) if mem_sevs else "ok"

    # ═════════════ 6. 설정 ═════════════
    cfg_sevs = []
    # readahead 가 큰 원인은 대개 tuned profile 이나 udev 규칙이다. 둘 다 이미 수집하고 있으니 원인 후보로 붙인다.
    tuned_prof = rd(S, "tuned").strip().split(":")[-1].strip()
    udev_ra = "read_ahead" in rd(S, "udev_rules")
    tuned_hint = ""
    if tuned_prof and tuned_prof.lower() not in ("", "none", "no current active profile"):
        tuned_hint += T("r.0403").format(tuned_prof)
    if udev_ra:
        tuned_hint += T("r.0404")

    # readahead: Elastic 공식 권고 128KiB
    ra_bad = []
    for d in sorted(set(phys + logical)):
        ra = num(topo.attr.get(topo.whole(d), {}).get("queue/read_ahead_kb"))
        if ra is not None and ra > 128:
            ra_bad.append("{}={}KB".format(d, int(ra)))
    if ra_bad:
        s = "warn" if any(int(x.split("=")[1][:-2]) >= 1024 for x in ra_bad) else "caution"
        cfg_sevs.append(s)
        add(s, T("r.0405"), T("r.0148"), T("r.0406"), ", ".join(ra_bad),
            T("r.0407"),
            T("r.0408")
            + tuned_hint,
            T("r.0409"))
    # scheduler
    # Red Hat 권고: 가상 게스트 mq-deadline/none, 고성능 SSD·NVMe none/kyber, 기존 HDD mq-deadline/bfq
    def sched_rec(d):
        if kind != "baremetal":
            return ("mq-deadline", "none"), T("r.0410")
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
            add("caution", T("r.0405"), T("r.0148"), T("r.0411"), ", ".join(sch_bad) + T("r.0412") + ", ".join(sorted(sch_kind)),
                T("r.0413"),
                T("r.0414"),
                T("r.0415"))
        else:
            add("caution", T("r.0405"), T("r.0148"), T("r.0416"), ", ".join(sch_bad),
                T("r.0417").format(
                    vs("vSAN", T("r.0418"), T("r.0419")) if is_vmware else T("r.0420")),
                T("r.0421"),
                "Red Hat 'Monitoring and managing system status and performance' > Setting the disk scheduler")
    # max_map_count
    mmc = num(sysctl.get("vm.max_map_count"))
    if mmc is not None and mmc < 262144:
        cfg_sevs.append("crit")
        add("crit", T("r.0405"), T("r.0148"), T("r.0422"), T("r.0423").format(int(mmc)),
            T("r.0424"),
            T("r.0425"), T("r.0426"))
    elif mmc is not None and mmc < 1048576:
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0148"), T("r.0427"), T("r.0428").format(int(mmc)),
            T("r.0429"), T("r.0430"),
            T("r.0431"))
    # THP
    thp = sysctl.get("thp.enabled", "")
    m = re.search(r'\[(\w+)\]', thp)
    thp_cur = m.group(1) if m else thp
    if thp_cur == "always":
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0148"), "THP(Transparent HugePage) = always", thp,
            T("r.0432"),
            T("r.0433"), T("r.0434"))
    # 파일시스템·마운트
    for pm in path_map:
        m = pm["mount"]
        if not m:
            continue
        opts = m["opts"].split(",")
        if m["fs"] not in ("xfs", "ext4"):
            s = "crit" if m["fs"] in ("nfs", "nfs4", "cifs", "tmpfs") else "caution"
            cfg_sevs.append(s)
            add(s, T("r.0405"), T("r.0148"), T("r.0435").format(m["fs"]), "{} on {}".format(m["mnt"], m["src"]),
                T("r.0436"),
                T("r.0437"), T("r.0438"))
        if "strictatime" in opts:
            cfg_sevs.append("caution")
            add("caution", T("r.0405"), T("r.0148"), T("r.0439"), m["opts"],
                T("r.0440"), T("r.0441"), "mount(8)")
        if "discard" in opts:
            cfg_sevs.append("caution")
            add("caution", T("r.0405"), T("r.0148"), T("r.0442"), m["opts"],
                T("r.0443") + ((" " + vs(
                    T("r.0444"),
                    T("r.0445"),
                    T("r.0446"))) if is_vmware else ""),
                T("r.0447") + (T("r.0448") if is_vmware else ""),
                "mount(8)" + (vs(T("r.0449"), T("r.0450"), T("r.0451")) if is_vmware else ""))
    # 파티션 정렬
    for d in phys:
        for part, (parent, start) in topo.parts.items():
            if parent == d and start % 2048 != 0:
                cfg_sevs.append("caution")
                add("caution", T("r.0405"), T("r.0148"), T("r.0452"), "{} start sector {}".format(part, start),
                    T("r.0453"),
                    T("r.0454"), T("r.0455"))
    # SCSI timeout: vSAN failover 대비
    # 180초 권고는 VMware(open-vm-tools) 기준. bare-metal 로컬 디스크는 커널 기본 30초가 정상이고,
    # SAN 은 multipath 벤더 권고를 따르므로 여기서 판정하지 않는다
    to_bad = ["{}={}s".format(d, topo.attr[d]["device/timeout"]) for d in phys
              if is_vmware and num(topo.attr.get(d, {}).get("device/timeout"), 999) < 60]
    if to_bad:
        cfg_sevs.append("warn")
        add("warn", T("r.0405"), T("r.0148"), vs(T("r.0456"),
                                               T("r.0457"),
                                               T("r.0457")), ", ".join(to_bad),
            vs(T("r.0458"), T("r.0459"), T("r.0460")) + T("r.0461"),
            T("r.0462"),
            T("r.0463"))
    # cgroup I/O 제한
    cg = rd(S, "es_cgroup_io").strip()
    if cg and "max" in cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg):
        cfg_sevs.append("warn")
        add("warn", T("r.0405"), T("r.0148"), T("r.0464"), cg[:200],
            T("r.0465"), T("r.0466"), "cgroup v2 io.max")
    # limits
    lim = rd(S, "es_limits")
    def limv(name):
        m = re.search(r'^' + re.escape(name) + r'\s+(\S+)\s+(\S+)', lim, re.M)
        return m.group(1) if m else None
    nofile = limv("Max open files")
    if nofile and nofile != "unlimited" and num(nofile, 0) < 65535:
        cfg_sevs.append("crit")
        add("crit", T("r.0405"), T("r.0148"), T("r.0467"), "Max open files {}".format(nofile),
            T("r.0468"),
            T("r.0469"), T("r.0470"))
    fdc = num(rd(S, "es_fdcount").strip())
    if fdc and nofile and nofile != "unlimited" and fdc > 0.8 * num(nofile, 1):
        cfg_sevs.append("warn")
        add("warn", T("r.0405"), T("r.0148"), T("r.0471"), "{} / {}".format(int(fdc), nofile),
            T("r.0472"), T("r.0473"), "/proc/<pid>/fd")
    # 디스크 용량 · watermark
    for line in rd(S, "df").splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and any(pm["mount"] and p[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
            use = num(p[4].rstrip("%"), 0)
            lim_pct, hi = wm_high(csettings, (num(p[1], 0) or 0) * 1024)
            lim_pct = lim_pct or 90
            if use >= lim_pct:
                cfg_sevs.append("crit")
                add("crit", T("r.0405"), T("r.own_es"), T("r.0474"), "{} {}% (high {})".format(p[5], int(use), hi),
                    T("r.0475"), T("r.0476"),
                    T("r.0055"))
            elif use >= lim_pct - 10:
                cfg_sevs.append("caution")
                add("caution", T("r.0405"), T("r.own_es"), T("r.0477"), "{} {}% (high {})".format(p[5], int(use), hi),
                    T("r.0478"), T("r.0479"), T("r.0055"))
    # OS와 data가 같은 장치/컨트롤러
    root = mount_for("/", mounts)
    root_phys = topo.physical(topo.kname(root["src"])) if root else []
    share_dev = sorted(set(root_phys) & set(phys))
    if share_dev and not dev_guess:
        cfg_sevs.append("info")
        if is_vmware:
            add("info", T("r.0405"), T("r.0072"), T("r.0480"), ", ".join(share_dev),
                T("r.0481"), T("r.0482"),
                vs(T("r.0483"), "VMware Performance Best Practices for vSphere", "VMware Performance Best Practices for vSphere"))
        else:
            add("info", T("r.0405"), T("r.0148") if kind == "baremetal" else OUT,
                T("r.0484").format(T("r.0485") if kind == "baremetal" else T("r.0410")), ", ".join(share_dev),
                T("r.0486"),
                T("r.0487").format(T("r.0488") if kind == "baremetal" else T("r.0410")),
                T("r.0489"))
    elif is_vmware:
        hosts = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
        rhosts = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
        if hosts and set(hosts) <= set(rhosts):
            add("info", T("r.0405"), T("r.0072"), T("r.0490"), "controller {}".format(", ".join(hosts)),
                T("r.0491"),
                T("r.0492"), "VMware 'Performance Best Practices for vSphere' > PVSCSI")
    # LVM linear over multiple PVs
    for d in logical:
        if d.startswith("dm-") and len(topo.slaves.get(d, [])) > 1:
            tbl = rd(S, "dmsetup_table")
            dmn = topo.attr.get(d, {}).get("dm/name", "")
            lines = [l for l in tbl.splitlines() if l.startswith(dmn + ":")]
            if lines and all(" linear " in l for l in lines):
                cfg_sevs.append("info")
                add("info", T("r.0405"), T("r.0148"), T("r.0493"),
                    "{} ← {}".format(dmn, ", ".join(topo.slaves[d])),
                    T("r.0494"),
                    T("r.0495"), "LVM lvcreate(8)")
    # iostats 비활성. 이러면 측정 자체가 무의미
    for d in phys:
        if topo.attr.get(d, {}).get("queue/iostats") == "0":
            cfg_sevs.append("warn")
            add("warn", T("r.0405"), T("r.0148"), T("r.0496").format(d),
                "/sys/block/{}/queue/iostats = 0".format(d),
                T("r.0497"),
                T("r.0498"), "Linux block layer sysfs")
    # writeback throttling: 커널이 쓰기를 의도적으로 억제
    wbt = [(d, num(topo.attr.get(d, {}).get("queue/wbt_lat_usec"))) for d in phys]
    wbt_on = [(d, v) for d, v in wbt if v and v > 0]
    if wbt_on and SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]:
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0148"), T("r.0499"),
            ", ".join("{} wbt_lat_usec={}".format(d, int(v)) for d, v in wbt_on),
            T("r.0500"),
            T("r.0501"),
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
                add("caution", T("r.0405"), T("r.0148"), T("r.0502").format(cpu_w),
                    T("r.0503").format(cpu_w, share, cpu_w, len(tot)),
                    T("r.0504"),
                    T("r.0505") + (T("r.0506") if is_vmware else
                    T("r.0507") if kind == "baremetal" else
                    T("r.0508").format(OUT)),
                    "/proc/interrupts")
    # mmap 여유 (ES는 segment를 mmap으로 연다)
    mapc = num(rd(S, "es_mapcount").strip())
    if mapc and mmc:
        use = 100.0 * mapc / mmc
        if use > 60:
            cfg_sevs.append("warn" if use > 80 else "caution")
            add("warn" if use > 80 else "caution", T("r.0405"), T("r.0148"), T("r.0509"),
                T("r.0510").format(int(mapc), int(mmc), use),
                T("r.0511"),
                T("r.0512"),
                T("r.0431"))
    # ES 로그에 남은 디스크 관련 메시지
    eslog = [l for l in rd(S, "es_log").splitlines() if l.strip() and not l.startswith("#FILE")]
    if eslog:
        pats = [(T("r.0513"), r'now throttling indexing'), (T("r.0514"), r'disk watermark|flood stage'),
                (T("r.0515"), r'failed to flush|failed to write'), (T("r.0516"), r'Too many open files'),
                (T("r.0517"), r'overhead, spent|\[gc\]\['), (T("r.0518"), r'translog')]
        hit = [(n, sum(1 for l in eslog if re.search(pt, l, re.I))) for n, pt in pats]
        hit = [(n, c) for n, c in hit if c]
        if hit:
            sev = "warn" if any(n in (T("r.0513"), T("r.0514"), T("r.0515"), T("r.0516")) for n, _ in hit) else "info"
            cfg_sevs.append("info")
            add(sev, T("r.0339"), T("r.0042"), T("r.0519"),
                " · ".join(T("r.0307").format(n, c) for n, c in hit),
                T("r.0520"),
                T("r.0521"),
                T("r.0522"))

    # ── 디스크와 직결되는 인덱스 설정 (명시적으로 바꾼 인덱스만 응답에 들어온다) ──
    idx_set = rjson(S, "es_idx_settings.json") or {}
    def idx_vals(key):
        """key 를 명시적으로 설정한 인덱스를 {인덱스: 값} 으로.
        수집은 중첩 응답(settings.index.translog.durability)이고, 예전 번들은 flat 키("index.translog.durability")다"""
        out = {}
        for name, blk in idx_set.items():
            st = (blk or {}).get("settings") or {}
            v = st.get(key) if key in st else dig(st, *key.split("."))
            if v is not None and not isinstance(v, dict):
                out[name] = str(v)
        return out

    # translog durability: 디스크 지연이 인덱싱 지연으로 이어지는 경로를 설명하는 핵심 설정
    dur_async = {k: v for k, v in idx_vals("index.translog.durability").items() if str(v).lower() == "async"}
    if dur_async:
        add("info", T("r.0051"), T("r.0013"), T("r.0523"),
            T("r.0524").format(len(dur_async), ", ".join(sorted(dur_async)[:6])),
            T("r.0525"),
            T("r.0526"),
            T("r.0527"))
    else:
        add("info", T("r.0051"), T("r.0013"), T("r.0528"),
            T("r.0529") if (idx_set or rd(S, "es_idx_settings.json").strip() == "{}") else T("r.0530"),
            T("r.0531"),
            T("r.0532"),
            T("r.0533"))

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
                ttl = T("r.0534")
                tail = T("r.0535")
            else:
                ttl = T("r.0536")
                tail = (T("r.0537").format(
                    T("r.0538") if unsure else T("r.0539")))
            add("caution", T("r.0051"), T("r.own_es"), ttl,
                T("r.0540").format(
                    len([k for k, v in mtc.items() if str(v) == "1"]),
                    T("r.0541") + ", ".join(sorted(not_one)[:4]) + ")" if not_one else "",
                    "Guest" if kind in ("vmware", "vm") else T("r.0542"), ", ".join(rot) or T("r.0543")),
                T("r.0544"),
                T("r.0545") + tail,
                T("r.0546"))
    elif mtc:
        add("info", T("r.0051"), T("r.0013"), T("r.0547"),
            ", ".join("{}={}".format(k, v) for k, v in sorted(mtc.items())[:6]),
            T("r.0548"),
            T("r.0549"), T("r.0550"))

    # store type / preload
    st_type = idx_vals("index.store.type")
    if st_type:
        add("info", T("r.0051"), T("r.0013"), T("r.0551"),
            ", ".join("{}={}".format(k, v) for k, v in sorted(st_type.items())[:6]),
            T("r.0552"),
            T("r.0553"),
            T("r.0554"))

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
            add(s_, T("r.0128"), T("r.0042") if kind != "baremetal" else OUT, T("r.0555"),
                T("r.0556").format(fmt(A["flush_ps"], 1), fmt(fms, 2, "ms")),
                T("r.0557"),
                (T("r.0558")).format(OUT),
                T("r.0559"))

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
        add("caution", T("r.0108"), T("r.0148"), T("r.0560"),
            T("r.0561").format(
                fmt(tot_mb, 0, "MB"), 100.0 * es_mb / tot_mb,
                ", ".join("{}(pid {}) {}".format(x["comm"], x["pid"], fmt(x["r_mb"] + x["w_mb"], 0, "MB")) for x in others[:4])),
            T("r.0562"),
            T("r.0563"),
            T("r.0564"))

    # inode
    for line in rd(S, "df_i").splitlines()[1:]:
        p_ = line.split()
        if len(p_) >= 6 and any(pm["mount"] and p_[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
            iu = num(p_[4].rstrip("%"))
            if iu is not None and iu >= 80:
                s_ = "warn" if iu >= 90 else "caution"
                cfg_sevs.append(s_)
                add(s_, T("r.0405"), T("r.0148"), T("r.0565"), "{} inode {}%".format(p_[5], int(iu)),
                    T("r.0566"),
                    T("r.0567"), "df -i")

    # 마운트 옵션: 데이터 안전·성능에 직접 닿는 것
    for pm in path_map:
        m_ = pm["mount"]
        if not m_:
            continue
        o = m_["opts"].split(",")
        if "nobarrier" in o or "barrier=0" in o:
            cfg_sevs.append("warn")
            add("warn", T("r.0405"), T("r.0148"), T("r.0568"), "{} {}".format(m_["mnt"], m_["opts"]),
                T("r.0569"),
                T("r.0570"), "mount(8), ext4(5)")
        if "sync" in o or "dirsync" in o:
            cfg_sevs.append("warn")
            add("warn", T("r.0405"), T("r.0148"), T("r.0571"), "{} {}".format(m_["mnt"], m_["opts"]),
                T("r.0572"),
                T("r.0573"), "mount(8)")
        if "data=journal" in o:
            cfg_sevs.append("info")
            add("info", T("r.0405"), T("r.0148"), T("r.0574"), "{} {}".format(m_["mnt"], m_["opts"]),
                T("r.0575"), T("r.0576"), "ext4(5)")

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
            add(s_, T("r.0405"), T("r.0148"), T("r.0577").format(dp),
                T("r.0578").format(nm, dp, mp, mode),
                T("r.0579"),
                T("r.0580"),
                "lvmthin(7), dmsetup status")
    if "snapshot-origin" in data_targets or any("snapshot" == t for t in data_targets):
        cfg_sevs.append("warn")
        add("warn", T("r.0405"), T("r.0148"), T("r.0581"),
            T("r.0582").format(", ".join(sorted(data_targets))),
            T("r.0583"),
            T("r.0584"),
            T("r.0585"))
    if "crypt" in data_targets:
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0013"), T("r.0586"), T("r.0587"),
            T("r.0588"),
            T("r.0589"), "cryptsetup(8)")
    if data_targets & {"cache", "writecache"}:
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0013"), T("r.0590"), T("r.0591") + ", ".join(sorted(data_targets & {"cache", "writecache"})),
            T("r.0592"),
            T("r.0593"), "lvmcache(7)")

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
        add("caution", T("r.0405"), T("r.0148"), T("r.0594"), ", ".join(swap_hit),
            T("r.0595"),
            T("r.0596"), T("r.0379"))
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
        add("caution", T("r.0405"), T("r.own_es"), T("r.0597"), ", ".join(same_repo),
            T("r.0598"),
            T("r.0599"), T("r.0600"))
    if logs_p and (phys_of_path(logs_p) & phys_set) and not dev_guess:
        cfg_sevs.append("info")
        add("info", T("r.0405"), T("r.0013"), T("r.0601"), logs_p,
            T("r.0602"),
            T("r.0603"), "Elasticsearch path settings")

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
            add("crit", T("r.0305"), OUT, T("r.0604").format(d, st), "/sys/block/{}/device/state = {}".format(d, st),
                T("r.0605"),
                T("r.0606"), "SCSI sysfs device/state")
        if iotmo:
            err_sev = sev_max(err_sev, "caution")
            add("caution", T("r.0305"), OUT, T("r.0607").format(d, iotmo), T("r.0608").format(iotmo, ioerr),
                T("r.0609"),
                T("r.0610"), "SCSI sysfs device/iotmo_cnt")
        elif ioerr and cnt.get(T("r.0291")):
            err_sev = sev_max(err_sev, "caution")
            add("caution", T("r.0305"), OUT, T("r.0611").format(d, ioerr), T("r.0612").format(ioerr, cnt.get(T("r.0291"))),
                T("r.0613"),
                T("r.0614"), "SCSI sysfs device/ioerr_cnt")
        ae = topo.aer.get(d)
        if ae and ((ae.get("fatal") or 0) + (ae.get("nonfatal") or 0)) > 0:
            err_sev = sev_max(err_sev, "warn")
            add("warn", T("r.0305"), OUT, T("r.0615").format(d),
                "{} · fatal {} · nonfatal {} · correctable {}".format(ae.get("pci"), ae.get("fatal"), ae.get("nonfatal"), ae.get("cor")),
                T("r.0616"),
                T("r.0617"), T("r.0618"))
    for c_ in sorted(set(nvme_ctrl(d) for d in phys if nvme_ctrl(d))):
        stn = sto["nvme"].get(c_, {}).get("state")
        if stn and stn != "live":
            err_sev = sev_max(err_sev, "warn")
            add("warn", T("r.0305"), OUT, T("r.0619").format(c_, stn), "/sys/class/nvme/{}/state".format(c_),
                T("r.0620"), T("r.0621"), "Linux nvme sysfs")
    for md_ in [k for k in data_chain if k.startswith("md")]:
        mm = num(topo.attr.get(md_, {}).get("md/mismatch_cnt"))
        if mm:
            cfg_sevs.append("info")
            add("info", T("r.0405"), T("r.0013"), T("r.0622").format(md_), "mismatch_cnt={} · sync_action={}".format(int(mm), topo.attr.get(md_, {}).get("md/sync_action")),
                T("r.0623"),
                T("r.0624"), "Linux md(4) mismatch_cnt")

    # 커널 로그의 RAID 컨트롤러 이벤트 (벤더 도구 없이 보는 배터리·논리 디스크·구성 디스크 이상)
    raid_ev = [l for l in klog if re.search(cats[T("r.0297")], l, re.I)]
    if raid_ev:
        s_ = "warn" if any(re.search(r'fail|degrad|offline|FATAL|DEAD|replace|predictive|pinned', l, re.I) for l in raid_ev) else "caution"
        err_sev = sev_max(err_sev, s_)
        add(s_, T("r.0305"), OUT, T("r.0625").format(len(raid_ev)),
            " / ".join(re.sub(r'^\S+\s+\S+\s+kernel:\s*', '', l)[:160] for l in raid_ev[-3:]),
            T("r.0626"),
            T("r.0627"),
            T("r.0628"))

    cfg_sev = sev_max(*cfg_sevs) if cfg_sevs else "ok"

    # ═════════════ 7. 플랫폼 자원 (VMware 자원 / 가상화 자원 / 하드웨어) ═════════════
    vm_sevs = []
    drivers = sorted(set(topo.hostdrv.get(topo.scsihost.get(d), "?") for d in phys if topo.scsihost.get(d)))
    if any(dr.startswith("nvme") for dr in phys):
        drivers.append("nvme")
    if is_vmware:
        if any(dr in ("mptspi", "mptsas", "mpt2sas", "mpt3sas", "ata_piix", "ahci") for dr in drivers):
            vm_sevs.append("caution")
            add("caution", T("r.0086"), T("r.0072"), T("r.0629"), ", ".join(drivers),
                T("r.0630"),
                T("r.0631"),
                T("r.0632"))
        tools = virt.get("tools_version", "absent")
        if tools == "absent":
            vm_sevs.append("caution")
            add("caution", T("r.0086"), T("r.0148"), T("r.0633"), T("r.0634"),
                T("r.0635"),
                T("r.0636"), "VMware open-vm-tools")
        def mb(x):
            m = re.search(r'(-?\d+)', x or "")
            return int(m.group(1)) if m else None
        balloon, hswap = mb(virt.get("stat_balloon")), mb(virt.get("stat_swap"))
        memres, memlim = mb(virt.get("stat_memres")), mb(virt.get("stat_memlimit"))
        cpulim = mb(virt.get("stat_cpulimit"))
        if balloon:
            vm_sevs.append("warn")
            add("warn", T("r.0086"), T("r.0072"), T("r.0637"), "{} MB".format(balloon),
                T("r.0638"),
                T("r.0639"), T("r.0640"))
        if hswap:
            vm_sevs.append("crit")
            add("crit", T("r.0086"), T("r.0072"), T("r.0641"), "{} MB".format(hswap),
                T("r.0642"),
                T("r.0643"), T("r.0640"))
        if memres is not None and mem_total_mb and memres < mem_total_mb * 0.95:
            vm_sevs.append("caution")
            add("caution", T("r.0086"), T("r.0072"), T("r.0644"),
                T("r.0645").format(memres, int(mem_total_mb)),
                T("r.0646"),
                T("r.0647"), "VMware 'Performance Best Practices for vSphere' > memory")
        if memlim is not None and 0 < memlim < mem_total_mb * 0.95 and memlim < 4000000:
            vm_sevs.append("warn")
            add("warn", T("r.0086"), T("r.0072"), T("r.0648"), "{} MB".format(memlim),
                T("r.0649"), T("r.0650"), T("r.0651"))
        if cpulim is not None and 0 < cpulim < 4000000:
            vm_sevs.append("caution")
            add("caution", T("r.0086"), T("r.0072"), T("r.0652"), "{} MHz".format(cpulim),
                T("r.0653") + vs(T("r.0654"), "", T("r.0654")) + T("r.0655"), T("r.0656"), T("r.0651"))
    # ── 하드웨어 (bare-metal): 장치 자체의 상태 ─────────────────────────────
    HW = {"nvme": [], "md": [], "smart": [], "fc": [], "governor": None, "raid": []}
    if kind == "baremetal":
        # NVMe 온도: hwmon temp1_max 는 컨트롤러에 현재 설정된 과열 임계값(기본값은 경고 온도 WCTEMP),
        # temp1_crit 는 위험 온도(CCTEMP).
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
                add("warn", RES_DIM, OUT, T("r.0657").format(c),
                    T("r.0658").format(row["temp"] or 0, fmt(row["tmax"], 0, "°C"), fmt(row["tcrit"], 0, "°C"),
                                                                 T("r.0659") if row["alarm"] == "1" else ""),
                    T("r.0660"),
                    T("r.0661"),
                    T("r.0662"))
            elif row["temp"] and row["tmax"] and row["temp"] >= row["tmax"] - 5:
                vm_sevs.append("caution")
                add("caution", RES_DIM, OUT, T("r.0663").format(c),
                    T("r.0664").format(row["temp"], row["tmax"]),
                    T("r.0665"),
                    T("r.0666"), T("r.0667"))
            def gts(x):
                m_ = re.search(r'([\d.]+)\s*GT/s', x or "")
                return float(m_.group(1)) if m_ else None
            cs, ms_ = gts(row["ls"]), gts(row["mls"])
            cw, mw = num(row["lw"]), num(row["mlw"])
            if (cs and ms_ and cs < ms_) or (cw and mw and cw < mw):
                vm_sevs.append("caution")
                add("caution", RES_DIM, OUT, T("r.0668").format(c),
                    T("r.0669").format(row["ls"] or "-", row["lw"] or "-", row["mls"] or "-", row["mlw"] or "-"),
                    T("r.0670"),
                    T("r.0671").format(c),
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
                add("warn", RES_DIM, OUT, T("r.0672").format(md_["name"]),
                    "{} {} · {}{}".format(md_["name"], md_["level"], md_["status"], " · " + md_["op"] if md_["op"] else ""),
                    T("r.0673")
                    + (T("r.0674") if mine else ""),
                    T("r.0675"),
                    "/proc/mdstat")
            elif md_["op"]:
                s_ = "caution" if (mine and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]) else "info"
                vm_sevs.append(s_)
                add(s_, RES_DIM, T("r.0013") if s_ == "info" else OUT, T("r.0676").format(md_["name"], md_["op"].split()[0]),
                    "{} {} · {}".format(md_["name"], md_["level"], md_["op"]),
                    T("r.0677")
                    + (T("r.0674") if mine else ""),
                    T("r.0678"),
                    "/proc/mdstat")
        # CPU governor
        gov, gdrv = (virt.get("cpu_governor") or "").strip(), (virt.get("cpu_scaling_driver") or "").strip()
        HW["governor"] = (gov, gdrv)
        if gov in ("powersave", "conservative", "ondemand"):
            s_ = "info" if (gov == "powersave" and gdrv in ("intel_pstate", "amd-pstate", "amd-pstate-epp")) else "caution"
            vm_sevs.append(s_)
            add(s_, RES_DIM, T("r.0148"), T("r.0679").format(gov),
                "scaling_governor={} · driver={} · tuned={}".format(gov, gdrv or "-", tuned_prof or "-"),
                T("r.0680")
                + (T("r.0681") if s_ == "info" else ""),
                T("r.0682"),
                T("r.0683"))
        # FC 포트 상태
        HW["fc"] = sto["fc"]
        down = [f_ for f_ in sto["fc"] if f_["state"] and f_["state"].lower() not in ("online",)]
        if down and attach == "san":
            vm_sevs.append("warn")
            add("warn", RES_DIM, T("r.0089"), T("r.0684"),
                ", ".join("{} {} ({})".format(f_["host"], f_["state"], f_["speed"]) for f_ in down),
                T("r.0685"),
                T("r.0686"),
                "/sys/class/fc_host/*/port_state")
        # 하드웨어 RAID 컨트롤러 (storcli·perccli / ssacli / arcconf 조회 결과)
        data_devs = set(phys)
        for r in hwraid:
            HW["raid"].append(r)
            for c in r["ctrl"]:
                st = (c.get("status") or "").strip()
                if st and st.lower() not in ("ok", "optimal", "okay"):
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, T("r.0687").format(c["name"]), "Controller Status: {}".format(st),
                        T("r.0688"),
                        T("r.0689"), T("r.0690").format(r["tool"]))
                bt = (c.get("battery") or "").strip()
                if bt and not re.search(r'^(ok|optimal|ready|zmm optimal|not present|not installed|absent|zmm not installed)$', bt, re.I):
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, T("r.0691"), "{}: {}".format(c["name"], bt),
                        T("r.0692"),
                        T("r.0693"), T("r.0690").format(r["tool"]))
                cs = (c.get("cache") or "").strip()
                if cs and cs.upper() != "OK":
                    vm_sevs.append("warn")
                    add("warn", RES_DIM, OUT, T("r.0694"), "{}: Cache Status {}".format(c["name"], cs),
                        T("r.0695"),
                        T("r.0696"), T("r.0690").format(r["tool"]))
            for v in r["vds"]:
                mine = v.get("dev") in data_devs
                where = "ES data" if mine else T("r.0697")
                st = v.get("state") or "-"
                if not v.get("ok") and v.get("state"):
                    s_ = ("crit" if re.search(r'fail|offline|OfLn', st, re.I) else "warn") if mine else "caution"
                    vm_sevs.append(s_)
                    add(s_, RES_DIM, OUT, T("r.0698").format(v.get("dev") or "VD " + str(v.get("id")), where),
                        T("r.0699").format(v.get("level") or "-", st,
                                                        ", ".join("{} {}".format(p_["id"], p_["state"]) for p_ in v.get("pds", [])[:8]) or "-"),
                        T("r.0700"),
                        T("r.0701"),
                        T("r.0690").format(r["tool"]))
                if mine and v.get("wb") is False:
                    hddish = (v.get("media") or storage) == "hdd"
                    s_ = "warn" if (hddish or SEV_ORDER.get(w_sev, 0) >= SEV_ORDER["caution"]) else "info"
                    vm_sevs.append(s_)
                    ini = v.get("cache_init") or ""
                    fell = "back" in ini.lower()
                    add(s_, RES_DIM, OUT if s_ != "info" else T("r.0013"),
                        T("r.0702") + (T("r.0703") if fell else ""),
                        T("r.0704").format(v.get("dev"), v.get("level") or "", v.get("cache_cur") or "-",
                                                               T("r.0705") + ini if ini else "", fmt(A["w_await_p95"], 2, "ms")),
                        (T("r.0706") if fell else
                         T("r.0707"))
                        + (T("r.0708") if hddish else
                           T("r.0709")),
                        T("r.0710"),
                        T("r.0711").format(r["tool"]))
                lvl = (v.get("level") or "").upper().replace(" ", "")
                if mine and re.search(r'RAID(5|6|50|60)|^5$|^6', lvl + "|" + str(v.get("level"))):
                    vm_sevs.append("info")
                    add("info" if SEV_ORDER.get(w_sev, 0) < SEV_ORDER["caution"] else "caution", RES_DIM, T("r.0013") if SEV_ORDER.get(w_sev, 0) < SEV_ORDER["caution"] else OUT,
                        T("r.0712").format(v.get("level")),
                        T("r.0713").format(v.get("dev"), fmt(A["w_await_p95"], 2, "ms")),
                        T("r.0714"),
                        T("r.0715"),
                        T("r.0716"))
            for desc, sv in r["pds_bad"]:
                vm_sevs.append(sv)
            if r["pds_bad"]:
                sv = sev_max(*[x[1] for x in r["pds_bad"]])
                add(sv, RES_DIM, OUT, T("r.0717"),
                    " · ".join(x[0] for x in r["pds_bad"][:8]),
                    T("r.0718"),
                    T("r.0719"),
                    T("r.0690").format(r["tool"]))
            if r["bg"]:
                vm_sevs.append("info")
                add("caution" if SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"] else "info", RES_DIM, T("r.0013"),
                    T("r.0720"), " · ".join(r["bg"][:6]),
                    T("r.0721"),
                    T("r.0722"), T("r.0690").format(r["tool"]))
        for rdv in topo.raiddev:               # 커널 raid_class (mpt*sas IR 볼륨 등)
            st = (rdv.get("state") or "").lower()
            if st and st not in ("active", "optimal", "ok", "unknown"):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, T("r.0723").format(rdv.get("dev") or rdv["name"]),
                    "level {} · state {} · resync {}".format(rdv.get("level"), rdv.get("state"), rdv.get("resync")),
                    T("r.0724"), T("r.0725"),
                    "/sys/class/raid_devices")
        for h_, at in topo.hostattr.items():
            if str(at.get("fw_crash_state", "0")).strip() not in ("0", ""):
                vm_sevs.append("warn")
                add("warn", RES_DIM, OUT, T("r.0726").format(h_), "fw_crash_state={}".format(at["fw_crash_state"]),
                    T("r.0727"),
                    T("r.0728"), "megaraid_sas sysfs")
        raid_data = any(devcls.get(d, {}).get("raid") for d in phys)
        unparsed = [a.split(None, 1)[1] for a in raid_absent if a.startswith("#UNPARSED")]
        raid_absent = [a for a in raid_absent if not a.startswith("#UNPARSED")]
        if raid_data and not hwraid and unparsed:
            add("info", RES_DIM, T("r.0013"), T("r.0729"),
                T("r.0730").format(", ".join(unparsed)),
                T("r.0731"),
                T("r.0732"), "Broadcom StorCLI2 User Guide")
        if raid_data and not hwraid and raid_absent:
            add("info", RES_DIM, T("r.0013"), T("r.0733"),
                T("r.0734").format(", ".join(raid_absent)),
                T("r.0735"),
                T("r.0736"), T("r.0737") + ", ".join(sorted(set(c["drv"] for c in devcls.values() if c["raid"]))))
        if unsure and storage_auto and attach == "local":
            add("info", RES_DIM, T("r.0013"), T("r.0738"),
                ", ".join("{}: {}".format(d, devcls[d]["why"]) for d in unsure),
                T("r.0739").format(STORAGE_LABEL[storage]),
                T("r.0740"),
                T("r.0741"))
    # SMART (--smart 로 수집한 경우). 플랫폼과 무관하게 읽지만 가상 디스크에서는 대개 의미가 없다
    smart_txt = rd(S, "smart")
    if "#SMARTCTL_ABSENT" in smart_txt and kind == "baremetal" and attach == "local" and \
            not all(devcls.get(d, {}).get("raid") for d in phys):
        add("info", RES_DIM, T("r.0013"), T("r.0742"), T("r.0743"),
            T("r.0744"),
            T("r.0745"), "smartctl")
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
                    cur["sev"] = "crit"; cur["notes"].append(T("r.0746") + m_.group(2))
            m_ = re.match(r'^\s*\d+\s+(Reallocated_Sector_Ct|Current_Pending_Sector|Offline_Uncorrectable|Reported_Uncorrect)\s+.*\s(\d+)\s*$', line)
            if m_ and int(m_.group(2)) > 0:
                cur["notes"].append("{} {}".format(m_.group(1), m_.group(2)))
                cur["sev"] = sev_max(cur["sev"], "caution" if m_.group(1) == "Reallocated_Sector_Ct" else "warn")
            m_ = re.match(r'^Critical Warning:\s*(0x[0-9a-fA-F]+)', line)
            if m_ and int(m_.group(1), 16) != 0:
                cur["notes"].append("NVMe Critical Warning " + m_.group(1)); cur["sev"] = sev_max(cur["sev"], "warn")
            m_ = re.match(r'^Percentage Used:\s*(\d+)%', line)
            if m_ and int(m_.group(1)) >= 90:
                cur["notes"].append(T("r.0747").format(m_.group(1))); cur["sev"] = sev_max(cur["sev"], "caution")
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
            add(sv, RES_DIM, OUT if kind == "baremetal" else T("r.0013"), T("r.0748"),
                " · ".join("{}: {}".format(x["dev"], ", ".join(x["notes"])) for x in bad),
                T("r.0749"),
                T("r.0750"),
                "smartctl -H -A (smartmontools)")

    steal = [r.get("steal") for r in sysr if r.get("steal") is not None]
    st95 = pctl(steal, .95)
    if st95 is not None and st95 >= 5:
        s = "warn" if st95 >= 10 else "caution"
        vm_sevs.append(s)
        add(s, RES_DIM, OUT, T("r.0751"), T("r.0752").format(fmt(st95, 1, "%"), fmt(vmax(steal), 1, "%")),
            T("r.0753") + (
                vs(T("r.0754"),
                   T("r.0755"),
                   T("r.0756")) if is_vmware else
                T("r.0755")),
            T("r.0757").format(OUT), "Linux /proc/stat steal")
    vm_sev = sev_max(*vm_sevs) if vm_sevs else ("ok" if (is_vmware or kind == "baremetal") else "na")

    # ═════════════ 8. 네트워크 (보조) ═════════════
    net_sev = "ok"
    netinfo = [l.split("|") for l in rd(S, "net").splitlines() if l.startswith("IF|")]
    for p in netinfo:
        if is_vmware and "e1000" in p[2]:
            net_sev = sev_max(net_sev, "caution")
            add("caution", T("r.0758"), T("r.0072"), T("r.0759"), "{} {}".format(p[1], p[2]),
                T("r.0760"),
                T("r.0761"), T("r.0762"))
    drops = {k: v for k, v in nets.items() if (v["rx_drop"] + v["tx_drop"] + v["rx_err"] + v["tx_err"]) > 0}
    if drops:
        net_sev = sev_max(net_sev, "caution")
        add("caution", T("r.0758"), T("r.0148"), T("r.0763"),
            ", ".join("{} drop {} err {}".format(k, v["rx_drop"] + v["tx_drop"], v["rx_err"] + v["tx_err"]) for k, v in drops.items()),
            T("r.0764"),
            T("r.0765") + (T("r.0766") if is_vmware else
            T("r.0767") if kind == "baremetal" else T("r.0768").format(OUT)), "ethtool(8)")
    rt = [r.get("retrans_pct") for r in sysr if r.get("retrans_pct") is not None]
    if rt and pctl(rt, .95) >= 1.0:
        net_sev = sev_max(net_sev, "caution")
        add("caution", T("r.0758"), T("r.0042"), T("r.0769"), "p95 {}".format(fmt(pctl(rt, .95), 2, "%")),
            T("r.0770"), T("r.0771"), "/proc/net/snmp")
    if is_vmware and VMBK != "ds":
        add("info", T("r.0758"), T("r.0013"), T("r.0772") + ("" if VMBK == "vsan" else T("r.0773")),
            T("r.0774"),
            T("r.0775"),
            T("r.0776"), T("r.0777"))

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
            add("caution", T("r.0128"), T("r.0042"), T("r.0778"),
                T("r.0779").format(worst[0], worst[3], fmt(worst[2], 1, "ms")),
                T("r.0780"),
                T("r.0781").format(
                    "·" + VMB if is_vmware else (T("r.0782") if kind == "baremetal" else "")), T("r.0783"))

    # ═════════════ Best practice 대조표 (통과 항목 포함 전체) ═════════════
    BP = []
    def bp(cat, item, rec, cur, st, src):
        BP.append((cat, item, rec, cur, st, src))
    es_devs = sorted(set(phys + logical))
    ras = [(d, num(topo.attr.get(topo.whole(d), {}).get("queue/read_ahead_kb"))) for d in es_devs]
    ras = [(d, v) for d, v in ras if v is not None]
    if ras:
        mx = max(v for _, v in ras)
        bp(T("r.0784"), "readahead", T("r.0785"), ", ".join("{} {}KB".format(d, int(v)) for d, v in ras),
           "ok" if mx <= 128 else ("warn" if mx >= 1024 else "caution"), T("r.0786"))
    schs = []
    for d in phys:
        m_ = re.search(r'\[(\S+)\]', topo.attr.get(d, {}).get("queue/scheduler", ""))
        if m_:
            schs.append((d, m_.group(1)))
    if schs and kind == "baremetal":
        recs = sorted(set("{} {}".format(sched_rec(d)[1], "/".join(sched_rec(d)[0][:2])) for d, _ in schs))
        bp(T("r.0784"), T("r.0787"), " · ".join(recs), ", ".join("{} {}".format(d, v) for d, v in schs),
           "caution" if sch_bad else "ok", T("r.0788"))
    elif schs:
        bp(T("r.0784"), T("r.0787"), T("r.0789"), ", ".join("{} {}".format(d, v) for d, v in schs),
           "caution" if any(v in ("cfq", "bfq") for _, v in schs) else "ok", T("r.0788"))
    ios = [topo.attr.get(d, {}).get("queue/iostats") for d in phys]
    if any(ios):
        bp(T("r.0784"), T("r.0790"), T("r.0791"), ", ".join(x or "-" for x in ios), "warn" if "0" in ios else "ok", T("r.0792"))
    tos = [num(topo.attr.get(d, {}).get("device/timeout")) for d in phys]
    tos = [t for t in tos if t is not None]
    if is_vmware:
        bp(T("r.0784"), T("r.0793"), T("r.0794"), ", ".join("{}s".format(int(t)) for t in tos) or T("r.0795"),
           ("warn" if min(tos) < 60 else "ok") if tos else "na", T("r.0796"))
    qds = [topo.attr.get(d, {}).get("device/queue_depth") for d in phys if topo.attr.get(d, {}).get("device/queue_depth")]
    if is_vmware:
        bp(T("r.0784"), T("r.0797"), T("r.0798"), ", ".join(qds) or "-",
           ("warn" if (qratio or 0) >= 0.8 else "ok") if qds else "na", T("r.0799"))
    elif qds:
        bp(T("r.0784"), "LUN queue_depth" if attach == "san" else T("r.0800"), T("r.0801"),
           T("r.0802").format(", ".join(qds), fmt((qratio or 0) * 100, 0, "%") if qratio is not None else "-"),
           "warn" if (qratio or 0) >= 0.8 else "ok", T("r.0803"))
    mis = [(pt, st) for pt, (par, st) in topo.parts.items() if par in phys and st % 2048]
    bp(T("r.0784"), T("r.0804"), T("r.0805"), T("r.0806") + ", ".join(p_ for p_, _ in mis) if mis else T("r.0807"),
       "caution" if mis else "ok", T("r.0808"))
    for pm in path_map:
        m_ = pm["mount"]
        if not m_:
            continue
        o = m_["opts"].split(",")
        bp(T("r.0809"), T("r.0810").format(m_["mnt"]), T("r.0811"), m_["fs"],
           "ok" if m_["fs"] in ("xfs", "ext4") else "crit", T("r.0786"))
        bp(T("r.0809"), T("r.0812").format(m_["mnt"]), T("r.0813"), next((x for x in o if "atime" in x), T("r.0814")),
           "caution" if "strictatime" in o else "ok", "mount(8)")
        bp(T("r.0809"), "online discard ({})".format(m_["mnt"]), T("r.0815"), T("r.0816") if "discard" in o else T("r.0817"),
           "caution" if "discard" in o else "ok", "mount(8)")
        nb = "nobarrier" in o or "barrier=0" in o
        bp(T("r.0809"), T("r.0818").format(m_["mnt"]), T("r.0819"), T("r.0817") if nb else T("r.0816"),
           "warn" if nb else "ok", "mount(8), ext4(5)")
        sy = "sync" in o or "dirsync" in o
        bp(T("r.0809"), T("r.0820").format(m_["mnt"]), T("r.0821"), T("r.0816") if sy else T("r.0817"), "warn" if sy else "ok", "mount(8)")
    fst = rd(S, "fstrim").split()
    bp(T("r.0809"), "fstrim.timer", vs(T("r.0822"), T("r.0823"),
                                        T("r.0824")) if is_vmware else
       (T("r.0825") if kind == "baremetal" else T("r.0826")),
       fst[0] if fst else T("r.0082"), "info", "systemd fstrim.timer")
    bp(T("r.0542"), "vm.max_map_count", T("r.0827"), sysctl.get("vm.max_map_count", "-"),
       "crit" if (mmc or 0) < 262144 else ("ok" if (mmc or 0) >= 1048576 else "info"), T("r.0786"))
    if mapc and mmc:
        bp(T("r.0542"), T("r.0828"), T("r.0829"), "{:,} / {:,} ({:.0f}%)".format(int(mapc), int(mmc), 100.0 * mapc / mmc),
           "ok" if mapc / mmc < 0.6 else ("warn" if mapc / mmc > 0.8 else "caution"), T("r.0786"))
    if not swap_on:
        sw_cur, sw_st = T("r.0830"), "ok"
    elif mlock is True:
        sw_cur, sw_st = T("r.0831"), "ok"
    elif swappiness is not None and swappiness <= 1:
        sw_cur, sw_st = T("r.0832").format(sysctl.get("vm.swappiness")), "caution"
    else:
        sw_cur, sw_st = T("r.0833").format(sysctl.get("vm.swappiness"), T("r.0082") if mlock is None else T("r.0817")), "warn"
    bp(T("r.0542"), T("r.0834"), T("r.0835"), sw_cur, sw_st, T("r.0786"))
    bp(T("r.0542"), "Transparent HugePage", T("r.0836"), thp_cur or "-", "info" if thp_cur == "always" else "ok", T("r.0837"))
    bp(T("r.0542"), T("r.0838"), T("r.0839"),
       "ratio {}/{} · bytes {}/{}".format(sysctl.get("vm.dirty_background_ratio", "-"), sysctl.get("vm.dirty_ratio", "-"),
                                         sysctl.get("vm.dirty_background_bytes", "-"), sysctl.get("vm.dirty_bytes", "-")), "info", T("r.0840"))
    if kind == "baremetal":
        bp(T("r.0784"), "tuned profile", T("r.0841"),
           tuned_prof or T("r.0082"),
           "ok" if tuned_prof.lower() in ("throughput-performance", "latency-performance", "network-throughput", "network-latency")
           else ("na" if not tuned_prof else "info"),
           T("r.0842"))
    else:
        bp(T("r.0784"), "tuned profile", T("r.0843"),
           tuned_prof or T("r.0082"),
           "ok" if tuned_prof.lower().endswith("virtual-guest") else ("na" if not tuned_prof else "info"),
           T("r.0844"))
    bp(T("r.0542"), T("r.0845"), T("r.0846"), sysctl.get("psi", "-"), "ok" if sysctl.get("psi") == "available" else "info", T("r.0840"))
    bp(T("r.0847"), T("r.0848"), T("r.0849"), nofile or "-", ("ok" if (nofile == "unlimited" or num(nofile, 0) >= 65535) else "crit") if nofile else "na", T("r.0786"))
    if heap_mb and mem_total_mb:
        bp(T("r.0847"), "JVM heap", T("r.0850"),
           "{:.1f}GB / RAM {:.1f}GB ({:.0f}%)".format(heap_mb / 1024, mem_total_mb / 1024, 100 * heap_mb / mem_total_mb),
           "caution" if (heap_mb / mem_total_mb > 0.5 or heap_big) else "ok", T("r.0786"))
    bp(T("r.0847"), T("r.0851"), T("r.0543"), T("r.0852") if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else T("r.0543"),
       "warn" if (cg and re.search(r'(rbps|wbps|riops|wiops)=\d', cg)) else "ok", "cgroup v2")
    bp(T("r.0853"), T("r.0854"), T("r.0855") if kind == "baremetal" else T("r.0856"),
       T("r.0857") + ", ".join(share_dev) if share_dev else T("r.0858"),
       "info" if share_dev else "ok", T("r.0859") if is_vmware else T("r.0489"))
    dh = sorted(set(topo.scsihost.get(d) for d in phys if topo.scsihost.get(d)))
    rh = sorted(set(topo.scsihost.get(d) for d in root_phys if topo.scsihost.get(d)))
    if dh and rh and is_vmware:
        shared = sorted(set(dh) & set(rh))
        bp(T("r.0853"), T("r.0860"), T("r.0861"), T("r.0862") + ", ".join(shared) if shared else T("r.0863") + ", ".join(dh) + ")",
           "info" if shared else "ok", T("r.0864"))
    lin = [d for d in logical if d.startswith("dm-") and len(topo.slaves.get(d, [])) > 1]
    bp(T("r.0853"), T("r.0865"), T("r.0866"), "linear: " + ", ".join(lin) if lin else T("r.0867"),
       "info" if lin and any(" linear " in l for l in rd(S, "dmsetup_table").splitlines()) else "ok", "LVM lvcreate(8)")
    if is_vmware:
        def mbv(x):
            m_ = re.search(r'(-?\d+)', x or "")
            return int(m_.group(1)) if m_ else None
        bal, hsw = mbv(virt.get("stat_balloon")), mbv(virt.get("stat_swap"))
        mres, mlim, clim = mbv(virt.get("stat_memres")), mbv(virt.get("stat_memlimit")), mbv(virt.get("stat_cpulimit"))
        bp("VMware", T("r.0868"), T("r.0869"), ", ".join(drivers) or "-",
           "caution" if any(x in ("mptspi", "mptsas", "ata_piix", "ahci") for x in drivers) else "ok", T("r.0864"))
        bp("VMware", "open-vm-tools", T("r.0870"), virt.get("tools_version", "absent"), "caution" if virt.get("tools_version", "absent") == "absent" else "ok", "VMware")
        if mres is not None:
            bp("VMware", T("r.0871"), T("r.0872"), "{} MB / VM {} MB".format(mres, int(mem_total_mb)),
               "ok" if mres >= mem_total_mb * 0.95 else "caution", T("r.0864"))
        if mlim is not None:
            bp("VMware", T("r.0873"), T("r.0874"), T("r.0543") if (mlim <= 0 or mlim >= 4000000 or mlim >= mem_total_mb) else "{} MB".format(mlim),
               "ok" if (mlim <= 0 or mlim >= 4000000 or mlim >= mem_total_mb) else "warn", T("r.0875"))
        if clim is not None:
            bp("VMware", "CPU limit", T("r.0874"), T("r.0543") if (clim <= 0 or clim >= 4000000) else "{} MHz".format(clim),
               "ok" if (clim <= 0 or clim >= 4000000) else "caution", T("r.0875"))
        if bal is not None:
            bp("VMware", T("r.0876"), "0", "{} MB".format(bal), "ok" if not bal else "warn", "VMware")
        if hsw is not None:
            bp("VMware", T("r.0877"), "0", "{} MB".format(hsw), "ok" if not hsw else "crit", "VMware")
        nics = [p_[2].replace("driver=", "") for p_ in netinfo]
        if nics:
            bp("VMware", "NIC", "VMXNET3", ", ".join(sorted(set(nics))), "caution" if any("e1000" in n for n in nics) else "ok", T("r.0762"))
    if kind == "baremetal":
        cls_txt = ", ".join("{} {}{}".format(d, STORAGE_LABEL.get(c["media"], c["media"] or "-"),
                                             "" if c["sure"] else T("r.0878")) for d, c in sorted(devcls.items()))
        bp(T("r.0090"), T("r.0879"), T("r.0880"), cls_txt + (" · " + attach.upper() if attach != "local" else ""),
           "info" if (storage == "hdd" or attach != "local") else "ok",
           T("r.0881"))
        gov_, gdrv_ = HW["governor"] or ("", "")
        if gov_:
            bp(T("r.0090"), "CPU governor", "performance (tuned throughput-performance)", "{} ({})".format(gov_, gdrv_ or "-"),
               "ok" if gov_ == "performance" else ("info" if gdrv_ in ("intel_pstate", "amd-pstate", "amd-pstate-epp") and gov_ == "powersave" else "caution"),
               T("r.0882"))
        for r_ in HW["nvme"]:
            if r_["temp"] is not None:
                bp(T("r.0090"), T("r.0883").format(r_["ctrl"]), T("r.0884"), T("r.0885").format(r_["temp"], fmt(r_["tmax"], 0, "°C")),
                   "warn" if (r_["tmax"] and r_["temp"] >= r_["tmax"]) else ("caution" if (r_["tmax"] and r_["temp"] >= r_["tmax"] - 5) else "ok"),
                   "Linux nvme hwmon")
            if r_["ls"] and r_["mls"]:
                bp(T("r.0090"), T("r.0886").format(r_["ctrl"]), T("r.0887"), T("r.0888").format(r_["ls"], r_["lw"], r_["mls"], r_["mlw"]),
                   "ok" if (r_["ls"] == r_["mls"] and r_["lw"] == r_["mlw"]) else "caution", "Linux PCI sysfs")
        for md_ in HW["md"]:
            bp(T("r.0090"), T("r.0889").format(md_["name"]), T("r.0890"),
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
            bp(T("r.0090"), T("r.0891").format(v["dev"]), T("r.0892"),
               T("r.0893").format(v.get("level") or "-", v.get("state") or "-", v.get("cache_cur") or "-",
                                                                bat, len(v.get("pds") or [])), st_, T("r.0690").format(r["tool"]))
    bp(T("r.0853"), T("r.0894"), T("r.0895"),
       ", ".join(DM["targets"]) or T("r.0543"),
       "warn" if ("snapshot-origin" in DM["targets"] or "snapshot" in DM["targets"]) else
       ("caution" if "thin" in DM["targets"] else ("info" if DM["targets"] and set(DM["targets"]) - {"linear", "striped"} else "ok")),
       "lvmthin(7), lvmsnapshot")
    bp(T("r.0853"), T("r.0896"), T("r.0897"), ", ".join(swap_hit) + T("r.0898") if swap_hit else (T("r.0543") if not swap_on else T("r.0899")),
       "caution" if swap_hit else "ok", T("r.0379"))
    if repos:
        bp(T("r.0853"), T("r.0900"), T("r.0901"), ", ".join(sorted(set(repos))) + (T("r.0902") if same_repo else ""),
           "caution" if same_repo else "ok", T("r.0903"))
    if HW["smart"]:
        bp(T("r.0090"), "SMART", T("r.0904"), ", ".join("{} {}".format(x["dev"], x["health"] or "-") for x in HW["smart"][:8]),
           sev_max(*[x["sev"] for x in HW["smart"]]), "smartctl")
    for line in rd(S, "df").splitlines()[1:]:
        p_ = line.split()
        if len(p_) >= 6 and any(pm["mount"] and p_[5] in (pm["mount"]["mnt"], pm["mount"].get("host_mnt")) for pm in path_map):
            use = num(p_[4].rstrip("%"), 0)
            lp, hl = wm_high(csettings, (num(p_[1], 0) or 0) * 1024)
            lp = lp or 90
            bp(T("r.0905"), T("r.0906").format(p_[5]), T("r.0907").format(hl), "{}%".format(int(use)),
               "crit" if use >= lp else ("caution" if use >= lp - 10 else "ok"), T("r.0786"))


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
                add("warn" if loaded else "info", T("r.0041"), T("r.own_es") if loaded else T("r.0013"),
                    T("r.0908") + (T("r.0909") if loaded else ""),
                    T("r.0910").format(
                        me_name, mine, med, SEV_LABEL.get(lat_sev, lat_sev), SEV_LABEL.get(sat_sev, sat_sev)),
                    T("r.0911")
                    if loaded else T("r.0912"),
                    T("r.0913"), T("r.0914"))
        # (교차 2) 같은 데이터스토어 동시 저하
        busy = [(r["name"], r["busy"]) for r in dr if r["busy"] is not None]
        if len(busy) >= 3:
            high = [n_ for n_, v in busy if v >= 70]
            if len(high) >= max(2, len(busy) // 2) and SEV_ORDER.get(lat_sev, 0) >= SEV_ORDER["caution"]:
                ev_ = T("r.0915").format(
                        len(high), ", ".join(high[:6]), len(busy), SEV_LABEL.get(lat_sev, lat_sev))
                if kind == "vmware":
                    add("warn", T("r.0041"), T("r.0072"), T("r.0916"), ev_,
                        T("r.0917")
                        + vs(T("r.0918"), T("r.0919"), T("r.0920"))
                        + T("r.0921"),
                        T("r.0922") + vs(
                            T("r.0923"),
                            T("r.0924"),
                            T("r.0925"))
                        + T("r.0926"),
                        T("r.0927"))
                elif kind == "baremetal" and attach == "san":
                    add("warn", T("r.0041"), T("r.0089"), T("r.0928"), ev_,
                        T("r.0929"),
                        T("r.0930"),
                        T("r.0927"))
                elif kind == "baremetal":
                    add("warn", T("r.0041"), T("r.0042"), T("r.0931"), ev_,
                        T("r.0932"),
                        T("r.0933"),
                        T("r.0927"))
                else:
                    add("warn", T("r.0041"), OUT, T("r.0916"), ev_,
                        T("r.0934"),
                        T("r.0935").format(OUT),
                        T("r.0927"))
    # (교차 3) 쓰기가 특정 인덱스에 집중
    if IDX:
        tot = sum(r["docs"] for r in IDX) or 1
        top = IDX[0]
        if top["docs"] / float(tot) > 0.5 and top["docs"] > 1000:
            add("info", T("r.0339"), T("r.own_es"), T("r.0936"),
                T("r.0937").format(
                    top["index"], 100.0 * top["docs"] / tot, int(top["docs"]),
                    " · ILM phase {}".format(top["phase"]) if top["phase"] else ""),
                T("r.0938"),
                T("r.0939"),
                "_nodes/_local/stats/indices?level=indices + _ilm/explain")


    cl_sev = "na"
    if CL:
        aw_ = CL["settings"].get("cluster.routing.allocation.awareness.attributes")
        bp(T("r.0940"), "shard allocation awareness", T("r.0941"), aw_ or T("r.0942"), "ok" if aw_ else "info", T("r.0786"))
        rmax_ = CL["settings"].get("indices.recovery.max_bytes_per_sec", "40mb")
        bp(T("r.0940"), "indices.recovery.max_bytes_per_sec", T("r.0943"), rmax_, "info", T("r.0786"))
        cl_sev = sev_max(*[f.sev for f in F if f.dim == T("r.0041")]) if any(f.dim == T("r.0041") for f in F) else "ok"

    dims = [
        (T("r.0128"), lat_sev), (T("r.0108"), sat_sev), (T("r.0305"), err_sev), (T("r.0339"), es_sev),
        (T("r.0373"), mem_sev), (T("r.0405"), cfg_sev), (RES_DIM, vm_sev), (T("r.0944"), net_sev), (T("r.0041"), cl_sev),
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
            if f.dim == T("r.0339") and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"]:
                f.action += T("r.0945")
    if SEV_ORDER.get(disk_rt, 0) >= SEV_ORDER["warn"]:
        verdict = ("bad", T("r.0946"),
                   T("r.0947"))
    elif SEV_ORDER.get(es_sev, 0) >= SEV_ORDER["warn"] and disk_clean:
        verdict = ("risk", T("r.0948"),
                   T("r.0949"))
    elif low_load and SEV_ORDER.get(runtime, 0) <= SEV_ORDER["caution"]:
        verdict = ("hold", T("r.0950"),
                   T("r.0951").format(fmt(A["iops_p95"], 0), fmt(A["mb_p95"], 1)))
    elif SEV_ORDER.get(latent, 0) >= SEV_ORDER["warn"] or SEV_ORDER.get(runtime, 0) == SEV_ORDER["caution"]:
        verdict = ("risk", T("r.0952"),
                   T("r.0953"))
    else:
        verdict = ("good", T("r.0954"),
                   T("r.0955"))

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
                    rel = T("r.0956")
                elif many_high:
                    rel = T("r.0957")
                else:
                    rel = T("r.0958")
                verdict = (verdict[0], verdict[1],
                           verdict[2] + T("r.0959").format(rel, me, med))


    # 우선 조치: ① 병목 위치(누가 움직일지 결정) → ② 실제로 손댈 수 있는 항목을 심각도순
    top = [f for f in F if f.title.startswith(T("r.0960")) and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["caution"]][:1]
    # 클러스터 교차 판정은 "누가 움직여야 하는가"를 바꾸므로 개별 설정 항목보다 앞에 둔다
    top += [f for f in F if f.dim == T("r.0041") and SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["warn"] and f not in top][:2 - len(top)]
    act_owner = (OUT, T("r.0148"), T("r.own_es"))
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
        "media_note": media_note, "unsure": unsure, "EBS": EBS, "HW": HW, "vmbk": VMBK, "raid_absent": raid_absent,
        "PROC": PROC, "DM": DM, "DEVERR": DEVERR, "out_owner": OUT, "res_dim": RES_DIM, "sto": sto, "src_lat": src_lat,
        "os": kv(rd(S, "os-release")).get("PRETTY_NAME", "").strip('"'), "kernel": (rd(S, "uname").split() + ["", "", ""])[2],
        "ncpu": ncpu, "mem_gb": mem_total_mb / 1024.0, "path_map": path_map, "phys": phys, "logical": logical,
        "dev_guess": dev_guess, "A": A, "dev_stats": dev_stats, "log_stats": log_stats, "topo": topo,
        "agg": agg, "sysr": sysr, "findings": F, "dims": dims, "verdict": verdict, "n_act": n_act,
        "low_load": low_load, "klog": klog, "hist": hist, "es_rows": es_rows,
        "overhead": overhead, "sysctl": sysctl, "virt": virt, "drivers": drivers,
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


COLOR = {"ok": "#1d7a4b", "info": "#2d5f9e", "caution": "#946300", "warn": "#bf4a10", "crit": "#b3261e", "na": "#8b93a1"}


def platform_label(R):
    p = R.get("plat") or {}
    kind = R.get("platform")
    if kind == "vmware":
        base = "VMware Guest"
    elif kind == "baremetal":
        base = "bare-metal" + {"san": " · SAN", "cloud": T("r.1046")}.get(R.get("attach"), "")
    elif kind == "vm":
        base = "{} Guest".format(HV_LABEL.get(p.get("hv"), p.get("hv") or T("r.1047")))
    else:
        base = T("r.1048")
    if p.get("cloud"):
        base += " · " + p["cloud"]
    if p.get("container"):
        base += T("r.1049").format(p["container"])
    return base

def storage_basis(R):
    st = R["storage"]
    lab = {"allflash": "All-Flash vSAN", "hybrid": "Hybrid vSAN"}.get(st, STORAGE_LABEL.get(st, st))
    how = T("r.1050") if R.get("storage_auto") else T("r.1051")
    if R.get("platform") == "vmware" and R.get("storage_auto"):
        # 데이터스토어가 vSAN 인지 SAN·NFS 인지, vSAN 이면 All-Flash 인지 Hybrid 인지 Guest 에서는 알 수 없다
        how = (T("r.1052"))
    if R.get("platform") == "baremetal" and R.get("attach") == "san" and R.get("storage_auto"):
        how = T("r.1053")
    elif R.get("unsure") and R.get("storage_auto"):
        how = T("r.1054")
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
    h.append('<!DOCTYPE html><html lang="' + LANG + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append(T("r.1055").format(E(meta.get("host")), CSS))
    h.append(T("r.1056"))
    h.append(T("r.1057").format(
        E(meta.get("host")), E(R["os"]), E(R["kernel"]), "CPU" if kind == "baremetal" else "vCPU", R["ncpu"], R["mem_gb"], E(R["es_version"]),
        E(platform_label(R)),
        E(meta.get("start_wall", "")), E(meta.get("end_wall", "")[11:19]), R["dur"], E(meta.get("interval"))))

    h.append('<div class="verdict {}"><b>{}</b><span>{}</span></div>'.format(v[0], E(v[1]), E(v[2])))
    if (R.get("plat") or {}).get("container"):
        h.append(T("r.1058"))
    h.append('<div class="dims">')
    for name, s in R["dims"]:
        h.append('<div class="dim"><div class="n">{}</div><div class="s s-{}">{}</div><div class="bar b-{}"></div></div>'.format(E(name), s, SEV_LABEL.get(s, s), s))
    h.append('</div>')
    h.append(T("r.1059").format(
        E(storage_basis(R)), th["caution"], th["warn"], th["crit"], R["n_act"], E(", ".join(R["phys"]) or "-"),
        T("r.1060") if R["dev_guess"] else "",
        " · " + E(R["media_note"]) if R.get("media_note") else ""))

    # 우선 조치
    if R.get("top"):
        h.append(T("r.1061"))
        for f in R["top"]:
            first = re.split(r'(?<=[.다])\s', f.action, 1)[0]
            h.append('<li><span class="tag" style="background:{}">{}</span> <b>{}</b> <span class="who">{}</span><div class="note">{}</div></li>'.format(
                COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.owner), E(first)))
        h.append(T("r.1062"))

    # KPI
    h.append(T("r.1063").format(MIN_IOS_PER_INTERVAL))
    h.append('<div class="kpis">')
    kp = [(T("r.1064"), A["r_await_p95"], "ms", grade(A["r_await_p95"], th), T("r.1065") + fmt(A["r_await_mean"], 2, "ms")),
          (T("r.1066"), A["w_await_p95"], "ms", grade(A["w_await_p95"], th), T("r.1065") + fmt(A["w_await_mean"], 2, "ms")),
          ("IOPS p95", A["iops_p95"], "", "info", T("r.1067").format(fmt(A["rs_p95"], 0), fmt(A["ws_p95"], 0))),
          (T("r.1068"), A["mb_p95"], "MB/s", "info", T("r.1067").format(fmt(A["rmb_p95"], 1), fmt(A["wmb_p95"], 1)))]
    for l, val, u, s, d in kp:
        h.append('<div class="kpi"><div class="l">{}</div><div class="v s-{}">{}</div><div class="d">{}</div></div>'.format(
            E(l), s if s != "na" else "na", fmt(val, 2 if u == "ms" else 1, " " + u if u else ""), E(d)))
    h.append('</div>')
    shape = T("r.1069").format(
        fmt(A.get("r_kb"), 0, "KB"), fmt(A.get("w_kb"), 0, "KB"), fmt(A.get("r_merge_pct"), 0, "%"), fmt(A.get("w_merge_pct"), 0, "%"))
    if A.get("flush_ps") is not None:
        shape += T("r.1070").format(fmt(A["flush_ps"], 1), fmt(A.get("flush_ms"), 2, "ms"))
    h.append(T("r.1071").format(E(shape)))

    # 차트
    h.append(T("r.1072").format(
        {"vmware": T("r.1073"), "baremetal": T("r.1074") if R.get("attach") == "san" else T("r.1075")}.get(kind, T("r.1073"))))
    charts = [
        ("c1", T("r.1076"), [{"k": "ra", "n": T("r.1077"), "c": "#2d5f9e"}, {"k": "wa", "n": T("r.1078"), "c": "#bf4a10"}],
         [{"v": th["caution"], "l": T("r.1079").format(th["caution"]), "c": "#946300"}, {"v": th["crit"], "l": T("r.1080").format(th["crit"]), "c": "#b3261e"}], "ms"),
        ("c2", "IOPS", [{"k": "rs", "n": T("r.1077"), "c": "#2d5f9e"}, {"k": "ws", "n": T("r.1078"), "c": "#bf4a10"}], [], ""),
        ("c3", T("r.1081"), [{"k": "rmb", "n": T("r.1077"), "c": "#2d5f9e"}, {"k": "wmb", "n": T("r.1078"), "c": "#bf4a10"}], [], "MB/s"),
        ("c4", T("r.1082"), [{"k": "aqu", "n": "aqu-sz", "c": "#23324a"}],
         [{"v": R["qd_total"], "l": "queue_depth {}".format(int(R["qd_total"])), "c": "#b3261e"}] if R["qd_total"] else [], ""),
        ("c5", T("r.1083"), [{"k": "psif", "n": "PSI io full", "c": "#b3261e"}, {"k": "psis", "n": "PSI io some", "c": "#946300"},
                             {"k": "iow", "n": "iowait", "c": "#8b93a1"}, {"k": "st", "n": "CPU steal", "c": "#6b4fa0"}], [], "%"),
        ("c6", T("r.1084"), [{"k": "dst", "n": T("r.1085"), "c": "#23324a"}, {"k": "dirty", "n": "Dirty MB", "c": "#1d7a4b"}], [], ""),
    ]
    for cid, title, _s, _t, _u in charts:
        h.append('<div class="chart" id="{}"><div class="t">{}</div><div class="lg">{}</div><svg preserveAspectRatio="none"></svg><div class="ro"></div></div>'.format(
            cid, E(title), " · ".join('<span style="color:{}">━</span> {}'.format(s["c"], E(s["n"])) for s in _s)))

    # 조치 항목
    h.append(T("r.1086"))
    F = sorted(R["findings"], key=lambda f: -SEV_ORDER.get(f.sev, 0))
    for owner in OWNER_ORDER:
        items = [f for f in F if f.owner == owner]
        if not items:
            continue
        h.append(T("r.1087").format(E(owner), len(items), E(OWNER_DESC[owner])))
        for f in items:
            op = " open" if SEV_ORDER.get(f.sev, 0) >= SEV_ORDER["warn"] else ""
            h.append(T("r.1088").format(
                         op, COLOR[f.sev], COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.dim), E(f.evidence), E(f.why), E(f.action), E(f.source)))
        h.append('</div>')

    # Best practice 대조표
    if R.get("BP"):
        nbad = sum(1 for b in R["BP"] if SEV_ORDER.get(b[4], 0) >= SEV_ORDER["caution"])
        h.append(T("r.1089").format(len(R["BP"]), nbad))
        h.append(T("r.1090"))
        cat_prev = None
        for cat, item, rec, cur, st, src in R["BP"]:
            h.append('<tr><td class="note">{}</td><td>{}</td><td>{}</td><td>{}</td><td class="st s-{}"><b>{}</b></td><td class="note">{}</td></tr>'.format(
                E(cat) if cat != cat_prev else "", E(item), E(rec), E(cur), st, SEV_LABEL.get(st, st), E(src)))
            cat_prev = cat
        h.append('</table></div>')

    # ES 지표
    if R["es_rows"]:
        h.append(T("r.1091"))
        h.append(T("r.1092"))
        for a, b, c in R["es_rows"]:
            h.append('<tr><td>{}</td><td class="n">{}</td><td class="note">{}</td></tr>'.format(E(a), E(b), E(c)))
        h.append('</table>')

    # 한계 추정 (부하 테스트 없이 계산할 수 있는 큐 기준 이론 상한)
    h.append(T("r.1093").format(
                 T("r.1094") if kind == "baremetal" else T("r.0410"),
                 {"vmware": T("r.1095") if R.get("vmbk") == "vsan" else T("r.1096"), "baremetal": T("r.1097")}.get(kind, T("r.1098"))))
    if R.get("q_ceiling"):
        use = 100.0 * (A["iops_p95"] or 0) / R["q_ceiling"]
        h.append(T("r.1099").format(
                     "OS" if kind == "baremetal" else "Guest", fmt(R["q_ceiling"], 0, " IOPS"), fmt(A["iops_p95"], 0, " IOPS"), use))
    else:
        h.append(T("r.1100"))

    # 클러스터 관점
    CL = R.get("CL")
    if CL:
        h.append(T("r.1101"))
        h.append(T("r.1102").format(
            "" if CL["io_ok"] else T("r.1103")))
        h.append(T("r.1104"))
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
        h.append(T("r.1105"))
        if CL["acts"]:
            h.append(T("r.1106").format(E(" · ".join(CL["acts"]))))

    # 이 노드의 인덱스별 쓰기 분포
    IDX = R.get("IDX")
    if IDX:
        h.append(T("r.1107"))
        h.append(T("r.1108"))
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
            h.append(T("r.1109").format(len(IDX)))

    # 디스크를 쓴 프로세스
    PROC = R.get("PROC") or []
    if PROC:
        h.append(T("r.1110"))
        h.append(T("r.1111"))
        for x in PROC[:10]:
            h.append('<tr{}><td>{}{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
                ' style="background:#eef3fa"' if x["es"] else "", E(x["comm"]), " <span class='note'>(Elasticsearch)</span>" if x["es"] else "",
                E(x["pid"]), fmt(x["r_mb"], 1, " MB"), fmt(x["w_mb"], 1, " MB")))
        h.append('</table>')

    # 디바이스 상세
    h.append(T("r.1112"))
    h.append(T("r.1113"))
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
    h.append(T("r.1114"))
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
        h.append(T("r.1115"))
        ATT = {"local": T("r.1116"), "san": "SAN", "nvmeof": "NVMe-oF", "cloud": T("r.1117"), "virtual": T("r.0410")}
        for d in sorted(dc):
            c = dc[d]
            virt_disk = kind in ("vmware", "vm", "unknown") and c["attach"] == "local"
            h.append('<tr><td><b>{}</b></td><td>{}</td><td>{}</td><td class="note">{}</td><td>{}</td><td>{}</td></tr>'.format(
                E(d), "-" if virt_disk else E(STORAGE_LABEL.get(c["media"], c["media"] or "-") + ("" if c["sure"] else T("r.1118"))),
                E(T("r.0410") if virt_disk else ATT.get(c["attach"], c["attach"])), E(c["why"] or "-"),
                E((c["vendor"] + " " + c["model"]).strip() or "-"), E(topo.attr.get(d, {}).get("queue/write_cache", "-"))))
        h.append('</table></div>')
        if kind == "baremetal":
            h.append(T("r.1119"))
    h.append(T("r.1120"))

    # 이력
    if R["hist"]:
        h.append(T("r.1121"))
        h.append(T("r.1122"))
        for day, p95, mxv, when, ut in R["hist"]:
            h.append('<tr><td>{}</td><td class="n s-{}">{}</td><td class="n s-{}">{}</td><td>{}</td><td class="n">{}</td></tr>'.format(
                E(day), grade(p95, th), fmt(p95, 1, "ms"), grade(mxv, th), fmt(mxv, 1, "ms"), E(when), fmt(ut, 0, "%")))
        h.append('</table>')

    # 측정 범위와 한계
    ov = R["overhead"]
    h.append(T("r.1123"))
    h.append(T("r.1124"))
    h.append('<div class="kpis">')
    load_kp = [
        (T("r.1125"), fmt(ov["cpu_s"], 2, T("r.1126")), T("r.1127").format(ov["dur"] or 0, fmt(ov["pct_core"], 2, "%"))),
        (T("r.1128"), fmt(ov["read_total_mb"], 1, " MB"), T("r.1129").format(
            fmt(ov["read_eslog_mb"], 1, "MB"), fmt(ov["read_sar_mb"], 1, "MB"))),
        (T("r.1130"), fmt(ov["write_mb"], 2, " MB"), T("r.1131")),
        (T("r.1132"), T("r.1133").format(ov["es_calls"]) if ov["es_calls"] else T("r.1134"),
         T("r.1135").format(fmt(ov["es_bytes_mb"], 2, "MB")) if ov["es_calls"] else T("r.1136")),
    ]
    for l, val, d in load_kp:
        h.append('<div class="kpi"><div class="l">{}</div><div class="v">{}</div><div class="d">{}</div></div>'.format(E(l), E(val), E(d)))
    h.append('</div>')
    notes = []
    notes.append(T("r.1137"))
    if ov["maps_ms"] is not None:
        notes.append(T("r.1138").format(fmt(ov["maps_ms"], 0, "ms")))
    if ov["light"]:
        notes.append(T("r.1139"))
    elif ov["skipped"]:
        notes.append(T("r.1140").format(E(ov["skipped"])))
    if ov["out_on_data_fs"]:
        notes.append(T("r.1141"))
    notes.append(T("r.1142"))
    h.append('<ul class="note">' + "".join("<li>{}</li>".format(n) for n in notes) + '</ul>')

    h.append(T("r.1143"))
    h.append(T("r.1144"))
    rows = [
        (T("r.1145"), T("r.1146"), T("r.1147"), T("r.1148")),
        (T("r.1149"), "/proc/diskstats, /sys/block/*/device/queue_depth",
         {"vmware": T("r.1150"),
          "baremetal": T("r.1151") if R.get("attach") != "san" else T("r.1152")
          }.get(kind, T("r.1150")), T("r.1153")),
        ("PSI io some/full", T("r.1154"), T("r.1155"), T("r.1156")),
        (T("r.1157"), "/proc/<pid>/task/*/stat", T("r.1158"), T("r.1156")),
        ("iowait · procs_blocked", "/proc/stat", T("r.1159"), T("r.1160")),
        (T("r.1161"), "/proc/<pid>/stat, /proc/<pid>/io", T("r.1162"), T("r.1163")),
        ("swap · dirty page", "/proc/vmstat, /proc/meminfo", T("r.1164"), T("r.1165")),
    ]
    if kind == "vmware":
        rows += [(T("r.1166"), "/proc/stat, vmware-toolbox-cmd stat", T("r.1167"), T("r.1168")),
                 (T("r.1169"), T("r.1170"), T("r.1171"), T("r.1172"))]
    elif kind == "baremetal":
        rows += [(T("r.1173"), T("r.1174"), T("r.1175"), T("r.1176")),
                 (T("r.1177"), "/sys/class/nvme/*/hwmon, */device/current_link_*", T("r.1178"), T("r.1179")),
                 (T("r.1180"), "/proc/mdstat", T("r.1181"), T("r.1179")),
                 ("CPU governor", "/sys/devices/system/cpu/cpu0/cpufreq", T("r.1182"), T("r.1179")),
                 ("SMART", T("r.1183"), T("r.1184"), T("r.1179")),
                 (T("r.1185"), T("r.1186"),
                  T("r.1187"), T("r.1188")),
                 (T("r.1169"), T("r.1170"), T("r.1189"), T("r.1172"))]
    else:
        rows += [("CPU steal", "/proc/stat", T("r.1190"), T("r.1191")),
                 (T("r.1169"), T("r.1170"), T("r.1192"), T("r.1172"))]
    rows += [
        (T("r.1193"), T("r.1194"), T("r.1195"), T("r.1196")),
        (T("r.1197"), T("r.1198"), T("r.1199"), T("r.1200")),
        (T("r.1201"), T("r.1202"), T("r.1203"), T("r.1204")),
        (T("r.1205"), T("r.1206"), T("r.1207"), T("r.1196")),
        (T("r.1208"), T("r.1209"), T("r.1210"), T("r.1200")),
        (T("r.1211"), T("r.1212"), T("r.1213").format("CPU" if kind == "baremetal" else "vCPU"), T("r.1200")),
        (T("r.1214"), T("r.1215"), T("r.1216"), T("r.1217")),
        (T("r.1218"), "_nodes/_local/stats/indices?level=indices + _ilm/explain", T("r.1219"), T("r.1220")),
        (T("r.1221"), "_cat/allocation", T("r.1222"), T("r.1223")),
    ]
    for r in rows:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in r) + '</tr>')
    h.append('</table>')
    h.append(T("r.1224"))
    h.append(T("r.1225"))
    gaps = [
        (T("r.1226"), T("r.1227"), T("r.1228"), T("r.1229")),
        (T("r.1230"), T("r.0543"), T("r.1231") + {"vmware": T("r.1232"), "baremetal": T("r.1233")}.get(kind, T("r.1232")),
         T("r.1234").format(R.get("out_owner", T("r.0072")))),
        (T("r.1235"), T("r.1236"), T("r.1237"), T("r.1238")),
        (T("r.1157"), T("r.0543"), T("r.1239"), T("r.1240")),
        (T("r.1241"), T("r.0543"), T("r.1242"), T("r.1243")),
        (T("r.1244"), T("r.0543"), T("r.1245"), T("r.1246")),
        (T("r.1247"), T("r.1248"), T("r.1249"),
         T("r.1250") if kind == "vmware" else T("r.1251")),
    ]
    if kind == "vmware":
        gaps.append((T("r.0086"), T("r.0543"), T("r.1252"), T("r.1253")))
    elif kind == "baremetal":
        gaps.append((T("r.1254"), T("r.1255"), T("r.1256"), T("r.1257")))
    gaps += [
        (T("r.1258"), T("r.0543"), T("r.1259"), T("r.1260").format("CPU" if kind == "baremetal" else "vCPU")),
        (T("r.1261"), T("r.0543"), T("r.1262"), T("r.1263")),
        (T("r.1264"), T("r.1265"), T("r.1266"), T("r.1267")),
        (T("r.1268"), T("r.0543"), T("r.1269"), T("r.1270")),
    ]
    for g in gaps:
        h.append('<tr>' + "".join('<td>{}</td>'.format(E(c)) for c in g) + '</tr>')
    h.append('</table>')
    h.append(T("r.1271"))
    if kind == "vmware":
        h.append(T("r.1272"))
    elif kind == "baremetal":
        h.append(T("r.1273").format(E(R.get("out_owner"))))
    else:
        h.append(T("r.1274").format(E(R.get("out_owner"))))
    h.append(T("r.1275"))
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
    h.append(T("r.1276"))
    sc = R["sysctl"]
    h.append(T("r.1277"))
    for k in ("vm.swappiness", "vm.max_map_count", "vm.dirty_ratio", "vm.dirty_background_ratio", "vm.dirty_bytes", "vm.dirty_background_bytes",
              "vm.zone_reclaim_mode", "fs.file-max", "thp.enabled", "thp.defrag", "psi"):
        h.append('<tr><td><code>{}</code></td><td>{}</td></tr>'.format(E(k), E(sc.get(k, "-"))))
    for k, vv in sorted(R["virt"].items()):
        h.append('<tr><td><code>{}</code></td><td>{}</td></tr>'.format(E(k), E(vv)))
    h.append(T("r.1278").format(E(" · ".join((R.get("plat") or {}).get("evidence") or []) or "-")))
    h.append('</table></div></details>')
    if R["klog"]:
        h.append(T("r.1279").format(
            min(60, len(R["klog"])), E("\n".join(R["klog"][-60:]))))
    h.append(T("r.1280").format(E(meta.get("tool_version", TOOL_VERSION))))

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
        sys.exit(T("r.1281") + cdir)
    root = rjson(cdir, "root.json") or {}
    worst = sev_max(*[f.sev for f in F]) if F else "ok"
    if SEV_ORDER.get(worst, 0) >= SEV_ORDER["warn"]:
        v = ("bad", T("r.1282"), T("r.1283"))
    elif SEV_ORDER.get(worst, 0) >= SEV_ORDER["caution"]:
        v = ("risk", T("r.1284"), T("r.1285"))
    else:
        v = ("good", T("r.1286"), T("r.1287"))
    h = ['<!DOCTYPE html><html lang="' + LANG + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         T("r.1288").format(CSS),
         T("r.1289"),
         T("r.1290").format(
             E(dig(root, "cluster_name") or "-"), E(dig(root, "version", "number") or "-"), len(CL["rows"]),
             E(CL["meta"].get("start_wall", "")), E(CL["meta"].get("end_wall", "")[11:19]), E(CL["meta"].get("gap", ""))),
         '<div class="verdict {}"><b>{}</b><span>{}</span></div>'.format(v[0], E(v[1]), E(v[2]))]
    h.append(T("r.1291"))
    for r in CL["rows"]:
        h.append('<tr><td><b>{}</b></td><td class="note">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td><td class="n">{}</td></tr>'.format(
            E(r["name"]), E(r["roles"]), fmt(r["busy"], 0, "%"), fmt(r["wmb"], 1, " MB/s"), fmt(r["used_pct"], 0, "%"),
            fmt(r["store_gb"], 0, "GB"), fmt(r["idx_rate"], 0, "/s"), fmt(r["throttle"], 0, "ms"), fmt(r["wrej"], 0), fmt(r["heap_pct"], 0, "%"), fmt(r["cpu"], 0, "%")))
    h.append('</table></div>')
    if not CL["io_ok"]:
        h.append(T("r.1292"))
    h.append(T("r.1293"))
    for f in sorted(F, key=lambda f: -SEV_ORDER.get(f.sev, 0)):
        h.append(T("r.1294").format(
                     COLOR[f.sev], COLOR[f.sev], SEV_LABEL[f.sev], E(f.title), E(f.owner), E(f.evidence), E(f.why), E(f.action), E(f.source)))
    if not F:
        h.append(T("r.1295"))
    h.append(T("r.1296").format(TOOL_VERSION) + '</div></body></html>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(h))
    return v



# 차트 스크립트. 화면 문구 두 개만 카탈로그에서 넣는다
JS_SRC = '\nfunction chart(id, series, ths, unit){\n  const el=document.getElementById(id); if(!el) return;\n  const svg=el.querySelector(\'svg\'), ro=el.querySelector(\'.ro\');\n  const W=1000,H=190,L=46,R=10,T=10,B=24;\n  const xs=DATA.t; if(!xs.length){ro.textContent=\'__NODATA__\';return;}\n  let mx=0; series.forEach(s=>DATA[s.k].forEach(v=>{if(v!=null&&v>mx)mx=v}));\n  ths.forEach(t=>{if(t.v>mx*0.6&&t.v<mx*2)mx=Math.max(mx,t.v)}); mx=mx*1.1||1;\n  const x=i=>L+(W-L-R)*(xs.length>1?i/(xs.length-1):0), y=v=>T+(H-T-B)*(1-v/mx);\n  let g=\'\';\n  for(let k=0;k<=4;k++){const v=mx*k/4;g+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#eceef2"/>`+\n    `<text x="${L-6}" y="${y(v)+4}" font-size="11" fill="#8b93a1" text-anchor="end">${v<10?v.toFixed(1):Math.round(v)}</text>`;}\n  ths.forEach(t=>{if(t.v<=mx){g+=`<line x1="${L}" x2="${W-R}" y1="${y(t.v)}" y2="${y(t.v)}" stroke="${t.c}" stroke-dasharray="5 4" stroke-width="1.2"/>`+\n    `<text x="${W-R-4}" y="${y(t.v)-4}" font-size="11" fill="${t.c}" text-anchor="end">${t.l}</text>`;}});\n  const n=xs.length, step=Math.max(1,Math.round(n/6));\n  for(let i=0;i<n;i+=step){g+=`<text x="${x(i)}" y="${H-6}" font-size="11" fill="#8b93a1" text-anchor="middle">${Math.round(xs[i])}s</text>`;}\n  series.forEach(s=>{let d=\'\',pen=false;DATA[s.k].forEach((v,i)=>{if(v==null){pen=false;return;}d+=(pen?\'L\':\'M\')+x(i).toFixed(1)+\',\'+y(v).toFixed(1);pen=true;});\n    g+=`<path d="${d}" fill="none" stroke="${s.c}" stroke-width="1.8"/>`;});\n  g+=`<line id="${id}_c" x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="#9aa3b2" visibility="hidden"/>`;\n  svg.setAttribute(\'viewBox\',`0 0 ${W} ${H}`); svg.innerHTML=g;\n  const cur=document.getElementById(id+\'_c\');\n  svg.addEventListener(\'mousemove\',e=>{const r=svg.getBoundingClientRect();const px=(e.clientX-r.left)/r.width*W;\n    let i=Math.round((px-L)/(W-L-R)*(n-1));i=Math.max(0,Math.min(n-1,i));cur.setAttribute(\'x1\',x(i));cur.setAttribute(\'x2\',x(i));cur.setAttribute(\'visibility\',\'visible\');\n    ro.textContent=`${Math.round(xs[i])}__SEC__ · `+series.map(s=>`${s.n} ${DATA[s.k][i]==null?\'-\':DATA[s.k][i].toFixed(2)}${unit}`).join(\' · \');});\n  svg.addEventListener(\'mouseleave\',()=>{cur.setAttribute(\'visibility\',\'hidden\');ro.textContent=\'\';});\n}\n'


def _init_texts():
    """Text constants that depend on the report language. Rebuilt by set_lang()."""
    global STORAGE_LABEL, LAT_SRC, SEV_LABEL, HV_LABEL, JS, OWNER_ORDER, OWNER_DESC, BLIND_VMWARE, BLIND_VMWARE_DS, BLIND_BAREMETAL, BLIND_SAN, BLIND_VM
    STORAGE_LABEL = {"allflash": "vSAN All-Flash", "hybrid": "vSAN Hybrid", "nvme": "NVMe", "ssd": "SSD",
                     "hdd": "HDD", "vm": T("r.0001"), "vmware": T("r.0002"),
                     "vmfs": T("r.0003"), "cloud": T("r.0004"), "network": T("r.0005")}
    LAT_SRC = {
        "vsan": T("r.0006"),
        "device": T("r.0007"),
        "vm": T("r.0008"),
        "vmware": T("r.0009"),
        "cloud": T("r.0010"),
    }
    SEV_LABEL = {"ok": T("r.0011"), "na": T("r.0012"), "info": T("r.0013"), "caution": T("r.0014"), "warn": T("r.0015"), "crit": T("r.0016")}
    HV_LABEL = {"vmware": "VMware", "kvm": "KVM", "qemu": "QEMU", "microsoft": "Hyper-V", "xen": "Xen",
                "amazon": "AWS Nitro", "google": "Google Compute Engine", "oracle": "VirtualBox", "powervm": "IBM PowerVM",
                "zvm": "IBM z/VM", "parallels": "Parallels", "bhyve": "bhyve", "qnx": "QNX", "acrn": "ACRN", "apple": "Apple Virtualization",
                "sre": "SRE", "bochs": "Bochs", "uml": "UML", "vm-other": T("r.0018"), "unknown-vm": T("r.0018")}
    JS = JS_SRC.replace("__NODATA__", T("r.js.nodata")).replace("__SEC__", T("r.js.sec"))
    OWNER_ORDER = [T("r.0042"), T("r.0148"), T("r.0072"), T("r.0098"), T("r.0095"), T("r.0089"), T("r.own_es"), T("r.0013")]
    OWNER_DESC = {
        T("r.0042"): T("r.0962"),
        T("r.0148"): T("r.0963"),
        T("r.0072"): T("r.0964"),
        T("r.0098"): T("r.0965"),
        T("r.0095"): T("r.0966"),
        T("r.0089"): T("r.0967"),
        T("r.own_es"): T("r.0968"),
        T("r.0013"): T("r.0969"),
    }
    BLIND_VMWARE = [
        (T("r.0970"), T("r.0971"), T("r.0972")),
        (T("r.0973"), T("r.0974"), T("r.0975")),
        (T("r.0976"), T("r.0977"), T("r.0978")),
        (T("r.0979"), T("r.0980"), T("r.0981")),
        (T("r.0982"), T("r.0983"), T("r.0984")),
        (T("r.0985"), T("r.0986"), "vSAN Skyline Health"),
        (T("r.0987"), T("r.0988"), T("r.0989")),
    ]
    BLIND_VMWARE_DS = [
        (T("r.0990"), T("r.0991"), T("r.0992")),
        (T("r.0993"), T("r.0994"), T("r.0995")),
        (T("r.0996"), T("r.0997"), T("r.0998")),
        (T("r.0976"), T("r.0977"), T("r.0978")),
        (T("r.0999"), T("r.1000"), T("r.0981")),
        (T("r.1001"), T("r.1002"), T("r.1003")),
    ]
    BLIND_BAREMETAL = [
        (T("r.1004"), T("r.1005"), T("r.1006")),
        (T("r.1007"), T("r.1008"), T("r.1009")),
        (T("r.1010"), T("r.1011"), T("r.1012")),
        (T("r.1013"), T("r.1014"), T("r.1015")),
        (T("r.1016"), T("r.1017"), T("r.1018")),
    ]
    BLIND_SAN = [
        (T("r.1019"), T("r.1020"), T("r.1021")),
        (T("r.1022"), T("r.1023"), T("r.1024")),
        (T("r.1025"), T("r.1026"), T("r.1027")),
        (T("r.1028"), T("r.1029"), T("r.1030")),
        (T("r.1031"), T("r.1032"), T("r.1033")),
    ]
    BLIND_VM = [
        (T("r.1034"), T("r.1035"), T("r.1036")),
        (T("r.1037"), T("r.1038"), T("r.1039")),
        (T("r.1040"), T("r.1041"), T("r.1042")),
        (T("r.1043"), T("r.1044"), T("r.1045")),
    ]


set_lang(os.environ.get("ESDP_LANG", "ko"))

def out_paths(out, default_dir, default_name, langs):
    """-o 가 있으면 그 이름에, 없으면 기본 이름에 언어를 붙인다 (report.html -> report.ko.html, report.en.html).
    한 언어만 만들 때 -o 를 주면 그 이름 그대로 쓴다"""
    if out and len(langs) == 1:
        return {langs[0]: out}
    base = out or os.path.join(default_dir, default_name)
    stem = base[:-5] if base.endswith(".html") else base
    return {l: "{}.{}.html".format(stem, l) for l in langs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--cluster-only")
    ap.add_argument("-o", "--out")
    ap.add_argument("--storage", choices=["auto", "allflash", "hybrid", "nvme", "ssd", "hdd"])
    ap.add_argument("--platform", choices=["auto", "vmware", "baremetal", "vm"])
    ap.add_argument("--cluster")
    ap.add_argument("--lang", choices=["both", "auto", "ko", "en"], default="both",
                    help="report language. both (default) writes <name>.ko.html and <name>.en.html")
    a = ap.parse_args()
    langs = list(LANGS) if a.lang == "both" else [detect_lang() if a.lang == "auto" else a.lang]
    if a.cluster_only:
        cdir = open_bundle(a.cluster_only)
        paths = out_paths(a.out, os.path.dirname(os.path.abspath(a.cluster_only)), "es_cluster_report.html", langs)
        for lang in langs:
            set_lang(lang)
            v = render_cluster_only(cdir, paths[lang])
            print(paths[lang]); print(T("r.1297") + v[1])
        return
    if not a.bundle:
        ap.error(T("r.1298"))
    base = open_bundle(a.bundle)
    paths = None
    for lang in langs:
        set_lang(lang)
        R = analyze(base, None if a.storage == "auto" else a.storage, a.cluster, a.platform)
        if paths is None:
            paths = out_paths(a.out, os.path.dirname(os.path.abspath(a.bundle)),
                              "es_disk_report_{}.html".format(R["meta"].get("host", "node")), langs)
        render(R, paths[lang])
        print(paths[lang])
        print(T("r.1299").format(platform_label(R), storage_basis(R)))
        print(T("r.1300").format(R["verdict"][1], R["n_act"]))

if __name__ == "__main__":
    main()
