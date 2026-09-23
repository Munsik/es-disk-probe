# es-disk-probe

**Read-only disk I/O diagnostics for Elasticsearch nodes on VMware vSAN (guest OS level)**

Elasticsearch 노드의 디스크가 지금 정상인지, 문제라면 원인이 VM 안인지 밖인지를 Guest OS에서 판정합니다.
측정 결과를 Elastic·VMware 공식 권장값과 대조해 담당자별 조치 항목까지 HTML 리포트로 냅니다.

> **v0.9.0 (초기 릴리스)** — 컨테이너와 합성 데이터로 검증했습니다. 실제 vSphere Guest와 운영 클러스터 검증은 아직입니다.
> 고객 환경에 적용하기 전에 사내 노드에서 먼저 실행해 보시길 권합니다.

---

## 왜 필요한가

Elasticsearch에서 디스크는 클러스터 안정성을 좌우하지만, "이 노드 디스크가 받아낼 수 있는 한계"를 재는 표준 도구가 없습니다.
특히 VMware vSAN 위에서는 Guest에서 본 지연이 vSAN·하이퍼바이저·가상 SCSI를 모두 거친 결과라, 느린 이유가 어디에 있는지 가려내기 어렵습니다.
VMware 관리자의 협조를 즉시 받기 어려운 현장이 많다는 점도 이 도구를 만든 이유입니다.

Guest OS에서 볼 수 있는 최대치를 모아 판정하고, Guest에서 원리상 볼 수 없는 것은 **VMware 관리자 요청 항목으로 분리**합니다.

---

## 핵심 원칙

| 원칙 | 구현 |
|---|---|
| 시스템을 바꾸지 않음 | `/proc`·`/sys` 읽기와 ES 조회 API(GET)만. 쓰기는 결과 디렉터리 안에만 |
| 서비스에 영향 없음 | 측정 시간 대비 CPU 1개의 약 1% 이하, 메모리 3.6MB, `nice 19` + `ionice idle` |
| 부하 테스트는 분리 | `es_disk_bench.sh`에만 존재. ES 실행 중이면 실행 거부 |
| 조치는 안내만 | 자동 튜닝 없음. 근거·이유·방법·출처를 제시하고 적용은 담당자가 판단 |
| 폐쇄망 동작 | 외부 패키지·CDN 불필요. bash + awk + coreutils, 분석기는 Python 3.6 표준 라이브러리 |

유일한 예외는 수집기가 **자기 자신**의 우선순위를 낮추는 `renice`/`ionice`입니다. 프로세스 종료와 함께 사라집니다.

---

## 빠른 시작

```bash
git clone https://github.com/Munsik/es-disk-probe.git
cd es-disk-probe
chmod +x *.sh

# 데이터 노드에서 피크 시간대에 실행 (기본 300초 / 5초 간격)
sudo ES_PASSWORD='***' ./es_disk_collect.sh --es-user elastic -d 600
```

끝나면 리포트 경로가 출력됩니다.

```
[14:22:31] HTML 리포트: /tmp/esdisk_es-hot-01_20260923_142031/es_disk_report.html
[14:22:31] 완료. 번들: /tmp/esdisk_es-hot-01_20260923_142031.tar.gz
판정: 디스크 성능 저하 징후가 있습니다 · 조치 필요 7건
```

서버에 `python3`가 없으면 번들만 옮겨 PC에서 만듭니다.

```bash
python3 es_disk_render.py esdisk_es-hot-01_20260923_142031.tar.gz
```

---

## 요구사항

| 구분 | 내용 |
|---|---|
| OS | RHEL / CentOS / Rocky 7·8·9, Ubuntu 20.04+ |
| 수집기 | bash 4.2+, awk, coreutils. `curl`은 ES 조회 시에만 |
| 분석기 | Python 3.6+ 표준 라이브러리만 (RHEL 8의 `/usr/libexec/platform-python` 자동 인식) |
| 권한 | root 권장 (ES 프로세스 I/O, 커널 로그, VMware 정보) |
| ES 권한 | `cluster monitor` (`monitoring_user` 수준). 관리자 계정 불필요 |
| 선택 | `sysstat`(과거 이력), `ethtool`(NIC 링), `open-vm-tools`(VMware 자원), `fio`(한계 측정) |

---

## 옵션

```
-d SEC        측정 시간 (기본 300)
-i SEC        샘플 간격 (기본 5)
-p PATH       ES data 경로 (여러 번 지정 가능, 미지정 시 자동 탐지)
-o DIR        결과 저장 위치 (기본 /tmp)
-s TYPE       vSAN 유형: allflash | hybrid (기본 allflash)
--es-url URL  ES 주소 (기본 자동 탐지)
--es-user U   ES 사용자 (비밀번호는 환경변수 ES_PASSWORD, API Key는 ES_API_KEY)
--no-es       ES API 조회 생략
--no-cluster  클러스터 전체 조회 생략 (이 노드만)
--no-render   HTML 생성 생략 (번들만)
```

클러스터 조회에 실패해도 경고만 남기고 로컬 결과로 리포트를 만듭니다.

---

## 구성

| 파일 | 역할 | 서버 영향 |
|---|---|---|
| `es_disk_collect.sh` | **수집 전부** — 로컬 + 클러스터 + 인덱스별 분포 | 읽기만. 설정 변경 없음 |
| `es_disk_render.py` | 분석·판정·HTML 생성 (서버 또는 PC) | 서버에서 안 돌려도 됨 |
| `es_cluster_probe.sh` | (선택) 노드 접속 없이 클러스터만 원격 조회 | 조회 API GET만 |
| `es_disk_bench.sh` | (선택) 최대 능력 측정 | **부하를 검. 점검 시간에만** |
| `GUARDLINE.md` | 설계·구성·상시 감시 기준과 변경 원칙 | 문서 |

---

## 한 번 실행으로 얻는 것

| 관점 | 내용 |
|---|---|
| 로컬 (커널) | 응답시간 p95, 대기 I/O, PSI, D 상태, 설정 전수, 커널 로그, VMware 자원 |
| 클러스터 | 노드별 디스크 사용량 비교, 샤드·용량 쏠림, 복구·스냅샷 등 클러스터발 부하 |
| 인덱스 | 이 노드 샤드의 인덱스별 쓰기·merge·검색 분포, ILM phase |

클러스터 스냅샷은 로컬 측정과 **같은 창**으로 찍습니다.

교차 판정으로 아래를 가립니다.

- 디스크가 느린 게 아니라 **이 노드에 샤드가 몰린** 경우
- 한 노드가 아니라 **여러 노드가 동시에** 느려진 경우 (공용 스토리지 의심)
- 쓰기가 **특정 인덱스 하나에 집중**된 경우

---

## 리포트 구성

1. **판정** — 한 문장 결론과 9개 차원
2. **먼저 할 일** — 병목 위치와 조치 항목 최대 3개, 담당자 표시
3. **핵심 수치·시간대별 흐름** — 응답시간·IOPS·대기 I/O·PSI 차트 (외부 CDN 없음)
4. **판정 근거와 조치 안내** — 담당자별 묶음, 근거·이유·조치·출처
5. **Best practice 대조표** — 통과 항목까지 전부 (약 30개)
6. **한계 추정** — bench 없으면 큐 기준 천장, 있으면 최대 능력 대비 사용률
7. **클러스터 관점 / 인덱스별 분포**
8. **측정 범위와 한계** — 상시 수집 지표와의 차이, Guest에서 못 보는 것

---

## 판정 읽는 법

| 판정 | 의미 |
|---|---|
| 디스크는 정상입니다 | 응답시간·포화·오류가 기준 안, 불리한 설정 없음 |
| 지금은 버티지만 위험 요인이 있습니다 | 측정값은 괜찮으나 부하·호스트 경합 시 문제 될 설정 존재 |
| ES에 처리 지연 신호가 있지만 디스크 응답은 정상 | 원인이 디스크 외(CPU·heap·bulk·샤드)일 가능성 |
| 성능 저하 징후 | 측정 중 디스크 지연·포화·오류 관측 |
| 성능 판정 보류 | 측정 시간대 부하가 낮아 판단 근거 부족 → 피크 때 재측정 |

**병목 위치**: 응답시간이 높을 때 대기 I/O가 queue_depth의 80% 이상이면 "Guest 큐 포화(서버에서 해결)", 대기 I/O는 적은데 느리면 "VM 바깥(VMware 관리자)"입니다.

---

## 기준값 출처

리포트의 모든 항목에 출처 등급을 표시합니다.

| 항목 | 기준 | 출처 |
|---|---|---|
| readahead | 128KiB (LVM·RAID는 수 MiB로 커질 수 있음) | **[Elastic 공식]** Tune for search speed |
| vm.max_map_count | 최소 262144, 권장 1048576 | **[Elastic 공식]** Bootstrap checks |
| swap | 비활성 > memory_lock > swappiness=1 | **[Elastic 공식]** Disable swapping |
| JVM heap | RAM 50% 이하, 약 31GB 이하 | **[Elastic 공식]** Set the JVM heap size |
| 파일 핸들 | 65535 이상 | **[Elastic 공식]** File descriptors |
| disk watermark | 기본 85/90/95% | **[Elastic 공식]** Disk-based shard allocation |
| 스토리지 종류 | 로컬 블록 장치, 원격 FS 회피 | **[Elastic 공식]** Hardware |
| vSAN 지연 | All-Flash 5ms / Hybrid 20ms 미만 정상 | **[VMware 공식]** Broadcom KB 389082 |
| 게스트/VMDK 지연 차이 | 큐 깊이 낮은 컨트롤러의 큐 고갈 가능성 | **[VMware 공식]** Troubleshooting vSAN Performance |
| PVSCSI 큐 | 기본 64(device)/254(adapter), ring_pages 8→32 | **[VMware 공식]** KB 2053145 |
| NIC | VMXNET3 | **[VMware 공식]** KB 1001805 |
| vSAN 네트워크 | 패킷 손실 2%면 스토리지 성능 32% 저하 | **[VMware 공식]** Troubleshooting vSAN Performance |
| I/O 스케줄러 | mq-deadline 또는 none | **[Red Hat 공식]** Setting the disk scheduler |
| 응답시간 3단계 구분 | 주의 / 경고 / 위험 | **[실무 기준]** KB 389082를 기준선으로 단계화. Elastic 공식 수치 없음 |
| PSI 단계 | 5% / 20% | **[실무 기준]** 커널 문서에 임계값 제시 없음 |
| THP | madvise 또는 never | **[참고]** DB 벤더 운영 관행. Elastic 필수 항목 아님 |

---

## 상시 모니터링과의 관계

이 도구는 Elastic의 System/Linux integration을 **대체하지 않습니다.** 장기 추세, 다중 노드 시계열, 경보 자동화는 Elastic 쪽이 맞습니다.
이 도구는 경보가 울린 순간에 한 번 깊게 파는 용도이고, 상시 수집으로는 볼 수 없는 아래를 채웁니다.

- 응답시간 **p95** (상시 지표는 평균값 위주라 짧은 급등이 지워짐)
- **병목 위치** 판정 (대기 I/O ÷ queue_depth)
- 블록 장치 **설정 전수** (readahead·scheduler·timeout·iostats·wbt)
- LVM·파티션 **토폴로지 역추적**
- 커널 로그의 **SCSI abort/reset·hung task**
- VMware **balloon·host swap·예약·limit**
- 스토리지 **IRQ 편중**, **mmap 여유**
- 공식 문서 기준 **전수 대조표**

상시 경보 기준은 `GUARDLINE.md` 4장에 정리했습니다.

---

## Guest에서 볼 수 없는 것

리포트에 "VMware 관리자 확인 항목"으로 분리해 출력합니다.

vSAN 스토리지 정책(RAID·FTT·stripe·IOPS 제한), ES 복제본 × vSAN 복제의 쓰기 증폭, VM 스냅샷,
ES 노드의 호스트 배치(anti-affinity), vSAN 네트워크·resync·캐시 사용률, 물리 디스크 상태, vNUMA.

---

## 검증 현황

| 항목 | 상태 |
|---|---|
| `/proc/diskstats` 계산 정확도 | iostat 12.6과 교차 검증 — r/w_await·aqu-sz·%util 일치 |
| 커널 로그 패턴 | 정탐/오탐 단위 테스트 통과 |
| LVM·NVMe·파티션 토폴로지 | 합성 데이터 단위 테스트 통과 |
| `path.data` 표기 5종 파싱 | 통과 |
| HTML·차트 | 태그 검증 + 가짜 DOM 런타임 검증 통과 |
| 호환성 | Python 3.6 문법, bash 4.2, mawk 검사 통과 |
| 실제 vSphere Guest | **미검증** |
| 운영 클러스터 `fs.io_stats` | **미검증** (모의 서버 기준) |

---

## 트러블슈팅

**ES API 접속 실패 (http=401)**
`--es-user` 와 `ES_PASSWORD`, 또는 `ES_API_KEY`를 지정하세요. 없으면 OS 레벨만 수집합니다.

**클러스터 조회 실패 (http=403)**
`cluster monitor` 권한이 필요합니다. 권한이 없으면 `--no-cluster`로 경고 없이 실행하세요.

**"성능 판정 보류"로 나옴**
측정 시간대 부하가 낮았다는 뜻입니다. 인덱싱·검색 피크 시간대에 `-d 600` 이상으로 다시 실행하세요.

**PSI 미지원 표시 (RHEL 8)**
커널에 포함돼 있지만 기본 비활성입니다. 부트 파라미터 `psi=1` 적용 후 재부팅하면 포화 판정이 정확해집니다. 없어도 D 상태·큐·지연으로 판정합니다.

**리포트가 생성되지 않음**
서버에 Python 3.6+ 가 없는 경우입니다. 번들(`.tar.gz`)을 PC로 옮겨 `es_disk_render.py`를 실행하세요.

---

## 로드맵

- [ ] 실제 vSphere 환경 검증 후 v1.0.0
- [ ] esxtop 출력 대조 가이드
- [ ] 기준선(baseline) 비교 모드 — 이전 번들과의 차이 표시
- [ ] 리포트 영문 출력 옵션

---

## 면책

이 도구는 진단 결과와 조치 안내를 제공할 뿐, 설정을 변경하지 않습니다.
조치 적용은 담당자 검토와 테스트를 거쳐야 하며, 적용 결과에 대한 책임은 사용자에게 있습니다.
Elastic 또는 VMware의 공식 제품이 아닙니다.

## 라이선스

Apache License 2.0
