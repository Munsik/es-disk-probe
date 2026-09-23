# Changelog

## [0.9.0] - 2026-09-23

초기 릴리스.

### 수집 (read-only)
- `/proc/diskstats` 직접 계산 — sysstat 버전과 무관하게 동작 (iostat 12.6과 교차 검증 완료)
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
- `es_disk_bench.sh` — fio 기반 한계 측정. ES 실행 중 실행 거부
- `es_cluster_probe.sh` — 노드 접속 없이 클러스터만 원격 조회

### 알려진 제약
- 실제 vSphere Guest 미검증
- 운영 클러스터 `fs.io_stats` 미검증 (모의 서버 기준)
- 응답시간 단계 구분과 PSI 임계값은 실무 기준 (공식 수치 없음)
