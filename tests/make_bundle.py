#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
합성 번들 생성기 (테스트 전용)

es_disk_collect.sh 가 만드는 번들과 같은 형식으로 디렉터리를 만든다.
실제 서버 없이 플랫폼·매체·부하 조합별로 판정 로직을 검증하려는 용도다.

  python3 tests/make_bundle.py <시나리오 이름> <출력 디렉터리>
  python3 tests/make_bundle.py --list
"""
import json, os, sys

# ─────────────────────────────────────────────────────────────────────────────
# 장치 정의: 이름 → sysfs 속성과 부하
#   lat_r / lat_w : 구간 평균 응답시간(ms), iops_r / iops_w : 초당 I/O
#   aqu : 평균 대기 I/O, inflight : 장치 처리 중 I/O, util : %util
# ─────────────────────────────────────────────────────────────────────────────
def dev(name, rot="0", qd=None, host=None, vendor="", model="", sched="none [mq-deadline] kyber bfq",
        ra="128", timeout=None, wcache="write back", lat_r=0.5, lat_w=0.8, iops_r=300, iops_w=300,
        aqu=1.0, inflight=1, util=30.0, extra=None):
    return dict(name=name, rot=rot, qd=qd, host=host, vendor=vendor, model=model, sched=sched, ra=ra,
                timeout=timeout, wcache=wcache, lat_r=lat_r, lat_w=lat_w, iops_r=iops_r, iops_w=iops_w,
                aqu=aqu, inflight=inflight, util=util, extra=extra or {})

VM_VIRT = ("detect_virt=vmware\nsys_vendor=VMware, Inc.\nproduct_name=VMware Virtual Platform\n"
           "tools_version=12.1.5.20735 (build-20735119)\nstat_balloon=0 MB\nstat_swap=0 MB\n"
           "stat_memlimit=4000000 MB\nstat_memres=16384 MB\nstat_cpulimit=4000000 MHz\nstat_cpures=0 MHz\n")
BM_VIRT = "detect_virt=none\nsys_vendor=Dell Inc.\nproduct_name=PowerEdge R750\ntools_version=absent\n"
KVM_VIRT = "detect_virt=kvm\nsys_vendor=Red Hat\nproduct_name=KVM\ntools_version=absent\n"

SCEN = {}

# 1) 기존 동작 기준: VMware, 정상
SCEN["vmware_ok"] = dict(virt=VM_VIRT, storage="allflash", hostdrv={"host2": "vmw_pvscsi", "host0": "vmw_pvscsi"},
    devs=[dev("sda", rot="1", qd="64", host="host0", vendor="VMware", model="Virtual disk", timeout="180", iops_r=50, iops_w=50),
          dev("sdb", rot="1", qd="64", host="host2", vendor="VMware", model="Virtual disk", timeout="180",
              lat_r=1.5, lat_w=2.0, iops_r=800, iops_w=600, aqu=3.0, inflight=3)])

# 2) VMware, 지연 높고 큐 여유 → VM 바깥
SCEN["vmware_outside"] = dict(SCEN["vmware_ok"], devs=[
    SCEN["vmware_ok"]["devs"][0],
    dev("sdb", rot="1", qd="64", host="host2", vendor="VMware", model="Virtual disk", timeout="180",
        lat_r=14, lat_w=22, iops_r=900, iops_w=700, aqu=8, inflight=8, util=95)])

# 3) VMware, 큐 포화
SCEN["vmware_queue"] = dict(SCEN["vmware_ok"], devs=[
    SCEN["vmware_ok"]["devs"][0],
    dev("sdb", rot="1", qd="64", host="host2", vendor="VMware", model="Virtual disk", timeout="180",
        lat_r=12, lat_w=15, iops_r=2500, iops_w=2500, aqu=60, inflight=62, util=100)])

# 4) bare-metal NVMe 정상
SCEN["bm_nvme_ok"] = dict(virt=BM_VIRT, hostdrv={"host0": "ahci"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="MZ7L3480HCHQ", timeout="30", iops_r=20, iops_w=40),
          dev("nvme0n1", rot="0", lat_r=0.15, lat_w=0.05, iops_r=6000, iops_w=4000, aqu=2, inflight=2, util=40)],
    nvme={"nvme0": {"model": "Dell Ent NVMe P5600 MU U.2 3.2TB", "firmware_rev": "1.2.0", "transport": "pcie",
                    "state": "live", "temp": "38850", "temp_max": "70850", "temp_crit": "75850", "temp_alarm": "0",
                    "link_speed": "16.0 GT/s PCIe", "max_link_speed": "16.0 GT/s PCIe",
                    "link_width": "4", "max_link_width": "4"}})

# 5) bare-metal NVMe 느림 + 과열 + 링크 저하
SCEN["bm_nvme_slow"] = dict(SCEN["bm_nvme_ok"], governor="powersave", tuned="balanced",
    devs=[SCEN["bm_nvme_ok"]["devs"][0],
          dev("nvme0n1", rot="0", lat_r=2.4, lat_w=3.5, iops_r=6000, iops_w=4000, aqu=30, inflight=30, util=99)],
    nvme={"nvme0": dict(SCEN["bm_nvme_ok"]["nvme"]["nvme0"], temp="72850", link_speed="8.0 GT/s PCIe", link_width="2")})

# 6) bare-metal HDD RAID (megaraid) 쓰기 느림, 큐 여유 → 장치 자체가 느림
SCEN["bm_hdd_raid"] = dict(virt=BM_VIRT, hostdrv={"host0": "megaraid_sas"}, governor="performance",
    tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="256", host="host0", vendor="DELL", model="PERC H740P Mini", timeout="90",
              lat_r=9, lat_w=45, iops_r=150, iops_w=250, aqu=6, inflight=6, util=90)],
    klog=["2026-09-28T03:11:02+0900 kernel: megaraid_sas 0000:18:00.0: 12567 (749175033s/0x0001/FATAL) - Controller cache pinned for missing or offline VD 00/0",
          "2026-09-28T03:11:05+0900 kernel: sd 0:2:0:0: [sda] tag#12 timing out command, waited 180s"])

# 7) bare-metal SATA SSD 4개 md RAID0, 한 개만 느림 + md resync
SCEN["bm_md_outlier"] = dict(virt=BM_VIRT, hostdrv={"host0": "mpt3sas"}, governor="performance",
    tuned="throughput-performance",
    devs=[dev(n, rot="0", qd="32", host="host0", vendor="ATA", model="SAMSUNG MZ7LH960",
              lat_r=(6.5 if n == "sdd" else 0.6), lat_w=(9.0 if n == "sdd" else 0.9),
              iops_r=1500, iops_w=1200, aqu=(9 if n == "sdd" else 1.5), inflight=(9 if n == "sdd" else 2),
              util=(98 if n == "sdd" else 45)) for n in ("sdb", "sdc", "sdd", "sde")]
         + [dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="SAMSUNG MZ7LH240", iops_r=10, iops_w=20)],
    md={"md0": ["sdb", "sdc", "sdd", "sde"]},
    mdstat=("Personalities : [raid0] [raid1]\n"
            "md1 : active raid1 sdf1[1] sdg1[0]\n      976630464 blocks super 1.2 [2/1] [U_]\n"
            "      [==>..................]  recovery = 12.6% (123456/976630464) finish=80.1min speed=150000K/sec\n\n"
            "md0 : active raid0 sde[3] sdd[2] sdc[1] sdb[0]\n      3750223872 blocks super 1.2 512k chunks\n\n"
            "unused devices: <none>\n"))

# 8) bare-metal SAN (FC + multipath) 지연 높음, 큐 여유 → 어레이 쪽
SCEN["bm_san"] = dict(virt=BM_VIRT, hostdrv={"host0": "ahci", "host5": "qla2xxx", "host6": "qla2xxx"},
    governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="MZ7L3480HCHQ", iops_r=10, iops_w=20),
          dev("sdb", rot="0", qd="64", host="host5", vendor="PURE", model="FlashArray",
              lat_r=4.5, lat_w=7.0, iops_r=3000, iops_w=2500, aqu=10, inflight=10, util=90),
          dev("sdc", rot="0", qd="64", host="host6", vendor="PURE", model="FlashArray",
              lat_r=4.5, lat_w=7.0, iops_r=3000, iops_w=2500, aqu=10, inflight=10, util=90)],
    dm={"dm-0": {"name": "mpatha", "slaves": ["sdb", "sdc"], "table": "mpatha: 0 4294967296 multipath 1 queue_if_no_path 0 1 1 service-time 0 2 1 8:16 1 8:32 1"}},
    mount_src="/dev/mapper/mpatha")

# 9) KVM 게스트 (virtio-blk) 지연 높음
SCEN["kvm_slow"] = dict(virt=KVM_VIRT, hostdrv={}, tuned="virtual-guest",
    devs=[dev("vda", rot="1", iops_r=20, iops_w=30),
          dev("vdb", rot="1", lat_r=9, lat_w=14, iops_r=1200, iops_w=900, aqu=12, inflight=12, util=97)])

# 10) 컨테이너 안에서 실행
SCEN["container"] = dict(virt="detect_virt=docker\ncontainer=docker\nsys_vendor=\nproduct_name=\ntools_version=absent\n",
    hostdrv={}, devs=[dev("vda", rot="1", lat_r=0.8, lat_w=1.2, iops_r=500, iops_w=400)],
    host_virt="kvm")


# ─────────────────────────────────────────────────────────────────────────────
def build(name, out):
    sc = SCEN[name]
    os.makedirs(os.path.join(out, "static"), exist_ok=True)
    S = lambda f: os.path.join(out, "static", f)
    def w(path, text):
        with open(path, "w") as fh:
            fh.write(text)
    devs = sc["devs"]
    data_dev = devs[-1]["name"]
    md, dm = sc.get("md") or {}, sc.get("dm") or {}
    # 마운트: data 는 md/dm/마지막 장치
    if md:
        data_src = "/dev/" + list(md)[0]
    elif sc.get("mount_src"):
        data_src = sc["mount_src"]
    else:
        data_src = "/dev/" + data_dev + ("p1" if data_dev.startswith("nvme") else "1")
    root_src = "/dev/" + devs[0]["name"] + ("p2" if devs[0]["name"].startswith("nvme") else "2")
    w(S("mounts"), "{} / xfs rw,relatime 0 0\n{} /var/lib/elasticsearch xfs rw,noatime 0 0\n".format(root_src, data_src))
    w(S("df"), "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
               "{} 104857600 10485760 94371840 10% /\n{} 1048576000 419430400 629145600 40% /var/lib/elasticsearch\n".format(root_src, data_src))
    lines = []
    for d in devs:
        n = d["name"]
        attrs = {"size": "1953525168", "queue/scheduler": d["sched"] if not n.startswith("nvme") else "[none] mq-deadline kyber bfq",
                 "queue/rotational": d["rot"], "queue/nr_requests": "256", "queue/read_ahead_kb": d["ra"],
                 "queue/write_cache": d["wcache"], "queue/iostats": "1", "queue/wbt_lat_usec": "0" if n.startswith("nvme") else "75000"}
        if d["qd"]:
            attrs["device/queue_depth"] = d["qd"]
        if d["timeout"]:
            attrs["device/timeout"] = d["timeout"]
        if d["vendor"]:
            attrs["device/vendor"] = d["vendor"]
        if d["model"]:
            attrs["device/model"] = d["model"]
        attrs.update(d["extra"])
        for k, v in attrs.items():
            lines.append("ATTR|{}|{}|{}".format(n, k, v))
        if d["host"]:
            lines.append("SCSIHOST|{}|{}".format(n, d["host"]))
        # 파티션
        if n == devs[0]["name"]:
            p = n + ("p2" if n.startswith("nvme") else "2")
            lines.append("PART|{}|{}|1050624".format(p, n))
        if n == data_dev and not md and not dm:
            p = n + ("p1" if n.startswith("nvme") else "1")
            lines.append("PART|{}|{}|2048".format(p, n))
    for m, sl in md.items():
        lines.append("ATTR|{}|queue/read_ahead_kb|512".format(m))
        lines.append("ATTR|{}|md/level|raid0".format(m))
        for s in sl:
            lines.append("SLAVE|{}|{}".format(m, s))
    for k, v in dm.items():
        lines.append("ATTR|{}|dm/name|{}".format(k, v["name"]))
        lines.append("ATTR|{}|queue/read_ahead_kb|128".format(k))
        for s in v["slaves"]:
            lines.append("SLAVE|{}|{}".format(k, s))
    for h, drv in sc.get("hostdrv", {}).items():
        lines.append("HOSTDRV|{}|{}".format(h, drv))
    w(S("sysfs"), "\n".join(lines) + "\n")
    w(S("dmsetup_table"), "\n".join(v["table"] for v in dm.values()) + ("\n" if dm else ""))
    w(S("virt"), sc["virt"])
    w(S("sysctl"), "vm.swappiness=1\nvm.max_map_count=1048576\nvm.dirty_ratio=20\nvm.dirty_background_ratio=10\n"
                   "vm.dirty_bytes=0\nvm.dirty_background_bytes=0\nthp.enabled=always [madvise] never\npsi=available\n")
    w(S("meminfo"), "MemTotal:       65536000 kB\nMemAvailable:   30000000 kB\n")
    w(S("nproc"), "16\n")
    w(S("uname"), "Linux node1 5.14.0-427.el9.x86_64 #1 SMP x86_64 GNU/Linux\n")
    w(S("os-release"), 'PRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)"\n')
    w(S("swaps"), "Filename Type Size Used Priority\n")
    w(S("tuned"), "Current active profile: {}\n".format(sc.get("tuned", "virtual-guest")))
    w(S("fstrim"), "enabled\nactive\n")
    w(S("udev_rules"), "")
    w(S("klog_io"), "\n".join(sc.get("klog", [])) + ("\n" if sc.get("klog") else ""))
    w(S("net"), "IF|eth0|driver={}|mtu=1500|speed=10000|state=up\n".format(
        "vmxnet3" if "vmware" in sc["virt"] else ("virtio_net" if "kvm" in sc["virt"] else "ice")))
    # 새 수집 항목 (구버전 분석기는 무시)
    plat = []
    if sc.get("governor"):
        plat.append("cpu_governor={}".format(sc["governor"]))
    if "hypervisor" not in sc["virt"]:
        plat.append("cpu_hypervisor_flag={}".format(0 if "detect_virt=none" in sc["virt"] else 1))
    if sc.get("host_virt"):
        plat.append("detect_virt_vm={}".format(sc["host_virt"]))
    if plat:
        with open(S("virt"), "a") as fh:
            fh.write("\n".join(plat) + "\n")
    if sc.get("mdstat"):
        w(S("mdstat"), sc["mdstat"])
    if sc.get("nvme"):
        out_l = []
        for c, a in sc["nvme"].items():
            for k, v in a.items():
                out_l.append("NVME|{}|{}|{}".format(c, k, v))
        w(S("storage"), "\n".join(out_l) + "\nISCSI|sessions|0\n")

    # 샘플: 5초 간격 25개
    n_s, dt = 25, 5.0
    acc = {d["name"]: [0] * 17 for d in devs}
    txt = []
    cpu = [1000, 0, 500, 100000, 50, 0, 10, 0, 0, 0]
    for i in range(n_s):
        t = 1000.0 + i * dt
        txt.append("#T {:.2f}".format(t))
        txt.append("==> /proc/diskstats")
        for d in devs:
            a = acc[d["name"]]
            if i > 0:
                rio, wio = int(d["iops_r"] * dt), int(d["iops_w"] * dt)
                a[0] += rio; a[2] += rio * 16; a[3] += int(rio * d["lat_r"])
                a[4] += wio; a[6] += wio * 64; a[7] += int(wio * d["lat_w"])
                a[9] += int(d["util"] * dt * 10); a[10] += int(d["aqu"] * dt * 1000)
            a[8] = d["inflight"]
            maj = 259 if d["name"].startswith("nvme") else 8
            txt.append(" {} 0 {} {}".format(maj, d["name"], " ".join(str(x) for x in a)))
        txt.append("==> /proc/stat")
        if i > 0:
            cpu[0] += 4000; cpu[2] += 1000; cpu[3] += 3000; cpu[4] += 200
        txt.append("cpu  " + " ".join(str(x) for x in cpu))
        txt.append("procs_running 3\nprocs_blocked 0")
        txt.append("==> /proc/vmstat\npgmajfault 100\npswpin 0\npswpout 0")
        txt.append("==> /proc/meminfo\nMemAvailable: 30000000 kB\nDirty: 20000 kB\nWriteback: 0 kB")
        txt.append("==> DSTATE\n0 150")
    w(os.path.join(out, "samples.raw"), "\n".join(txt) + "\n")
    w(os.path.join(out, "meta"), "tool_version=test\nhost={}\nduration=120\ninterval=5\nstorage={}\nis_root=1\n"
      "es_pid=\nsamples={}\n".format(name, sc.get("storage", "auto"), n_s))
    w(os.path.join(out, "user_paths"), "")
    w(os.path.join(out, "self_overhead"), "0m0.10s 0m0.05s\n0m0.30s 0m0.20s\n")
    return out

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        print("\n".join(SCEN)); sys.exit(0)
    if len(sys.argv) != 3 or sys.argv[1] not in SCEN:
        print(__doc__); sys.exit(1)
    print(build(sys.argv[1], sys.argv[2]))
