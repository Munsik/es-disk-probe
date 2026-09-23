# ES Disk I/O 진단 툴킷 v3

Elasticsearch 노드의 디스크가 "지금 정상인지", 문제라면 "VM 안인지 밖인지"를 Guest OS에서 판정합니다.
VMware + vSAN 환경을 기본으로 가정했고, 물리 서버에서도 동작합니다.

## 구성

| 파일 | 역할 | 서버 영향 |
|---|---|---|
| `es_disk_collect.sh` | **수집 전부** — 로컬 + 클러스터 + 인덱스별 분포 | 읽기만 함. 설정 변경 없음 |
| `es_disk_render.py` | 분석·판정·HTML 생성 (서버 또는 PC) | 서버에서 안 돌려도 됨 |
| `es_cluster_probe.sh` | (선택) 클러스터만 따로 조회 — 노드 접속 불가할 때 | 조회 API GET만 |
| `es_disk_bench.sh` | 최대 능력 측정 (선택) | **부하를 검. 점검 시간에만** |
| `GUARDLINE.md` | 설계·구성·상시 감시 기준과 변경 원칙 | 문서 |

## 실행

데이터 노드에서 이것 하나만 실행하면 됩니다. 로컬 디스크, 클러스터 전체, 인덱스별 분포를 한 번에 수집합니다.

```bash
# 피크 시간대에 실행 (기본 300초 / 5초 간격)
sudo ES_PASSWORD='***' ./es_disk_collect.sh --es-user elastic -d 600

# API Key
sudo ES_API_KEY='base64값' ./es_disk_collect.sh -d 600

# Hybrid vSAN
sudo ./es_disk_collect.sh -s hybrid

# 이 노드만 (클러스터 조회 권한이 없을 때)
sudo ./es_disk_collect.sh --no-cluster
```

클러스터 조회에 실패해도 경고만 남기고 로컬 결과로 리포트를 만듭니다. 필요 권한은 `cluster monitor`입니다.

서버에 python3가 없으면 번들만 PC로 옮겨 렌더합니다.

```bash
python3 es_disk_render.py esdisk_<host>_<시각>.tar.gz
```

선택 사항 두 가지입니다.

```bash
# 최대 능력 측정 — 서비스 투입 전이나 점검 시간에만
sudo ./es_disk_bench.sh -t /data/elasticsearch -s 4G
python3 es_disk_render.py <번들> --bench /tmp/esbench_<host>_<시각>

# 노드에 접속할 수 없을 때, 클러스터만 원격 조회
ES_PASSWORD='***' ./es_cluster_probe.sh --es-url https://es:9200 --es-user elastic
python3 es_disk_render.py --cluster-only /tmp/escluster_<시각>
```

## 한 번 실행으로 얻는 것

| 관점 | 내용 |
|---|---|
| 로컬 (커널) | 응답시간 p95, 대기 I/O, PSI, D 상태, 설정 전수, 커널 로그, VMware 자원 |
| 클러스터 | 노드별 디스크 사용량 비교, 샤드·용량 쏠림, 복구·스냅샷 등 클러스터발 부하 |
| 인덱스 | 이 노드 샤드의 인덱스별 쓰기·merge·검색 분포, ILM phase |

클러스터 스냅샷은 로컬 측정과 **같은 창**으로 찍습니다. 시간대가 어긋난 비교가 아닙니다.

교차 판정으로 아래를 가립니다.

- 디스크가 느린 게 아니라 **이 노드에 샤드가 몰린** 경우
- 한 노드가 아니라 **여러 노드가 동시에** 느려진 경우 (공용 스토리지 의심)
- 쓰기가 **특정 인덱스 하나에 집중**된 경우

## 서버에 주는 영향 (실측)

- 30~40초 측정 시 CPU 0.42초 (2초 간격 기준 CPU 1개의 약 1%, 기본 5초 간격이면 그 이하)
- 메모리 3.6MB, 결과 파일 5초 간격 기준 분당 약 110KB
- 자기 자신을 `nice 19` + `ionice idle`로 낮춰 ES와 경합하지 않음
- 하지 않는 것: drop_caches, sync 강제, fio/dd 부하, 설정 변경, 원시 장치 접근
- 유일한 예외: 수집기가 자기 자신의 우선순위를 낮추는 `renice`/`ionice`. 수집기 프로세스에만 적용되고 종료와 함께 사라집니다
- ES API는 GET만 호출합니다 (`-X`, `--data` 사용 없음). 코드에서 전수 확인 가능
- 출력 경로 여유 공간이 50MB 미만이면 시작하지 않음

## 리포트 구성

1. **판정** — 한 문장 결론과 9개 차원(지연·포화·오류·ES 영향·메모리·설정·VMware·네트워크·클러스터)
2. **먼저 할 일** — 병목 위치와 실제로 손댈 수 있는 항목 최대 3개, 담당자 표시
3. **핵심 수치·시간대별 흐름** — 응답시간·IOPS·대기 I/O·PSI 차트
4. **판정 근거와 조치 안내** — 담당자별로 묶고 근거·이유·조치·출처 표기
5. **Best practice 대조표** — 통과 항목까지 전부. "이것도 확인했나"를 여기서 검증
6. **한계 추정** — bench 없으면 Guest 큐 기준 천장, 있으면 최대 능력 대비 사용률
7. **클러스터 관점**(프로브 결과가 있을 때) — 노드별 비교, 쏠림, 클러스터발 디스크 부하
8. **측정 범위와 한계** — 무엇을 어떻게 쟀고, Guest에서 원리상 못 보는 것

## 판정 읽는 법

| 판정 | 의미 |
|---|---|
| 디스크는 정상입니다 | 응답시간·포화·오류가 기준 안, 불리한 설정 없음 |
| 지금은 버티지만 위험 요인이 있습니다 | 측정값은 괜찮으나 부하·호스트 경합 시 문제 될 설정 존재 |
| ES에 처리 지연 신호가 있지만 디스크 응답은 정상 | 원인이 디스크 외(CPU·heap·bulk·샤드)일 가능성 큼 |
| 성능 저하 징후 | 측정 중 디스크 지연·포화·오류 관측 → 병목 위치 판정 확인 |
| 성능 판정 보류 | 측정 시간대 부하가 낮아 판단 근거 부족 → 피크 때 재측정 |

**병목 위치 판정**: 응답시간이 높을 때 VM 안의 대기 I/O(aqu-sz)가 queue_depth의 80% 이상이면
"Guest 큐 포화(서버에서 해결)", 대기 I/O는 적은데 느리면 "VM 바깥(VMware 관리자 확인)"입니다.

## 기준값 출처

| 항목 | 기준 | 출처 |
|---|---|---|
| 응답시간 | All-Flash p95 5/10/20ms, Hybrid 10/20/30ms | Broadcom KB 389082 기반 실무 기준 (Elastic 공식 수치 아님) |
| readahead | 128KiB | Elastic "Tune for search speed" |
| vm.max_map_count | 최소 262144, 권장 1048576 | Elastic "Bootstrap checks" |
| swap | 비활성 또는 memory_lock 또는 swappiness=1 | Elastic "Disable swapping" |
| heap | RAM 50% 이하, 약 31GB 이하 | Elastic "Set the JVM heap size" |
| 파일 핸들 | 65535 이상 | Elastic "File descriptors" |
| SCSI timeout | 60초 이상 (open-vm-tools 설치 시 180초) | open-vm-tools udev 규칙 |
| PVSCSI queue | cmd_per_lun 254 / ring_pages 32 (큐 포화 시에만) | VMware KB 2053145 |

## Guest에서 볼 수 없는 것

vSAN 스토리지 정책(RAID·FTT·IOPS 제한), ES 복제본 × vSAN 복제의 쓰기 증폭, VM 스냅샷,
ES 노드의 호스트 배치(anti-affinity), vSAN 네트워크·resync, 물리 디스크 상태.
리포트에 "관리자 확인 항목"으로 따로 정리됩니다.
