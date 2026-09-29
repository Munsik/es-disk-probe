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
        aqu=1.0, inflight=1, util=30.0, extra=None, hctl=None, pattern=None, flush_ps=0, flush_ms=0.0):
    # pattern: 구간별 (IOPS 배율, aqu) 목록. 한도에 걸린 모양처럼 구간마다 다른 부하를 만들 때 쓴다
    return dict(name=name, rot=rot, qd=qd, host=host, vendor=vendor, model=model, sched=sched, ra=ra,
                timeout=timeout, wcache=wcache, lat_r=lat_r, lat_w=lat_w, iops_r=iops_r, iops_w=iops_w,
                aqu=aqu, inflight=inflight, util=util, extra=extra or {}, hctl=hctl, pattern=pattern,
                flush_ps=flush_ps, flush_ms=flush_ms)

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


# ── RAID 컨트롤러 도구 출력 (테스트용으로 직접 만든 것. 형식은 각 도구의 JSON 키·텍스트 레이블을 따름) ──
import json as _json
STORCLI_ALL = {"Controllers": [{"Command Status": {"Controller": 0, "Status": "Success"}, "Response Data": {
    "Basics": {"Controller": 0, "Model": "PERC H740P Mini", "Serial Number": "X"},
    "Version": {"Driver Name": "megaraid_sas", "Firmware Version": "51.16.0-4076"},
    "Status": {"Controller Status": "Optimal", "BBU Status": "NA"},
    "Cachevault_Info": [{"Model": "CVPM02", "State": "Degraded", "Temp": "31C"}],
    "VD LIST": [{"DG/VD": "0/0", "TYPE": "RAID5", "State": "Optl", "Access": "RW", "Consist": "Yes",
                 "Cache": "NRWTD", "Cac": "-", "sCC": "ON", "Size": "5.457 TB", "Name": "esdata"}],
    "PD LIST": [{"EID:Slt": "32:{}".format(i), "DID": i, "State": "Onln", "DG": 0, "Size": "1.818 TB", "Intf": "SAS",
                 "Med": "HDD", "SED": "N", "PI": "N", "SeSz": "512B", "Model": "ST2000NM0135", "Sp": "U", "Type": "-"} for i in range(4)],
}}]}
STORCLI_VALL = {"Controllers": [{"Response Data": {
    "/c0/v0": [{"DG/VD": "0/0", "TYPE": "RAID5", "State": "Optl", "Cache": "NRWTD"}],
    "PDs for VD 0": STORCLI_ALL["Controllers"][0]["Response Data"]["PD LIST"],
    "VD0 Properties": {"Strip Size": "256 KB", "OS Drive Name": "/dev/sda", "Write Cache(initial setting)": "WriteBack"},
}}]}
STORCLI_PD = {"Controllers": [{"Response Data": {
    "Drive /c0/e32/s2 - Detailed Information": {"Drive /c0/e32/s2 State": {
        "Shield Counter": 0, "Media Error Count": 12, "Other Error Count": 0, "Predictive Failure Count": 3,
        "S.M.A.R.T alert flagged by drive": "No"}},
    "Drive /c0/e32/s0 - Detailed Information": {"Drive /c0/e32/s0 State": {
        "Shield Counter": 0, "Media Error Count": 0, "Other Error Count": 0, "Predictive Failure Count": 0,
        "S.M.A.R.T alert flagged by drive": "No"}},
}}]}
STORCLI_TXT = ("#CMD /opt/MegaRAID/perccli/perccli64 /call show all J\n" + _json.dumps(STORCLI_ALL, indent=1) +
               "\n#CMD /opt/MegaRAID/perccli/perccli64 /call/vall show all J\n" + _json.dumps(STORCLI_VALL, indent=1) +
               "\n#CMD /opt/MegaRAID/perccli/perccli64 /call/eall/sall show all J\n" + _json.dumps(STORCLI_PD, indent=1) +
               "\n#CMD /opt/MegaRAID/perccli/perccli64 /call show patrolread J\n" +
               _json.dumps({"Controllers": [{"Response Data": {"Controller Properties": [{"Ctrl_Prop": "PR Current State", "Value": "Stopped"}]}}]}) + "\n")

# storcli2·perccli2 형식을 가정한 변형 (키 이름·값 표기를 바꾼 것). 실제 키가 문서로 확정되지 않아,
# 이름이 바뀌어도 같은 결론이 나오는지 확인하는 용도다
S2_PDS = [{"EID:Slot": "32:{}".format(i), "DID": i, "State": "Online", "DG": 0, "Size": "1.818 TB",
           "Interface": "SAS", "Media Type": "HDD", "Model": "ST2000NM0135"} for i in range(4)]
S2_ALL = {"Controllers": [{"Command Status": {"Controller": 0, "Status": "Success"}, "Response Data": {
    "Basics": {"Controller": 0, "Product Name": "PERC H965i Front", "Serial Number": "X"},
    "Status": {"Controller Status": "Optimal"},
    "Virtual Drives": 1, "Physical Drives": 4,
    "Energy Pack Info": [{"Type": "Supercap", "State": "Degraded"}],
    "Virtual Drive List": [{"DG": 0, "VD": 0, "RAID Level": "RAID5", "State": "Optimal", "Write Cache": "Write Through",
                            "Name": "esdata"}],
    "Physical Drive List": S2_PDS,
}}]}
S2_VALL = {"Controllers": [{"Response Data": {
    "/c0/v0": [{"DG/VD": "0/0", "RAID Level": "RAID5", "State": "Optimal", "Write Cache": "Write Through"}],
    "Drives for VD 0": S2_PDS,
    "Virtual Drive 0 Properties": {"Strip Size": "256 KB", "OS Device Name": "/dev/sda", "Initial Write Cache": "Write Back"},
}}]}
S2_PD = {"Controllers": [{"Response Data": {
    "/c0/e32/s2 Detailed Information": {"Drive /c0/e32/s2 State": {
        "Media Error Count": 12, "Predictive Failure Count": 3, "S.M.A.R.T alert flagged by drive": "No"}},
}}]}
STORCLI2_TXT = ("#CMD /opt/MegaRAID/perccli2/perccli2 /call show all J\n" + _json.dumps(S2_ALL, indent=1) +
                "\n#CMD /opt/MegaRAID/perccli2/perccli2 /call/vall show all J\n" + _json.dumps(S2_VALL, indent=1) +
                "\n#CMD /opt/MegaRAID/perccli2/perccli2 /c0/eall/sall show all J\n" + _json.dumps(S2_PD, indent=1) +
                "\n#TOOL storcli2\n")
# storcli2 변형 2: snake_case 키, 대문자 값, 매체 SSD. 정상 RAID1 이 정상으로, 매체는 추정 없이 SSD 로 나와야 함
S3_PDS = [{"eid_slot": "0:{}".format(i), "state": "ONLINE", "dg": 0, "media_type": "SSD", "interface": "SAS"} for i in range(2)]
S3_ALL = {"controllers": [{"command_status": {"status": "success"}, "response_data": {
    "basics": {"controller": 0, "product_name": "MegaRAID 9660-16i"},
    "status": {"controller_status": "OPTIMAL"},
    "energy_pack_info": [{"type": "supercap", "state": "OPTIMAL"}],
    "virtual_drive_list": [{"dg_vd": "0/0", "raid_level": "RAID1", "state": "OPTIMAL", "write_cache": "WRITE BACK"}],
    "physical_drive_list": S3_PDS}}]}
S3_VALL = {"controllers": [{"response_data": {
    "drives_for_vd_0": S3_PDS,
    "vd_0_properties": {"os_device_name": "/dev/sda", "initial_write_cache": "WRITE BACK"}}}]}
STORCLI2_SNAKE_TXT = ("#CMD /opt/MegaRAID/storcli2/storcli2 /call show all J\n" + _json.dumps(S3_ALL, indent=1) +
                      "\n#CMD /opt/MegaRAID/storcli2/storcli2 /call/vall show all J\n" + _json.dumps(S3_VALL, indent=1) +
                      "\n#TOOL storcli2\n")
# 실제 storcli 처럼 목록 옆에 개수 필드("Virtual Drives": 1)가 있는 경우. 개수가 목록을 가리면 안 된다
_ra = STORCLI_ALL["Controllers"][0]["Response Data"]
STORCLI_CNT = {"Controllers": [{"Command Status": {"Controller": 0, "Status": "Success"},
    "Response Data": dict([("Virtual Drives", 1), ("Physical Drives", 4)] + list(_ra.items()))}]}
STORCLI_CNT_TXT = STORCLI_TXT.replace(_json.dumps(STORCLI_ALL, indent=1), _json.dumps(STORCLI_CNT, indent=1))

SSACLI_TXT = """#CMD /usr/sbin/ssacli ctrl all show config detail

Smart Array P408i-a SR Gen10 in Slot 0 (Embedded)
   Bus Interface: PCI
   Slot: 0
   Controller Status: OK
   Hardware Revision: B
   Cache Status: OK
   Battery/Capacitor Status: OK
   Total Cache Size: 2.0

   Array: A
      Interface Type: Solid State SAS
      Status: OK
      Array Type: Data

      Logical Drive: 1
         Size: 1.7 TB
         Fault Tolerance: 1+0
         Status: OK
         Caching:  Enabled
         Disk Name: /dev/sda
         Logical Drive Label: esdata

      physicaldrive 1I:1:1
         Port: 1I
         Box: 1
         Bay: 1
         Status: OK
         Drive Type: Data Drive
         Interface Type: Solid State SAS
         Size: 960 GB

      physicaldrive 1I:1:2
         Port: 1I
         Box: 1
         Bay: 2
         Status: OK
         Drive Type: Data Drive
         Interface Type: Solid State SAS
         Size: 960 GB
"""

ARCCONF_TXT = """#CMD /usr/sbin/arcconf getconfig 1 AL
Controllers found: 1
----------------------------------------------------------------------
Controller information
----------------------------------------------------------------------
   Controller Status                        : Optimal
   Controller Model                         : Adaptec ASR8805
    Overall Backup Unit Status              : Ready
----------------------------------------------------------------------
Logical device information
----------------------------------------------------------------------
Logical Device number 0
   Logical Device name                      : esdata
   RAID level                               : 10
   Status of Logical Device                 : Degraded
   Device Type                              : SSD
   Write-cache setting                      : Enabled (write-back) when protected by battery/ZMM
   Write-cache status                       : On
   Group 0, Segment 0                       : Present (915715MB, SATA, SSD, Enclosure:0, Slot:0) S1
   Group 0, Segment 1                       : Missing
----------------------------------------------------------------------
Physical Device information
----------------------------------------------------------------------
      Device #0
         State                              : Online
      Device #1
         State                              : Failed
#CMD /usr/sbin/arcconf getconfig 2 AL
Invalid controller number.
"""

# 11) HDD RAID5 (PERC) + perccli: 설정은 write-back 인데 CacheVault 이상으로 write-through, 구성 디스크 predictive failure
SCEN["bm_raid_storcli"] = dict(virt=BM_VIRT, hostdrv={"host0": "megaraid_sas"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="256", host="host0", vendor="DELL", model="PERC H740P Mini", hctl="0:2:0:0",
              lat_r=8, lat_w=40, iops_r=150, iops_w=250, aqu=5, inflight=5, util=85)],
    raw={"raid_storcli": STORCLI_TXT})
# 12) SSD RAID10 (HPE) + ssacli: 커널은 rotational=1 로 보고하지만 컨트롤러 조회로 SSD 확정
SCEN["bm_raid_ssacli"] = dict(virt="detect_virt=none\nsys_vendor=HPE\nproduct_name=ProLiant DL380 Gen10\ntools_version=absent\n",
    hostdrv={"host0": "smartpqi"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="1013", host="host0", vendor="HPE", model="LOGICAL VOLUME", hctl="0:1:0:0",
              extra={"device/raid_level": "RAID 1(+0)"}, lat_r=0.6, lat_w=0.9, iops_r=3000, iops_w=2000, aqu=3, inflight=3, util=60)],
    raw={"raid_ssacli": SSACLI_TXT})
# 13) Adaptec + arcconf: OS 장치 이름 없이 SCSI 주소로 연결, 논리 디스크 degraded
SCEN["bm_raid_arcconf"] = dict(virt=BM_VIRT, hostdrv={"host0": "aacraid"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="256", host="host0", vendor="ASR8805", model="esdata", hctl="0:0:0:0",
              lat_r=1.0, lat_w=1.5, iops_r=2000, iops_w=1500, aqu=2, inflight=2, util=50)],
    raw={"raid_arcconf": ARCCONF_TXT})
# 14) RAID 도구가 없는 PERC: 도구 설치 안내
SCEN["bm_raid_notool"] = dict(virt=BM_VIRT, hostdrv={"host0": "megaraid_sas"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="256", host="host0", vendor="DELL", model="PERC H740P Mini", hctl="0:2:0:0",
              lat_r=5, lat_w=6, iops_r=200, iops_w=200, aqu=2, inflight=2, util=60)],
    raw={"raid_storcli": "#TOOL_ABSENT storcli/perccli\n"})
# 15) VMware, 데이터스토어 종류 미확인(기본) + VM 바깥
SCEN["vmware_default"] = dict(SCEN["vmware_outside"], storage="auto")
# 16) VMware, SAN·NFS 데이터스토어 지정
SCEN["vmware_vmfs"] = dict(SCEN["vmware_outside"], storage="vmfs")
# 17) AWS EBS: 처리량이 볼륨 한도에서 막히는 모양 (절반 구간은 상한 + 큐 적체)
SCEN["aws_ebs_cap"] = dict(virt="detect_virt=amazon\ndetect_virt_vm=amazon\nsys_vendor=Amazon EC2\nproduct_name=r6i.2xlarge\ntools_version=absent\n",
    hostdrv={}, tuned="virtual-guest",
    devs=[dev("nvme0n1", rot="0", iops_r=50, iops_w=50),
          dev("nvme1n1", rot="0", lat_r=4, lat_w=6, iops_r=1500, iops_w=1500, util=99,
              pattern=[(1.0, 24), (1.0, 26), (0.5, 3), (1.0, 25), (0.6, 4), (0.4, 2)])],
    nvme={"nvme0": {"model": "Amazon Elastic Block Store", "transport": "pcie"},
          "nvme1": {"model": "Amazon Elastic Block Store", "transport": "pcie"}})
# 18) ECK: 호스트에서 실행, ES 는 컨테이너. data 경로는 컨테이너 기준이고 mountinfo 로 NVMe 에 연결
SCEN["eck_host"] = dict(virt=BM_VIRT, hostdrv={"host0": "ahci"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="MZ7L3480HCHQ", iops_r=10, iops_w=20),
          dev("nvme1n1", rot="0", lat_r=0.2, lat_w=0.1, iops_r=5000, iops_w=3000, aqu=2, inflight=2)],
    nvme={"nvme1": {"model": "Samsung PM9A3", "transport": "pcie"}},
    host_data_mnt="/var/lib/kubelet/pods/abc/volumes/kubernetes.io~local-volume/pv-es-0",
    data_paths=["/usr/share/elasticsearch/data"],
    datadev=[("/usr/share/elasticsearch/data", "259:1", "nvme1n1p1", "xfs", "/dev/nvme1n1p1", "/usr/share/elasticsearch/data")],
    meta_extra="es_in_container=1\n")
# 19) Ceph RBD (bare-metal Kubernetes 노드의 CSI 볼륨): 네트워크 블록 → 스토리지 관리자
SCEN["bm_ceph_rbd"] = dict(virt=BM_VIRT, hostdrv={"host0": "ahci"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="MZ7L3480HCHQ", iops_r=10, iops_w=20),
          dev("rbd0", rot="1", lat_r=6, lat_w=9, iops_r=1500, iops_w=1000, aqu=3, inflight=3, util=80)],
    host_data_mnt="/var/lib/kubelet/plugins/kubernetes.io/csi/rbd.csi.ceph.com/x/globalmount",
    data_paths=["/usr/share/elasticsearch/data"],
    datadev=[("/usr/share/elasticsearch/data", "252:0", "rbd0", "ext4", "/dev/rbd0", "/usr/share/elasticsearch/data")],
    meta_extra="es_in_container=1\n")

# 21) AWS EBS: Nitro 가 볼륨 한도 초과 시간을 직접 보고 (nvme amzn stats). 시작은 JSON, 끝은 사람이 읽는 형식
EBS_START = ('#DEV nvme1n1\n{"total_read_ops": 100, "ebs_volume_performance_exceeded_iops": 1000000, '
             '"ebs_volume_performance_exceeded_tp": 0, "ec2_instance_ebs_performance_exceeded_iops": 0, '
             '"ec2_instance_ebs_performance_exceeded_tp": 200000, "volume_queue_length": 1}\n')
EBS_END = """#DEV nvme1n1
Total Ops
  Read: 900000
  Write: 800000
EBS Volume Performance Exceeded (us)
  IOPS: 31000000
  Throughput: 0
EC2 Instance EBS Performance Exceeded (us)
  IOPS: 0
  Throughput: 500000
Queue Length (point in time): 12
"""
# 같은 값(볼륨 IOPS 한도 초과 30초, 인스턴스 0.3초)을 다른 출력 형식으로. 형식이 달라도 결론이 같아야 한다
EBS_V2_START = """#DEV nvme1n1
{
  "total_read_ops": 100,
  "ebs_volume_performance_exceeded_iops": 1000000,
  "ebs_volume_performance_exceeded_tp": 0,
  "ec2_instance_ebs_performance_exceeded_iops": 0,
  "ec2_instance_ebs_performance_exceeded_tp": 200000,
  "volume_queue_length": 1
}
"""
EBS_V2_END = """#DEV nvme1n1
total_read_ops                               : 900000
ebs_volume_performance_exceeded_iops         : 31000000
ebs_volume_performance_exceeded_tp           : 0
ec2_instance_ebs_performance_exceeded_iops   : 0
ec2_instance_ebs_performance_exceeded_tp     : 500000
volume_queue_length                          : 12
"""
EBS_V3_START = """#DEV nvme1n1
Total Read Ops: 100
EBS Volume Performance Exceeded IOPS: 1000000 us
EBS Volume Performance Exceeded Throughput: 0 us
EC2 Instance EBS Performance Exceeded IOPS: 0 us
EC2 Instance EBS Performance Exceeded Throughput: 200000 us
"""
EBS_V3_END = EBS_V3_START.replace("100\n", "900000\n").replace("IOPS: 1000000", "IOPS: 31000000").replace("Throughput: 200000", "Throughput: 500000")
SCEN["aws_ebs_throttle"] = dict(SCEN["aws_ebs_cap"], raw={"ebs_stats_start": EBS_START, "ebs_stats_end": EBS_END})
SCEN["aws_ebs_v2"] = dict(SCEN["aws_ebs_cap"], raw={"ebs_stats_start": EBS_V2_START, "ebs_stats_end": EBS_V2_END})
SCEN["aws_ebs_v3"] = dict(SCEN["aws_ebs_cap"], raw={"ebs_stats_start": EBS_V3_START, "ebs_stats_end": EBS_V3_END})
# 22) Dell PERC 12 (mpi3mr) + perccli2: 도구는 돌았지만 JSON 형식을 해석하지 못함 → 원문 보존 안내
SCEN["bm_mpi3mr_unparsed"] = dict(virt=BM_VIRT, hostdrv={"host0": "mpi3mr"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="128", host="host0", vendor="DELL", model="PERC H965i Front", hctl="0:1:0:0",
              lat_r=0.5, lat_w=0.8, iops_r=2000, iops_w=1500, aqu=2, inflight=2, util=40)],
    raw={"raid_storcli": '#CMD /opt/MegaRAID/perccli2/perccli2 /call show all J\n'
                         '{"Controllers":[{"Command Status":{"Status":"Success"},"Response Data":{"Basics":{"Controller":0}}}]}\n'
                         '#TOOL storcli2\n'})

# 23) PERC 12 (mpi3mr) + perccli2, 키 이름·값 표기가 바뀐 형식: bm_raid_storcli 와 같은 결론이어야 함
SCEN["bm_raid_storcli2"] = dict(virt=BM_VIRT, hostdrv={"host0": "mpi3mr"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="128", host="host0", vendor="DELL", model="PERC H965i Front", hctl="0:1:0:0",
              lat_r=8, lat_w=40, iops_r=150, iops_w=250, aqu=5, inflight=5, util=85)],
    raw={"raid_storcli": STORCLI2_TXT})
# 23-2) MegaRAID 9660 (mpi3mr) + storcli2, snake_case 형식, SSD RAID1 정상
SCEN["bm_raid_storcli2_snake"] = dict(virt=BM_VIRT, hostdrv={"host0": "mpi3mr"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="1", qd="128", host="host0", vendor="BROADCOM", model="MR9660-16i", hctl="0:1:0:0",
              lat_r=0.4, lat_w=0.6, iops_r=3000, iops_w=2000, aqu=2, inflight=2, util=40)],
    raw={"raid_storcli": STORCLI2_SNAKE_TXT})
# 24) storcli 에 개수 필드가 함께 있는 형식
SCEN["bm_raid_storcli_cnt"] = dict(SCEN["bm_raid_storcli"], raw={"raid_storcli": STORCLI_CNT_TXT})

# 구축 전 점검: ES 부하 없이 es_disk_bench.sh 결과만 있는 경우.
# fio 가 쓰는 JSON 모양("key" : value, 2칸 들여쓰기)을 흉내 낸다. 셸 요약이 이 모양을 그대로 읽어야 한다
def fio_json(name, r_iops=0, r_bw=0, r_p99_ms=None, w_iops=0, w_bw=0, w_p99_ms=None, sync_p99_ms=None):
    def side(iops, bw, p99):
        d = {"io_bytes": 1, "bw": bw, "iops": iops, "clat_ns": {"min": 1, "mean": 1.0}}
        if p99 is not None:
            d["clat_ns"]["percentile"] = {"50.000000": int(p99 * 3e5), "99.000000": int(p99 * 1e6), "99.900000": int(p99 * 2e6)}
        return d
    job = {"jobname": name, "groupid": 0, "error": 0, "read": side(r_iops, r_bw, r_p99_ms), "write": side(w_iops, w_bw, w_p99_ms)}
    if sync_p99_ms is not None:
        job["sync"] = {"total_ios": 1000, "lat_ns": {"min": 1, "mean": 1.0, "percentile": {"99.000000": int(sync_p99_ms * 1e6)}}}
    return _json.dumps({"fio version": "fio-3.35", "jobs": [job]}, indent=2, separators=(",", " : "))

def bench_set(fsync_ms, rr1_ms, rr_iops=400000, seqw=2500, seqr=3000):
    return {"fsync_4k": fio_json("fsync_4k", w_iops=3000, w_bw=12000, sync_p99_ms=fsync_ms),
            "randread_4k_qd1": fio_json("randread_4k_qd1", r_iops=8000, r_bw=32000, r_p99_ms=rr1_ms),
            "randread_4k": fio_json("randread_4k", r_iops=rr_iops, r_bw=rr_iops * 4, r_p99_ms=1.5),
            "seqwrite_1m": fio_json("seqwrite_1m", w_iops=seqw, w_bw=seqw * 1024),
            "seqread_1m": fio_json("seqread_1m", r_iops=seqr, r_bw=seqr * 1024)}

_IDLE_NVME = [dev("sda", rot="0", qd="32", host="host0", vendor="ATA", model="MZ7L3480HCHQ", timeout="30", iops_r=2, iops_w=4),
              dev("nvme0n1", rot="0", lat_r=0.1, lat_w=0.05, iops_r=3, iops_w=5, aqu=0.1, inflight=0, util=1)]
# 25) 구축 전, NVMe, 벤치 기준 충족
SCEN["predeploy_ok"] = dict(SCEN["bm_nvme_ok"], devs=_IDLE_NVME, bench=bench_set(0.08, 0.12))
# 26) 구축 전, NVMe 인데 동기 쓰기가 느림 (쓰기 캐시 보호가 없는 소비자용 장치 등)
SCEN["predeploy_slow_fsync"] = dict(SCEN["bm_nvme_ok"], devs=_IDLE_NVME, bench=bench_set(4.5, 0.12))
# 27) 구축 전, fio 없이 dd 만: 동기 쓰기 평균이 주의 수준
SCEN["predeploy_dd"] = dict(SCEN["bm_nvme_ok"], devs=_IDLE_NVME, bench={
    "fsync_4k": '{"engine": "dd", "jobs": [{"jobname": "fsync_4k", "write": {"iops": 800.0, "bw": 3200.0}}], "sync_avg_ms": 1.250}\n',
    "seqwrite_1m": '{"engine": "dd", "jobs": [{"jobname": "seqwrite_1m", "write": {"iops": 1500.0, "bw": 1536000.0}}]}\n'})
# 28) 부하가 낮고 벤치도 없음: 판정 보류
SCEN["idle_nobench"] = dict(SCEN["bm_nvme_ok"], devs=_IDLE_NVME)

# 29) ES 9.x 노드: merge 가 시작·끝 모두 대기열에 쌓임 + 벡터 rescoring direct IO 켜짐 (둘 다 참고 수준)
def _es_stats(ts, mq, idx):
    return _json.dumps({"nodes": {"n1": {"timestamp": ts, "name": "es-hot-1",
        "indices": {"indexing": {"index_total": idx, "index_time_in_millis": idx // 10, "throttle_time_in_millis": 0},
                    "merges": {"total_time_in_millis": 1000, "total_throttled_time_in_millis": 100}},
        "thread_pool": {"write": {"rejected": 0}, "search": {"rejected": 0},
                        "merge": {"threads": 4, "queue": mq, "active": 4, "rejected": 0, "largest": 4, "completed": 10}}}}})
SCEN["bm_es_merge_vector"] = dict(SCEN["bm_nvme_ok"], raw={
    "es_stats_start.json": _es_stats(1700000000000, 6, 100000),
    "es_stats_end.json": _es_stats(1700000120000, 9, 700000),
    "es_cmdline": "/usr/share/elasticsearch/jdk/bin/java -Xms16g -Xmx16g -Dvector.rescoring.directio=true org.elasticsearch.bootstrap.Elasticsearch\n"})
# 30) VMware vSAN Hybrid (OSA): 향후 중단 예정 안내
SCEN["vmware_hybrid"] = dict(SCEN["vmware_ok"], storage="hybrid")

# 31) RHEL 패키지 설치 ES (systemd PrivateTmp 로 mount namespace 만 다름): 컨테이너로 오판하면 안 됨.
#     실제 UTM Rocky 9 + ES 8.19 번들에서 발견. 0.10.0 초기 수집기는 es_in_container=1 로 기록했다
SCEN["rhel_service_ns"] = dict(SCEN["kvm_slow"], meta_extra="es_in_container=1\n",
    raw={"es_cgroup": "0::/system.slice/elasticsearch.service\n"})

# 20) bare-metal, OS 기본 도구만으로 보이는 문제 모음:
#     LVM thin pool 92%, nobarrier, swap·snapshot 저장소가 data 디스크, 옆집 프로세스, 느린 flush,
#     megaraid 커널 로그 이벤트(벤더 도구 없음), SCSI 타임아웃 카운터
SCEN["bm_os_only"] = dict(virt=BM_VIRT, hostdrv={"host0": "megaraid_sas"}, governor="performance", tuned="throughput-performance",
    devs=[dev("sda", rot="0", qd="256", host="host0", vendor="DELL", model="PERC H330 Mini", hctl="0:2:0:0", iops_r=20, iops_w=30),
          dev("sdb", rot="0", qd="256", host="host0", vendor="DELL", model="PERC H330 Mini", hctl="0:2:1:0",
              lat_r=1.2, lat_w=2.0, iops_r=1500, iops_w=1200, aqu=3, inflight=3, util=70, flush_ps=40, flush_ms=9.0,
              extra={"device/iotmo_cnt": "0x3", "device/ioerr_cnt": "0x5", "device/state": "running"})],
    dm={"dm-3": {"name": "vg-esdata", "slaves": ["sdb"], "table": "vg-esdata: 0 2097152000 thin 253:2 1"}},
    mount_src="/dev/mapper/vg-esdata",
    meta_extra="es_pid=4242\n",
    klog=["2026-09-27T02:10:11+0900 kernel: megaraid_sas 0000:18:00.0: 8812 (812345678s/0x0008/CRIT) - Battery has failed and cannot support data retention. Please replace the battery"],
    raw={"dmsetup_status": "vg-pool-tpool: 0 2097152000 thin-pool 12 1200/4096 9420/10240 - rw no_discard_passdown queue_if_no_space - 1024\n"
                           "vg-esdata: 0 2097152000 thin 1932735283 2097151999\n",
         "swaps": "Filename Type Size Used Priority\n/var/lib/elasticsearch/swapfile file 8388604 0 -2\n",
         "es_yml": "path.data: /var/lib/elasticsearch\npath.repo: [\"/var/lib/elasticsearch/backup\"]\npath.logs: /var/log/elasticsearch\n",
         "procio_start": "/proc/4242/io:read_bytes: 1000\n/proc/4242/io:write_bytes: 1000\n/proc/4242/comm:java\n"
                         "/proc/777/io:read_bytes: 0\n/proc/777/io:write_bytes: 0\n/proc/777/comm:backup-agent\n",
         "procio_end": "/proc/4242/io:read_bytes: 104858600\n/proc/4242/io:write_bytes: 209716200\n/proc/4242/comm:java\n"
                       "/proc/777/io:read_bytes: 524288000\n/proc/777/io:write_bytes: 1048576\n/proc/777/comm:backup-agent\n"})
SCEN["bm_os_only"]["mounts_opts"] = "rw,noatime,nobarrier"

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
    if sc.get("host_data_mnt"):
        # 컨테이너 안 ES: 호스트에는 data 가 다른 경로(kubelet PV 등)로 마운트돼 있다
        w(S("mounts"), "{} / xfs rw,relatime 0 0\n{} {} xfs rw,noatime 0 0\n".format(root_src, data_src, sc["host_data_mnt"]))
    else:
        w(S("mounts"), "{} / xfs rw,relatime 0 0\n{} /var/lib/elasticsearch ext4 {} 0 0\n".format(root_src, data_src, sc.get("mounts_opts", "rw,noatime")) if sc.get("mounts_opts")
          else "{} / xfs rw,relatime 0 0\n{} /var/lib/elasticsearch xfs rw,noatime 0 0\n".format(root_src, data_src))
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
        if d["hctl"]:
            lines.append("HCTL|{}|{}".format(n, d["hctl"]))
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
    for fn, text in (sc.get("raw") or {}).items():
        w(S(fn), text)
    if sc.get("bench"):
        os.makedirs(os.path.join(out, "bench"), exist_ok=True)
        for fn, text in sc["bench"].items():
            w(os.path.join(out, "bench", fn + ".json"), text)
    if sc.get("data_paths"):
        w(S("data_paths"), "\n".join(sc["data_paths"]) + "\n")
    if sc.get("datadev"):
        w(S("datadev"), "\n".join("DATADEV|" + "|".join(x) for x in sc["datadev"]) + "\n")
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
                mul, aq = 1.0, d["aqu"]
                if d["pattern"]:
                    mul, aq = d["pattern"][(i - 1) % len(d["pattern"])]
                rio, wio = int(d["iops_r"] * mul * dt), int(d["iops_w"] * mul * dt)
                a[0] += rio; a[2] += rio * 16; a[3] += int(rio * d["lat_r"])
                a[4] += wio; a[6] += wio * 64; a[7] += int(wio * d["lat_w"])
                a[9] += int(d["util"] * dt * 10); a[10] += int(aq * dt * 1000)
                a[15] += int(d["flush_ps"] * dt); a[16] += int(d["flush_ps"] * dt * d["flush_ms"])
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
      "es_pid=\nsamples={}\n{}".format(name, sc.get("storage", "auto"), n_s, sc.get("meta_extra", "")))
    w(os.path.join(out, "user_paths"), "")
    w(os.path.join(out, "self_overhead"), "0m0.10s 0m0.05s\n0m0.30s 0m0.20s\n")
    return out

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        print("\n".join(SCEN)); sys.exit(0)
    if len(sys.argv) != 3 or sys.argv[1] not in SCEN:
        print(__doc__); sys.exit(1)
    print(build(sys.argv[1], sys.argv[2]))
