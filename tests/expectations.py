# -*- coding: utf-8 -*-
"""
시나리오별 기대 판정. run_tests.py 가 불러 쓴다.

각 항목은 (시나리오, 기대 플랫폼, 판정 문구 일부, 있어야 할 판정, 없어야 할 판정).
판정은 (심각도, 담당자, 제목 일부) 로 비교한다. 심각도나 담당자에 None 을 주면 비교하지 않는다.
"""

EXPECT = [
    ("vmware_ok", "vmware", "정상",
     [],
     [(None, None, "병목 위치")]),
    ("vmware_outside", "vmware", "성능 저하",
     [("warn", "VMware 관리자", "VM 바깥(하이퍼바이저·vSAN)")],
     []),
    ("vmware_queue", "vmware", "성능 저하",
     [("warn", "서버 담당자", "Guest 쪽 큐가 가득 참")],
     []),
    ("bm_nvme_ok", "baremetal", "정상",
     [],
     [(None, "VMware 관리자", ""), (None, None, "vSAN"), (None, None, "SCSI 명령 타임아웃")]),
    ("bm_nvme_slow", "baremetal", "성능 저하",
     [("warn", None, "NVMe 기준"), ("warn", "하드웨어 담당자", "NVMe 온도가 경고 온도에 도달"),
      ("caution", "하드웨어 담당자", "PCIe 링크"), (None, "서버 담당자", "CPU 주파수 정책")],
     [(None, "VMware 관리자", "")]),
    ("bm_hdd_raid", "baremetal", "성능 저하",
     [(None, None, "HDD 기준"), ("warn", "하드웨어 담당자", "디스크·컨트롤러 자체가 느림"),
      (None, "하드웨어 담당자", "RAID 컨트롤러 쓰기 캐시"), ("warn", "하드웨어 담당자", "커널 로그"),
      ("caution", "ES 설정", "HDD인데 merge 스레드"), ("info", None, "추정으로 판정")],
     [(None, "VMware 관리자", ""), (None, None, "vSAN")]),
    ("bm_md_outlier", "baremetal", "성능 저하",
     [("warn", "하드웨어 담당자", "하나만 느림. 불량 디스크 후보 (sdd)"), ("warn", None, "md1 가 degraded")],
     [(None, "VMware 관리자", "")]),
    ("bm_san", "baremetal", "성능 저하",
     [(None, None, "SSD 기준"), ("warn", "스토리지 관리자", "스토리지 어레이·SAN 경로")],
     [(None, "VMware 관리자", ""), (None, None, "하나만 느림")]),
    ("kvm_slow", "vm", "성능 저하",
     [(None, None, "queue_depth를 제공하지 않음")],
     [(None, "VMware 관리자", ""), (None, None, "root")]),
    ("container", "vm", "",
     [("caution", None, "컨테이너 안에서 실행됨")],
     [(None, "VMware 관리자", "")]),
]


def _match(f, sev, owner, title):
    return (sev is None or f[0] == sev) and (owner is None or f[2] == owner) and title in f[3]


def check(out):
    fails = []
    for name, plat, verdict, must, must_not in EXPECT:
        r = out.get(name)
        if r is None:
            fails.append("{}: 결과 없음".format(name)); continue
        if r["platform"] != plat:
            fails.append("{}: 플랫폼 {} (기대 {})".format(name, r["platform"], plat))
        if verdict and verdict not in r["verdict"]:
            fails.append("{}: 판정 '{}' (기대 '{}' 포함)".format(name, r["verdict"], verdict))
        for sev, owner, title in must:
            if not any(_match(f, sev, owner, title) for f in r["findings"]):
                fails.append("{}: 없음 {}".format(name, (sev, owner, title)))
        for sev, owner, title in must_not:
            hit = [f for f in r["findings"] if _match(f, sev, owner, title)]
            if hit:
                fails.append("{}: 있으면 안 됨 {} → {}".format(name, (sev, owner, title), hit[0]))
    return fails
