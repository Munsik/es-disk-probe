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

EXPECT += [
    ("bm_raid_storcli", "baremetal", "성능 저하",
     [("warn", "하드웨어 담당자", "배터리·캐시 보호 모듈"), ("warn", "하드웨어 담당자", "write-through로 동작 중 (설정은 write-back)"),
      (None, "하드웨어 담당자", "패리티 RAID"), ("warn", "하드웨어 담당자", "RAID 구성 디스크에 이상 징후"),
      (None, None, "HDD 기준")],
     [(None, None, "추정으로 판정"), (None, None, "도구가 없어")]),
    ("bm_raid_ssacli", "baremetal", "정상",
     [],
     [(None, None, "추정으로 판정"), (None, None, "HDD"), (None, "하드웨어 담당자", "RAID")]),
    ("bm_raid_arcconf", "baremetal", "",
     [("warn", "하드웨어 담당자", "RAID 논리 디스크가 정상 상태가 아님 (sda"), ("warn", None, "RAID 구성 디스크")],
     []),
    ("bm_raid_notool", "baremetal", "",
     [("info", None, "도구가 없어"), ("info", None, "추정으로 판정")],
     []),
    ("vmware_default", "vmware", "성능 저하",
     [(None, None, "VMware 공유 스토리지 기준"), ("warn", "VMware 관리자", "vSAN·데이터스토어 스토리지")],
     []),
    ("vmware_vmfs", "vmware", "성능 저하",
     [("warn", "VMware 관리자", "VM 바깥(하이퍼바이저·데이터스토어 스토리지)")],
     [(None, None, "vSAN")]),
    ("aws_ebs_cap", "vm", "",
     [("caution", "가상화·클라우드 관리자", "상한에서 더 오르지 않음"), (None, None, "클라우드 블록 볼륨 기준")],
     [(None, "VMware 관리자", "")]),
    ("eck_host", "baremetal", "정상",
     [("info", None, "컨테이너(Docker·Kubernetes) 안에서 실행 중")],
     [(None, None, "컨테이너 안에서 실행됨")]),
    ("bm_ceph_rbd", "baremetal", "위험 요인",
     [(None, None, "네트워크 블록 스토리지 기준"), (None, "스토리지 관리자", "네트워크 블록 스토리지(Ceph RBD 등)")],
     [(None, None, "NVMe")]),
]

EXPECT += [
    ("bm_os_only", "baremetal", "성능 저하",
     [("warn", None, "flush(장치 캐시 비우기)가 느림"), ("caution", None, "ES 가 아닌 프로세스"),
      ("warn", None, "barrier"), ("warn", None, "thin pool 위에 있음 (데이터 92%"),
      ("caution", None, "swap 이 ES data 디스크"), ("caution", "ES 설정", "path.repo"),
      ("caution", None, "명령 타임아웃 기록 (sdb 3회)"), ("warn", None, "RAID 컨트롤러 이벤트")],
     [(None, "VMware 관리자", "")]),
]

EXPECT += [
    ("aws_ebs_throttle", "vm", "",
     [("warn", "가상화·클라우드 관리자", "EBS 볼륨 성능 한도를 넘긴 시간이 있음")],
     [(None, None, "EC2 인스턴스의 EBS 성능 한도")]),
    ("bm_mpi3mr_unparsed", "baremetal", "",
     [("info", None, "결과 형식을 해석하지 못함")],
     [(None, None, "도구가 없어")]),
]

_STORCLI_MUST = [("warn", "하드웨어 담당자", "배터리·캐시 보호 모듈"), ("warn", "하드웨어 담당자", "write-through로 동작 중 (설정은 write-back)"),
                 (None, "하드웨어 담당자", "패리티 RAID"), ("warn", "하드웨어 담당자", "RAID 구성 디스크에 이상 징후"),
                 (None, None, "HDD 기준")]
EXPECT += [
    ("bm_raid_storcli2", "baremetal", "성능 저하", _STORCLI_MUST,
     [(None, None, "추정으로 판정"), (None, None, "도구가 없어"), (None, None, "해석하지 못함")]),
    ("bm_raid_storcli_cnt", "baremetal", "성능 저하", _STORCLI_MUST,
     [(None, None, "추정으로 판정"), (None, None, "해석하지 못함")]),
]

EXPECT += [(n, "vm", "",
            [("warn", "가상화·클라우드 관리자", "EBS 볼륨 성능 한도를 넘긴 시간이 있음")],
            [(None, None, "EC2 인스턴스의 EBS 성능 한도")]) for n in ("aws_ebs_v2", "aws_ebs_v3")]

EXPECT += [
    ("predeploy_ok", "baremetal", "부하 전 점검: 스토리지가 ES 기준을 충족합니다", [], [(None, None, "한 건 지연이 기준보다 큼")]),
    ("predeploy_slow_fsync", "baremetal", "부하 전 점검: 스토리지가 ES 기준보다 느립니다",
     [("warn", "하드웨어 담당자", "동기 쓰기(fsync) 한 건 지연이 기준보다 큼")], []),
    ("predeploy_dd", "baremetal", "부하 전 점검: 대체로 충족하지만 확인할 항목이 있습니다",
     [("caution", None, "동기 쓰기(fsync) 한 건 지연이 기준보다 큼 (벤치 평균")], []),
    ("idle_nobench", "baremetal", "성능 판정은 보류", [], []),
]

EXPECT += [
    ("bm_raid_storcli2_snake", "baremetal", "정상",
     [],
     [(None, None, "추정으로 판정"), (None, None, "해석하지 못함"), (None, "하드웨어 담당자", "RAID")]),
]

EXPECT += [
    ("bm_es_merge_vector", "baremetal", "정상",
     [("info", None, "merge 가 대기열에 쌓여 있음"), ("info", None, "direct IO")],
     [(None, None, "ES 지표 미수집")]),
    ("vmware_hybrid", "vmware", "",
     [("info", "VMware 관리자", "vSAN Hybrid(OSA) 는 향후 VCF 릴리스에서 중단 예정")],
     []),
]

EXPECT += [
    ("rhel_service_ns", "vm", "",
     [],
     [(None, None, "컨테이너(Docker·Kubernetes) 안에서 실행 중"), (None, None, "컨테이너 안에서 실행됨")]),
]
