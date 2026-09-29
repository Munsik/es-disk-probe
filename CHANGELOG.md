# Changelog

## [0.9.5] - 2026-09-29

### 수정
- README의 라이선스 표기가 `Apache License 2.0` 으로 잘못 적혀 있었다. `LICENSE` 파일은
  처음부터 MIT 였으므로 README 를 MIT 로 맞춤. 라이선스 변경이 아니라 표기 오류 정정

## [0.9.4] - 2026-09-29

벤더 문서 커버리지를 제로베이스로 다시 점검해 빠진 항목을 채우고, 과하게 방어적인 문구를 정리.

### 새로 점검하는 항목 (모두 공식 문서 근거)
- `index.translog.durability`. 기본 request 에서는 bulk 요청마다 fsync 를 기다리므로
  디스크 쓰기 지연이 그대로 인덱싱 응답 시간이 된다. 이 리포트가 쓰기 지연을 중요하게 보는 이유를
  리포트 안에서 설명하게 됨. async 로 바꾼 인덱스가 있으면 데이터 유실 범위와 함께 안내
  [Elastic 공식] Translog settings
- `index.merge.scheduler.max_thread_count`. Elastic 은 회전 디스크에 1 을 권고한다.
  Hybrid vSAN(`-s hybrid`)에서 1 이 아니면 안내. VMware 가상 디스크는 백엔드가 all-flash 여도
  `rotational=1` 로 보고하는 경우가 많아, rotational 값만으로 판정하면 전부 오탐이 된다.
  그래서 사용자가 선언한 스토리지 유형을 기준으로 삼고 rotational 은 보조 근거로만 표시
  [Elastic 공식] Merge settings
- `index.store.type`. 기본 hybridfs 와 다르게 지정한 인덱스 표시 [Elastic 공식] Store
- tuned profile. 지금까지 수집만 하고 쓰지 않았다. readahead 판정에 원인 후보로 연결해,
  값을 바꿔도 되돌아오는 경우를 설명할 수 있게 함. udev 규칙 존재 여부도 함께 표시
  [Red Hat 공식] TuneD profiles

### 기준값 출처 보강
- Broadcom KB 424485 추가. 장치 레벨 기대 지연(NVMe 0.5ms 미만, SAS/SATA SSD 1ms 내외,
  HDD 10~20ms)을 VMware 관리자에게 백엔드 확인을 요청할 때 함께 전달하도록 병목 위치 안내에 넣음.
  판정 기준은 그대로 VM 관점 수치(KB 389082)를 쓴다. 이 도구가 재는 것은 Guest 에서 본 지연이라
  백엔드·hypervisor·가상 SCSI 를 지나온 시간이 모두 포함되기 때문
- Elastic "Tune for indexing speed"(SSD, RAID 0, 원격 스토리지 회피)와
  레거시 가상 어댑터 queue depth 32 대 PVSCSI 64 를 출처 표에 명시

### 수집
- `_all/_settings` 1회 추가. `include_defaults` 를 쓰지 않아 명시적으로 바꾼 인덱스만 응답에 들어온다.
  `--no-index-stats` 와 `--light` 에서 함께 생략

### 문서 톤
- 과하게 방어적인 문구 정리. "지원 대상이 아닙니다", "실행 결과로 문제가 생겨도",
  "적용 결과에 대한 책임은 사용자에게 있습니다" 같은 표현을 덜어내고 면책 6줄을 참고사항 3줄로 줄임

## [0.9.3] - 2026-09-29

문서와 리포트 본문이 기계가 쓴 것처럼 읽힌다는 지적을 받아 문체를 손질. 기능 변경 없음.

### 문체
- em dash(`—`) 102개를 전부 제거. 마침표, 콜론, 괄호로 교체.
  목록의 "항목 — 설명"은 "항목: 설명"으로, 공식 문서 경로는
  `Bootstrap checks > Maximum map count` 형태로 바꿈
- README 굵게 강조를 95쌍에서 5쌍으로 줄임. 문단마다 굵게 박혀 있어 강조가 의미를 잃었다
- 번역체 정리. "…라는 점입니다", "…하는 셈이라", "…하기 위한 것입니다",
  "무엇을 하는지 / 하지 않는지" 같은 영어 구조를 한국어 문장으로
- 직역 용어 교체. 천장 → 상한, 블록 계층 → block layer, 측정 창 → 측정 구간, 매핑 목록 → maps.
  기술 용어는 영문 원어를 유지 (page cache, readahead, queue_depth, aqu-sz, inflight,
  segment, throttle, watermark, PSI 등)
- 용어 통일. 툴 → 도구

### 문서 정확성
- README의 리포트 구성 목록이 실제 섹션과 어긋나 있었다. 실제 13개 섹션 기준으로 다시 씀
  (Elasticsearch 쪽 영향, 디바이스별 상세, 과거 7일 이력, 부록이 빠져 있었다)
- `es_disk_bench.sh`, `es_cluster_probe.sh`의 `-h`에 옵션 목록이 없어 추가.
  세 스크립트 모두 실제 파싱하는 옵션과 도움말이 일치함을 확인

### 검증
- 문자열 치환이 판정 로직에 영향을 주지 않았는지 회귀 확인
  (문제 환경 33건, 빈 번들 0건, 손상 번들 25건 모두 이전과 동일)
- 이번에 처음 실행해 본 경로: `--cluster-only`, `--cluster` 외부 디렉터리, `--bench`(fio 결과 5종)
- README에 적힌 기본값 8개가 코드 실제값과 일치함을 대조

## [0.9.2] - 2026-09-28

프로덕션 투입을 전제로 이 도구 자체의 부하를 실측하고, 큰 항목을 줄이고 고지를 추가.

### 부하 실측 (측정 300초 / 5초 간격 · cold cache · ES 로그 530MB 디렉터리)

`getrusage(RUSAGE_CHILDREN)` 로 자식 프로세스까지 포함해 측정.
`ru_inblock`/`ru_oublock` 은 page cache 적중을 제외한 실제 블록 계층 I/O.

| 항목 | v0.9.1 | v0.9.2 | v0.9.2 `--light` |
|---|---|---|---|
| 디스크 읽기 | **96.0 MB** | **12.0 MB** | **0 MB** |
| CPU | 0.93초 | 0.97초 | 0.62초 |
| 디스크 쓰기 | 0.18 MB | 0.39 MB | 0.18 MB |
| 메모리 (수집기) | 4.2 MB | 4.2 MB | 4.2 MB |

### 부하 감소
- ES 서버 로그 읽기 96MB → 12MB (8배): 파일당 끝 32MB를 무제한 개수로 읽던 것을,
  최신 로그 8MB + 직전 로그 2개 × 2MB (합계 상한 12MB)로 변경. 최종 출력이 `tail -100`이라
  대부분의 경우 이 범위에서 충분하다. 읽은 양은 그대로 page cache 에서 밀려나는 양이고,
  ES 노드에서 밀려나는 자리는 segment 캐시라 ES 가 나중에 그만큼 디스크를 더 읽게 된다
- `sar` 파일을 `-d`·`-u` 로 두 번 읽던 것을 한 번으로. `sar_u` 는 리포트에서 쓰이지 않아
  읽는 만큼이 그대로 낭비였다
- `journalctl -k` 에 `-n` 상한(기본 20000) 추가. 장애가 반복되는 노드에서 커널 메시지가
  수십만 줄이 되면 journal 파일을 읽는 것 자체가 디스크 부하가 된다

### 부하 조절 옵션 추가
- `--light`. 디스크를 읽는 부가 수집 전부 생략 (ES 로그·커널 로그·sar·매핑 목록) → 디스크 읽기 0
- `--no-eslog`. ES 서버 로그 읽기만 생략
- `--no-index-stats`. 인덱스 수에 비례해 커지는 ES 조회 2건 생략
  (`_nodes/_local/stats?level=indices`, `_ilm/explain`)
- 환경변수 `ESLOG_TAIL_MB` / `ESLOG_OLD_MB` / `ESLOG_MAX_FILES` / `KLOG_MAX_LINES`

### 부하 고지
- 리포트에 **"이 진단이 서버에 준 부하"** 섹션 신설. 그 실행의 실측 CPU·디스크 읽기·쓰기·
  ES 조회 횟수와 응답 크기를 찍는다. 공유받은 사람이 직접 확인할 수 있게 하기 위한 것
- 수집기가 읽은 바이트 수를 스스로 기록 (`read_eslog_bytes`, `read_sar_bytes`),
  ES 조회 횟수·응답 크기 기록 (`es_api_calls`, `es_api_bytes`),
  매핑 목록 읽기 소요 시간 기록 (`maps_read_ms`)
- 결과 저장 위치가 ES data 와 같은 파일시스템이면 경고하고 리포트에 표시.
  측정 대상 디스크에 쓰기를 더하는 셈이라 측정값이 오염된다
- README에 부하 실측 표, 감소 옵션별 효과, ES 조회 19건의 규모 의존성,
  `/proc/<pid>/maps` 읽기의 mmap_lock 경합(매핑 4만 개 기준 17ms, 실행당 1회) 명시
- README의 메모리 수치 정정. 기존 "3.6MB" 는 수집기만의 값이었고,
  리포트 생성 단계의 python 18.5MB 가 빠져 있었다

### 버그
- `find` 가 심볼릭 링크 디렉터리를 따라가지 않아, `/var/log/elasticsearch` 가 링크인 구성에서
  **ES 서버 로그를 한 줄도 읽지 못하던 문제** (`find -H`)

## [0.9.1] - 2026-09-28

고객 공유 전 전수 점검에서 나온 수정.

### 판정 로직 수정
- 병목 위치 판정을 3단으로 분리: 기존에는 큐 사용률 80%를 기준으로 둘로만 갈라, 사용률 73% 같은 중간 구간에서
  "대기 I/O는 적음"이라는 근거 문장이 실제 수치와 모순됐습니다. 이제 80% 이상 / 40~80% / 40% 미만 세 구간으로 나누고,
  중간 구간은 단정하지 않고 esxtop DAVG·KAVG 분리 확인을 안내합니다. queue_depth를 못 읽으면 "판정 보류"로 처리합니다
- `inflight` 지표 추가: `aqu-sz`는 블록 계층 대기 요청까지 포함해 queue_depth를 넘을 수 있으므로,
  장치에 실제로 넘겨진 I/O 수를 함께 계산해 교차 확인에 사용하고 디바이스 표에도 표시
- 노드 간 쏠림 비교를 같은 data tier 안으로 제한: warm 노드가 hot 노드보다 용량이 큰 것은 정상인데
  "용량 쏠림"으로 잡히는 오탐이 있었습니다. 클러스터 비교 문장의 중앙값도 같은 tier 기준으로 변경
- "디스크 응답은 정상" 판정 조건 강화: 응답시간을 측정하지 못한 경우(`na`)가 정상으로 취급될 수 있었던 경로 제거

### 안정성
- 번들(`.tar.gz`) 추출 시 심볼릭·하드링크 건너뛰기, 번들 루트를 `meta` 파일 위치로 판별
- `uname` 파일이 비어 있을 때 IndexError 발생 가능 구간 제거
- `--es-url` 뒤 슬래시 제거 (`//_cluster/health` 로 요청이 나가던 문제)
- `-p` 로 준 경로에 공백이 있으면 잘리던 문제. 한 줄에 하나씩 별도 파일로 저장
- ES 주소 자동 탐지 시 중복 요청 제거

### es_disk_bench.sh
- `-s` 크기 표기 검증 추가 (`4GB`·`4g` 같은 표기에서 산술 오류가 나던 문제)
- `-r` 값 검증, 파일시스템 용량을 못 읽는 경우 0으로 나누는 문제 방지

### es_cluster_probe.sh
- `--strict-tls` / `--cacert` 추가. 기존 `--insecure`는 아무 동작도 하지 않는 옵션이었고 항상 `curl -k`로 동작했습니다.
  인증서 검증이 필요한 환경에서 선택할 수 있게 함
- `_ilm/explain` 수집 추가 (수집기의 클러스터 조회와 동일하게 맞춤), `-g` 값 검증

### 문서
- README에 **"먼저 읽어 주세요"** 섹션 신설. 비공식 도구 고지, point-in-time vs 상시 모니터링 구분,
  적합/부적합 상황, 서버에 무엇을 하고 하지 않는지, 번들에 담기는 정보, 검증 수준

## [0.9.0] - 2026-09-23

초기 릴리스.

### 수집 (read-only)
- `/proc/diskstats` 직접 계산. sysstat 버전과 무관하게 동작 (iostat 12.6과 교차 검증 완료)
- 로컬 지표: 응답시간 p95, 대기 I/O, PSI, ES 스레드 D 상태, major fault, swap, dirty page
- 설정 전수: readahead, I/O 스케줄러, SCSI timeout, iostats, wbt, queue_depth, 마운트 옵션, 파티션 정렬, cgroup I/O 제한
- 토폴로지: LVM(dm) → 물리 디스크 역추적, 파티션·NVMe 처리
- VMware: PVSCSI 여부, balloon, host swap, 메모리 예약·limit, CPU steal
- 커널 로그: SCSI abort/reset, hung task, I/O error, 파일시스템 오류 분류
- ES: node stats 차분, 서버 로그, mmap 사용량, 인덱스별 쓰기 분포 + ILM phase
- 클러스터: 노드별 디스크 사용량 비교, 샤드·용량 쏠림, 복구·스냅샷 (로컬 측정과 같은 창)
- 과거 이력: sysstat sar 7일치 (추가 부하 없음)

### 판정
- 5단계 판정 + 9개 차원 스코어
- 병목 위치 분리: Guest 큐 포화 vs VM 바깥
- 교차 판정: 샤드 쏠림 ↔ 로컬 부하, 다중 노드 동시 저하, 인덱스 쓰기 집중
- 부하 부족 시 "판정 보류" (거짓 정상 방지)
- Best practice 대조표 약 30개 항목, 출처 등급 표시

### 리포트
- 단일 HTML, 외부 CDN 없음 (폐쇄망)
- SVG 차트 6종 자체 구현, 인쇄 대응
- 담당자별 조치 그룹화 (서버 / VMware 관리자 / ES 설정)

### 선택 도구
- `es_disk_bench.sh`. fio 기반 한계 측정. ES 실행 중 실행 거부
- `es_cluster_probe.sh`. 노드 접속 없이 클러스터만 원격 조회

### 알려진 제약
- 실제 vSphere Guest 미검증
- 운영 클러스터 `fs.io_stats` 미검증 (모의 서버 기준)
- 응답시간 단계 구분과 PSI 임계값은 실무 기준 (공식 수치 없음)
