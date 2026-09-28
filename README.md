# es-disk-probe

Read-only disk I/O diagnostics for Elasticsearch nodes on VMware vSAN (guest OS level)

Elasticsearch 노드의 디스크가 지금 정상인지, 문제라면 원인이 VM 안인지 밖인지를 Guest OS에서 판정합니다.
측정 결과를 Elastic·VMware 공식 권장값과 대조해 담당자별 조치 항목까지 HTML 리포트로 냅니다.

v0.9.4 · 비공식 도구 · 읽기 전용 · 한 시점을 보는 진단 도구 (상시 모니터링 도구가 아닙니다)

---

## 먼저 읽어 주세요

### 이 도구의 성격

Elastic이나 VMware의 공식 제품이 아닙니다. 현장에서 필요해서 직접 만든 진단 스크립트입니다.

리포트가 인용하는 기준값은 Elastic, VMware, Red Hat 공식 문서에서 가져왔고 항목마다 출처를 적었습니다.
다만 그 기준값을 조합해 내리는 판정과 조치 안내는 이 도구의 해석입니다.
리포트의 모든 항목에는 `[Elastic 공식]`, `[VMware 공식]`, `[Red Hat 공식]`, `[실무 기준]`, `[참고]` 중
하나가 붙어 있습니다. 고객에게 전달할 때는 공식 표기가 있는 항목을 권고로 쓰고, 나머지는 검토 의견으로 다루시면 됩니다.

### 한 시점을 보는 도구입니다

상시 모니터링을 대체하지 않습니다. 성격이 다릅니다.

| | 이 도구 | 상시 모니터링 (Elastic System/Linux integration) |
|---|---|---|
| 성격 | 한 번 실행하고 리포트 한 장 | 계속 수집해서 시계열·경보 |
| 실행 방식 | 필요할 때 손으로 실행 (기본 5분) | agent 상주 |
| 강점 | 그 시점을 깊게 (p95, 설정 전수, 병목 위치) | 장기 추세, 여러 노드 비교, 자동 경보 |
| 볼 수 없는 것 | 측정한 시간대 밖의 일 | 짧은 지연 급등, 설정값, 커널 로그 대조 |

cron이나 systemd timer로 반복 실행하도록 만들지 마세요. 측정 구간이 짧아서 추세 판단에는 쓸 수 없고,
매번 따로 떨어진 리포트만 쌓입니다. 상시 감시가 필요하면 Elastic Agent를 쓰시고,
경보 임계값은 이 도구와 같은 기준으로 `GUARDLINE.md` 4장에 정리해 두었습니다.

리포트도 그 시점의 스냅샷입니다. 설정을 바꾸거나 부하가 달라지면 다시 측정해야 합니다.

### 언제 쓰면 되는지

쓸 만한 상황입니다.

- ES 노드에서 디스크 관련 장애나 경보가 났고, 원인이 VM 안인지 vSAN·호스트 쪽인지 갈라야 할 때
- 인덱싱 지연이나 검색 지연의 원인이 디스크인지 확인해야 할 때
- VMware 관리자에게 "느리다"가 아니라 측정 근거를 들고 가야 할 때
- 신규 구축이나 증설 직전에 Guest OS 설정과 VM 구성이 권고에 맞는지 한 번 훑을 때
- 폐쇄망이라 외부 도구를 들이기 어려울 때

이럴 때는 쓰지 마세요.

- 디스크가 원인이라고 이미 확인한 뒤의 튜닝 작업. 이 도구는 원인을 가리는 데까지만 씁니다
- 상시 감시 목적. 위 표를 봐 주세요
- 용량 사이징 근거. 최대 성능을 재는 쪽은 `es_disk_bench.sh`(부하 발생)이고, 기본 수집은 현재 부하만 봅니다
- 디스크 밖의 성능 문제. CPU, heap, GC, 쿼리 튜닝은 범위가 아닙니다. 다만 "디스크가 원인이 아니다"까지는 판정합니다
- 물리 서버(베어메탈). 동작은 하지만 판정 기준이 vSAN 지연값(All-Flash 5ms)이라 VMware 관련 항목이 전부 의미가 없어집니다

### 서버에서 하는 일과 하지 않는 일

하지 않습니다.

- 시스템 설정 변경. sysctl, /sys, mount 옵션, ES 설정 어느 것도 바꾸지 않습니다
- ES에 쓰기 요청. 조회 API(`GET`)만 씁니다. 인덱스 생성, 설정 변경, 재시작 없습니다
- `drop_caches`, 강제 `sync`, raw device 접근
- 외부 네트워크 통신. 지정한 ES 주소 외에는 아무 곳에도 접속하지 않습니다
- 디스크에 부하를 거는 테스트. 그건 `es_disk_bench.sh`이고 별개입니다

합니다.

- `/proc`, `/sys` 읽기, ES 조회 API 호출, 결과 디렉터리(기본 `/tmp`)에 파일 쓰기
- ES 서버 로그와 sar 기록의 끝부분 읽기 (아래 부하 표를 봐 주세요)
- 자기 자신의 우선순위를 낮추는 `renice 19`, `ionice idle`. 프로세스가 끝나면 사라집니다

---

## 이 도구가 서버에 주는 부하

프로덕션 노드에서 돌리는 도구라 직접 측정했습니다.
기본값(측정 300초, 5초 간격)으로 ES 로그 530MB가 쌓인 디렉터리를 대상으로,
page cache를 비운 상태에서 잰 값입니다.
측정에는 `getrusage(RUSAGE_CHILDREN)`을 썼습니다. 자식 프로세스까지 포함한 CPU와 최대 RSS,
그리고 `ru_inblock`, `ru_oublock`(page cache 적중을 뺀 실제 block layer I/O)입니다.

| 항목 | 기본 모드 | `--light` | 비고 |
|---|---|---|---|
| CPU | 0.97초 | 0.62초 | 측정 300초 대비 CPU 1개의 0.32% |
| 디스크 읽기 | 12.0 MB | 0 MB | 전량이 ES 서버 로그. sampling 구간은 0 |
| 디스크 쓰기 | 0.39 MB | 0.18 MB | 결과 파일 (`-o` 위치) |
| 메모리 (최대 RSS) | 수집기 4.2 MB | 4.2 MB | 리포트를 만드는 python은 별도로 18.5 MB |
| ES 조회 | 최대 19회 GET | 0회 | 쓰기 요청 0건 |

sampling 구간은 디스크를 읽지 않습니다. 5초마다 `/proc/diskstats`, `/proc/stat`, `/proc/pressure/io`,
ES 스레드의 `/proc/<pid>/task/*/stat` 같은 것을 읽는데, 커널이 메모리에서 만들어 주는 값이라
block layer I/O가 생기지 않습니다. 한 번 샘플링할 때 새로 뜨는 프로세스는 `awk` 1개와 `sleep` 1개뿐이고
CPU는 약 5ms입니다. 그래서 측정 시간을 300초에서 600초로 늘려도 CPU만 약 0.3초 늘고 디스크 읽기는 그대로입니다.

디스크 읽기 12MB는 전부 ES 서버 로그입니다. 가장 최신 로그의 끝 8MB, 그 앞 로그 2개의 끝 2MB씩
해서 최대 12MB를 읽고 throttle, watermark, flush 실패 기록을 찾습니다.
읽은 양은 그대로 page cache에서 밀려나는 양이 되는데, ES 노드에서 밀려나는 자리는 segment 캐시입니다.
결국 ES가 나중에 그만큼 디스크를 더 읽게 됩니다. 그래서 상한을 두었고, 필요하면 더 줄일 수 있습니다.

| 옵션 | 효과 | 잃는 것 |
|---|---|---|
| `--no-eslog` | 디스크 읽기 12MB에서 0 MB로 | ES 로그 기반 판정 (throttle, watermark 기록) |
| `--light` | 위 + 커널 로그, sar, maps 읽기 생략 | 과거 장애 흔적, 7일 이력, mmap 여유 판정 |
| `--no-index-stats` | 인덱스 수에 비례하는 ES 조회 2건 생략 | 인덱스별 쓰기 분포, ILM phase |
| `ESLOG_TAIL_MB=2` | 로그 읽기 12MB에서 6MB로 | 오래된 로그 이벤트 |

ES 쪽 부하는 GET 19회가 전부이고 모두 모니터링용 조회입니다. 쓰기 요청은 한 건도 없습니다.
이 중 클러스터 규모에 따라 비용이 커지는 것은 세 개입니다.

| 조회 | 호출 수 | 비용이 커지는 조건 |
|---|---|---|
| `_nodes/_local/stats?level=indices` | 2 (시작·종료) | 이 노드의 인덱스 수에 비례 |
| `_ilm/explain?only_managed=true` | 1 | ILM 관리 인덱스 수에 비례 |
| `_nodes/stats` (클러스터 전체) | 2 | 노드 수에 비례 |
| 나머지 14회 (`_cat/*`, `_cluster/health`, `_snapshot/_status` 등) | 각 1~2 | 무시할 수준 |

인덱스가 수천 개인 클러스터라면 `--no-index-stats`로 앞의 두 개를 빼거나,
`--no-cluster`로 클러스터 조회를 전부 생략해 9회로 줄일 수 있습니다.
`_snapshot/_status`는 인자 없이 호출하므로 실행 중인 snapshot만 보는 가벼운 형태입니다.
저장소를 읽는 무거운 형태가 아닙니다.

ES 프로세스에 직접 닿는 동작은 하나입니다. `/proc/<pid>/maps`를 실행할 때마다 한 번 읽습니다.
매핑이 4만 개인 프로세스에서 약 17ms 걸렸고, 그동안 그 프로세스의 `mmap_lock`을 read 모드로 잡습니다.
ES가 segment를 열거나 닫을 때(`mmap`, `munmap`) 잠깐 경합하는 정도입니다. 이것도 피하려면 `--light`를 쓰세요.
나머지 `/proc/<pid>/io`, `/proc/<pid>/task/*/stat` 읽기는 lock을 잡지 않습니다.

결과 저장 위치는 ES data와 다른 디스크로 두세요. 같은 파일시스템이면 측정 대상 디스크에 쓰기를 더하게 되고
그만큼 측정값이 오염됩니다. 도구가 이 상황을 감지하면 경고하고 리포트에도 표시합니다.

실행할 때마다 그 실행의 실측 부하가 리포트의 "이 진단이 서버에 준 부하" 섹션에 찍힙니다.
리포트를 받은 사람이 직접 확인할 수 있게 넣었습니다.

처음 쓰실 때는 사내 노드나 개발 노드에서 한 번 돌려 보고 출력을 확인하시면 좋습니다.

### 부하를 거는 도구는 따로 있습니다

`es_disk_bench.sh`는 fio로 실제 부하를 겁니다. 위 내용은 이 스크립트에 해당하지 않습니다.

- ES가 떠 있으면 실행을 거부합니다. `--force-with-es`로만 우회됩니다
- vSAN은 여러 호스트가 공유하는 스토리지라서 같은 클러스터의 다른 VM에도 영향이 갈 수 있습니다. VMware 관리자와 시간을 맞추세요
- 테스트 파일이 vSAN 캐시 계층에 들어가면 결과가 실제보다 좋게 나옵니다. 상한으로만 해석하세요

### 결과물에 들어가는 정보

번들(`.tar.gz`)에는 호스트명, OS·커널 버전, mount 경로, 인덱스 이름, ES 노드 이름과 IP,
커널 로그와 ES 로그 발췌가 들어갑니다.
`elasticsearch.yml`은 `password`, `secret`, `token`, `key`가 들어간 줄을 빼고 필요한 키만 추출합니다.
사외로 내보낼 일이 있으면 번들을 한 번 열어 보시는 편이 안전합니다.

### 검증 수준

컨테이너와 합성 데이터로 검증했습니다. `/proc/diskstats` 계산은 iostat 12.6과 교차 검증했고
계산식 단위 테스트를 통과했습니다. 실제 vSphere Guest와 운영 클러스터 검증은 아직 남아 있습니다.
상세 내역은 아래 [검증 현황](#검증-현황) 표에 있습니다.

---

## 왜 필요한가

Elasticsearch에서 디스크는 클러스터 안정성을 좌우하는데, 이 노드 디스크가 어디까지 받아낼 수 있는지
재는 표준 도구가 없습니다.
특히 VMware vSAN 위에서는 Guest에서 본 지연이 vSAN, hypervisor, 가상 SCSI를 모두 거친 결과라서
느린 이유가 어디에 있는지 가려내기 어렵습니다.
VMware 관리자의 협조를 바로 받기 어려운 현장이 많다는 것도 이 도구를 만든 이유입니다.

Guest OS에서 볼 수 있는 것을 최대한 모아 판정하고, Guest에서 원리상 볼 수 없는 것은
VMware 관리자 요청 항목으로 따로 분리합니다.

---

## 핵심 원칙

| 원칙 | 구현 |
|---|---|
| 시스템을 바꾸지 않음 | `/proc`, `/sys` 읽기와 ES 조회 API(GET)만. 쓰기는 결과 디렉터리 안에만 |
| 서비스에 영향 없음 | 실측 CPU 0.97초(300초 측정 시 CPU 1개의 0.32%), 메모리 4.2MB, 디스크 읽기 12MB, `nice 19` + `ionice idle`. [부하 실측 상세](#이-도구가-서버에-주는-부하) |
| 부하 테스트는 분리 | `es_disk_bench.sh`에만 있음. ES 실행 중이면 실행 거부 |
| 조치는 안내만 | 자동 튜닝 없음. 근거, 이유, 방법, 출처를 제시하고 적용은 담당자가 판단 |
| 폐쇄망 동작 | 외부 패키지나 CDN 필요 없음. bash + awk + coreutils, 분석기는 Python 3.6 표준 라이브러리 |

한 가지 예외는 수집기가 자기 자신의 우선순위를 낮추는 `renice`, `ionice`입니다.
프로세스가 끝나면 함께 사라집니다.

---

## 빠른 시작

```bash
git clone https://github.com/Munsik/es-disk-probe.git
cd es-disk-probe
chmod +x *.sh

# 데이터 노드에서 피크 시간대에 실행 (기본 300초, 5초 간격)
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
| 수집기 | bash 4.2+, awk, coreutils. `curl`은 ES 조회할 때만 |
| 분석기 | Python 3.6+ 표준 라이브러리만 (RHEL 8의 `/usr/libexec/platform-python` 자동 인식) |
| 권한 | root 권장 (ES 프로세스 I/O, 커널 로그, VMware 정보) |
| ES 권한 | `cluster monitor` (`monitoring_user` 수준). 관리자 계정 필요 없음 |
| 선택 | `sysstat`(과거 이력), `ethtool`(NIC ring), `open-vm-tools`(VMware 자원), `fio` + `libaio`(최대 성능 측정) |

`es_disk_collect.sh`와 `es_cluster_probe.sh`는 기본적으로 `curl -k`로 동작합니다.
자체 서명 인증서 환경을 감안한 기본값입니다. 수집기는 localhost만 보므로 그대로 두어도 무리가 없습니다.
원격으로 조회하는 `es_cluster_probe.sh`에서 인증서 검증이 필요하면 `--strict-tls`나 `--cacert <파일>`을 쓰세요.

---

## 옵션

```
-d SEC        측정 시간 (기본 300)
-i SEC        샘플 간격 (기본 5)
-p PATH       ES data 경로 (여러 번 지정 가능, 안 주면 자동 탐지)
-o DIR        결과 저장 위치 (기본 /tmp)
-s TYPE       vSAN 유형: allflash | hybrid (기본 allflash)
--es-url URL  ES 주소 (기본 자동 탐지)
--es-user U   ES 사용자 (비밀번호는 환경변수 ES_PASSWORD, API Key는 ES_API_KEY)
--no-es       ES API 조회 생략
--no-cluster  클러스터 전체 조회 생략 (이 노드만)
--no-render   HTML 생성 생략 (번들만)

부하를 줄이는 옵션 (위 "이 도구가 서버에 주는 부하" 참조)
--light           디스크를 읽는 부가 수집 전부 생략. 디스크 읽기 0
--no-eslog        ES 서버 로그 읽기만 생략. 읽기량의 대부분
--no-index-stats  인덱스 수에 비례하는 ES 조회 2건 생략
```

환경변수로 세부 조정할 수 있습니다.
`ESLOG_TAIL_MB`(최신 로그 읽기 상한, 기본 8), `ESLOG_OLD_MB`(직전 로그, 기본 2),
`ESLOG_MAX_FILES`(대상 파일 수, 기본 3), `KLOG_MAX_LINES`(커널 로그 줄 상한, 기본 20000).

클러스터 조회에 실패해도 경고만 남기고 로컬 결과로 리포트를 만듭니다.

---

## 구성

| 파일 | 역할 | 서버 영향 |
|---|---|---|
| `es_disk_collect.sh` | 수집 전부. 로컬 + 클러스터 + 인덱스별 분포 | 읽기만. 설정 변경 없음 |
| `es_disk_render.py` | 분석, 판정, HTML 생성 (서버 또는 PC) | 서버에서 안 돌려도 됨 |
| `es_cluster_probe.sh` | (선택) 노드 접속 없이 클러스터만 원격 조회 | 조회 API GET만 |
| `es_disk_bench.sh` | (선택) 최대 성능 측정 | 부하를 검. 점검 시간에만 |
| `GUARDLINE.md` | 설계, 구성, 상시 감시 기준과 변경 원칙 | 문서 |

---

## 한 번 실행으로 얻는 것

| 관점 | 내용 |
|---|---|
| 로컬 (커널) | 응답시간 p95, aqu-sz, PSI, D 상태, 설정 전수, 커널 로그, VMware 자원 |
| 클러스터 | 노드별 디스크 사용량 비교, 샤드·용량 쏠림, recovery·snapshot 같은 클러스터발 부하 |
| 인덱스 | 이 노드 샤드의 인덱스별 쓰기, merge, 검색 분포, ILM phase |

클러스터 스냅샷은 로컬 측정과 같은 시간대로 찍습니다.

교차 판정으로 아래를 가려냅니다.

- 디스크가 느린 게 아니라 이 노드에 샤드가 몰린 경우
- 한 노드가 아니라 여러 노드가 동시에 느려진 경우 (공용 스토리지 의심)
- 쓰기가 특정 인덱스 하나에 집중된 경우

---

## 리포트 구성

맨 위에 한 문장 판정과 9개 항목 요약(지연, 포화, 오류, ES 영향, 메모리·캐시, 설정, VMware 자원,
네트워크, 클러스터)이 오고, 이어서 담당자를 표시한 "먼저 할 일" 최대 3개가 나옵니다.
그다음은 아래 순서입니다.

1. 핵심 수치, 시간대별 흐름. 응답시간, IOPS, aqu-sz, PSI 차트 (외부 CDN 없음)
2. 판정 근거와 조치 안내. 담당자별 묶음, 근거·이유·조치·출처
3. Best practice 대조표. 통과한 항목까지 전부 (약 30개)
4. Elasticsearch 쪽 영향. 측정 구간의 지표 변화량
5. 한계 추정. bench 결과가 없으면 큐 기준 상한, 있으면 "최대 능력 대비 사용률"로 바뀝니다
6. 클러스터 관점, 이 노드 디스크를 쓰는 인덱스
7. 디바이스별 상세, 과거 7일 이력(sar)
8. 이 진단이 서버에 준 부하. 그 실행의 실측값
9. 측정 범위와 한계. 상시 수집 지표와의 차이, Guest에서 못 보는 것
10. 부록. 커널·메모리 설정 원본, 커널 로그 발췌

클러스터 조회나 sar 기록이 없으면 해당 섹션은 빠집니다.

---

## 판정 읽는 법

| 판정 | 의미 |
|---|---|
| 디스크는 정상입니다 | 응답시간, 포화, 오류가 모두 기준 안이고 불리한 설정도 없음 |
| 지금은 버티지만 위험 요인이 있습니다 | 측정값은 괜찮으나 부하가 늘거나 호스트가 경합하면 문제 될 설정이 있음 |
| ES에 처리 지연 신호가 있지만 디스크 응답은 정상 | 원인이 디스크 밖(CPU, heap, bulk, 샤드)일 가능성 |
| 성능 저하 징후 | 측정 중에 디스크 지연, 포화, 오류를 관측 |
| 성능 판정 보류 | 측정한 시간대의 부하가 낮아 판단 근거가 부족. 피크 때 다시 측정 |

병목 위치는 응답시간이 기준을 넘었을 때만 판정합니다.
큐 사용률(aqu-sz ÷ queue_depth 합계)로 세 구간으로 나눕니다.

| 큐 사용률 | 판정 | 다음 단계 |
|---|---|---|
| 80% 이상 | Guest 쪽 큐가 가득 참 | 서버 담당자. VMDK 분할 + 별도 PVSCSI 컨트롤러, queue depth 상향 |
| 40 ~ 80% | 큐도 깊고 지연도 높음 | 단정하지 않습니다. esxtop의 DAVG(백엔드)와 KAVG(큐 대기)를 나눠 확인 |
| 40% 미만 | VM 바깥 가능성 높음 | VMware 관리자. vSAN, 호스트 경합, resync 확인 |
| queue_depth 미확인 | 판정 보류 | root 권한으로 다시 측정 |

`aqu-sz`는 block layer에서 대기하는 요청까지 포함한 시간 평균이라 queue_depth를 넘을 수 있습니다.
그래서 장치에 실제로 넘어간 I/O 수(`inflight`)를 함께 표시하고, 둘 중 하나라도 80%를 넘으면 큐 포화로 봅니다.
구간 경계인 40%와 80%는 실무 기준이고 공식 수치가 아닙니다.

---

## 기준값 출처

리포트의 모든 항목에 출처 등급을 표시합니다.

| 항목 | 기준 | 출처 |
|---|---|---|
| readahead | 128KiB (LVM·RAID는 수 MiB로 커질 수 있음) | [Elastic 공식] Tune for search speed |
| vm.max_map_count | 최소 262144, 권장 1048576 | [Elastic 공식] Bootstrap checks |
| swap | 비활성 > memory_lock > swappiness=1 | [Elastic 공식] Disable swapping |
| JVM heap | RAM 50% 이하, 약 31GB 이하 | [Elastic 공식] Set the JVM heap size |
| 파일 핸들 | 65535 이상 | [Elastic 공식] File descriptors |
| disk watermark | 기본 85/90/95% | [Elastic 공식] Disk-based shard allocation |
| 스토리지 종류 | 로컬 block device, 원격 파일시스템 회피 | [Elastic 공식] Hardware |
| translog durability | 기본 request는 요청마다 fsync, async는 sync_interval(기본 5s) 단위 | [Elastic 공식] Translog settings |
| merge 스레드 수 | 기본은 프로세서 수의 절반, 회전 디스크면 1로 낮춤 | [Elastic 공식] Merge settings |
| index.store.type | 기본 hybridfs | [Elastic 공식] Store |
| 인덱싱용 스토리지 | SSD 권장, RAID 0 stripe, 원격 스토리지 회피 | [Elastic 공식] Tune for indexing speed |
| vSAN 지연 (VM 관점) | All-Flash 5ms / Hybrid 20ms 미만을 정상으로 제시 | [VMware 공식] Broadcom KB 389082 |
| vSAN 지연 (장치 관점) | NVMe 0.5ms 미만, SAS/SATA SSD 1ms 내외, HDD 10~20ms | [VMware 공식] Broadcom KB 424485 |
| Guest와 VMDK 지연 차이 | queue depth 낮은 컨트롤러의 큐 고갈 가능성 | [VMware 공식] Troubleshooting vSAN Performance |
| PVSCSI 큐 | 기본 64(device) / 254(adapter), ring_pages 8에서 32로 | [VMware 공식] KB 2053145 |
| 가상 SCSI 컨트롤러 | 레거시 어댑터는 queue depth 32, PVSCSI는 64 | [VMware 공식] Troubleshooting vSAN Performance |
| NIC | VMXNET3 | [VMware 공식] KB 1001805 |
| vSAN 네트워크 | 패킷 손실 2%면 스토리지 성능 32% 저하 | [VMware 공식] Troubleshooting vSAN Performance |
| I/O scheduler | mq-deadline 또는 none | [Red Hat 공식] Setting the disk scheduler |
| tuned profile | VM은 virtual-guest. throughput-performance 기반이고 dirty_ratio를 올림 | [Red Hat 공식] TuneD profiles |
| 응답시간 3단계 구분 | 주의 / 경고 / 위험 | [실무 기준] KB 389082를 기준으로 단계화. Elastic 공식 수치 없음 |
| 큐 사용률 구간 | 40% / 80% | [실무 기준] 공식 수치 없음 |
| PSI 단계 | 5% / 20% | [실무 기준] 커널 문서에 임계값 제시 없음 |
| THP | madvise 또는 never | [참고] DB 벤더 운영 관행. Elastic 필수 항목 아님 |

vSAN 지연 기준이 둘인 이유가 있습니다. 이 도구가 재는 것은 Guest OS에서 본 지연이고, 여기에는
vSAN 백엔드와 hypervisor, 가상 SCSI를 지나온 시간이 모두 들어 있습니다. 그래서 판정 기준은
VM 관점 수치(KB 389082)를 씁니다. 장치 관점 수치(KB 424485)는 VMware 관리자에게 백엔드 확인을
요청할 때 "어느 정도가 정상인지" 함께 전달하려고 실었습니다.

---

## 상시 모니터링과의 관계

이 도구는 Elastic의 System/Linux integration을 대체하지 않습니다.
장기 추세, 여러 노드 시계열, 경보 자동화는 Elastic 쪽이 맞습니다.
이 도구는 경보가 울린 순간에 한 번 깊게 파는 용도이고, 상시 수집으로는 볼 수 없는 아래를 채웁니다.

- 응답시간 p95 (상시 지표는 평균값 위주라 짧은 급등이 지워집니다)
- 병목 위치 판정 (aqu-sz ÷ queue_depth)
- block device 설정 전수 (readahead, scheduler, timeout, iostats, wbt)
- LVM과 파티션 구성 역추적
- 커널 로그의 SCSI abort/reset, hung task
- VMware balloon, host swap, 예약, limit
- 스토리지 IRQ 편중, mmap 여유
- 공식 문서 기준 전수 대조표

상시 경보 기준은 `GUARDLINE.md` 4장에 정리했습니다.

---

## Guest에서 볼 수 없는 것

리포트에 "VMware 관리자 확인 항목"으로 따로 출력합니다.

vSAN 스토리지 정책(RAID, FTT, stripe, IOPS 제한), ES replica와 vSAN 복제가 겹쳐 생기는 쓰기 증폭,
VM snapshot, ES 노드의 호스트 배치(anti-affinity), vSAN 네트워크와 resync, 캐시 사용률,
물리 디스크 상태, vNUMA.

---

## 검증 현황

| 항목 | 상태 |
|---|---|
| `/proc/diskstats` 계산 정확도 | iostat 12.6과 교차 검증. r_await, w_await, aqu-sz, %util 일치 |
| 커널 로그 패턴 | 정탐·오탐 단위 테스트 통과 |
| LVM, NVMe, 파티션 구성 | 합성 데이터 단위 테스트 통과 |
| `path.data` 표기 5종 파싱 | 통과 |
| HTML과 차트 | 태그 검증 + 가짜 DOM 실행 검증 통과 |
| 호환성 | Python 3.6 문법, bash 4.2, mawk 검사 통과 |
| 실제 vSphere Guest | 미검증 |
| 운영 클러스터 `fs.io_stats` | 미검증 (모의 서버 기준) |

---

## 트러블슈팅

**ES API 접속 실패 (http=401)**
`--es-user`와 `ES_PASSWORD`, 또는 `ES_API_KEY`를 지정하세요. 없으면 OS 레벨만 수집합니다.

**클러스터 조회 실패 (http=403)**
`cluster monitor` 권한이 필요합니다. 권한이 없으면 `--no-cluster`로 실행하면 경고가 나오지 않습니다.

**"성능 판정 보류"로 나옴**
측정한 시간대의 부하가 낮았다는 뜻입니다. 인덱싱이나 검색 피크 시간대에 `-d 600` 이상으로 다시 실행하세요.

**PSI 미지원 표시 (RHEL 8)**
커널에 들어 있지만 기본 비활성입니다. 부트 파라미터에 `psi=1`을 넣고 재부팅하면 포화 판정이 정확해집니다.
없어도 D 상태, 큐, 지연으로 판정합니다.

**리포트가 생성되지 않음**
서버에 Python 3.6 이상이 없는 경우입니다. 번들(`.tar.gz`)을 PC로 옮겨 `es_disk_render.py`를 실행하세요.

---

## 로드맵

- [ ] 실제 vSphere 환경 검증 후 v1.0.0
- [ ] esxtop 출력 대조 가이드
- [ ] baseline 비교 모드. 이전 번들과의 차이 표시
- [ ] 리포트 영문 출력 옵션

---

## 참고사항

- Elastic이나 VMware의 공식 제품이 아닙니다.
- 진단 결과와 조치 안내만 제공하고 설정은 바꾸지 않습니다. 조치는 담당자가 검토하고 적용합니다.
- `es_disk_bench.sh`만 예외로 실제 부하를 겁니다. vSAN은 공유 스토리지라 같은 클러스터의 다른 VM에도 영향이 갈 수 있습니다.

## 라이선스

Apache License 2.0
