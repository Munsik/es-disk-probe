# es-disk-probe

Read-only disk I/O diagnostics for Elasticsearch nodes (VMware vSAN guest, bare-metal, SAN, other hypervisors) at OS level

Elasticsearch 노드의 디스크가 지금 정상인지, 문제라면 원인이 어디에 있는지를 OS에서 판정합니다.
VMware Guest면 VM 안과 밖을, bare-metal이면 부하 포화와 디스크·컨트롤러 이상을, SAN이면 서버와 스토리지 어레이를 가릅니다.
플랫폼은 실행한 서버에서 자동으로 판별하고, 측정 결과를 Elastic·VMware·Red Hat 공식 권장값과 대조해 담당자별 조치 항목까지 HTML 리포트로 냅니다.

v0.10.0 · 비공식 도구 · 읽기 전용 · 한 시점을 보는 진단 도구 (상시 모니터링 도구가 아닙니다)

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
- bare-metal 노드에서 부하가 디스크 능력을 넘은 것인지, 디스크·RAID 컨트롤러가 이상한 것인지 갈라야 할 때
- SAN을 쓰는 노드에서 서버 쪽(HBA 큐, 경로) 문제인지 스토리지 어레이 문제인지 갈라야 할 때
- 인덱싱 지연이나 검색 지연의 원인이 디스크인지 확인해야 할 때
- VMware·스토리지 관리자나 하드웨어 유지보수 쪽에 "느리다"가 아니라 측정 근거를 들고 가야 할 때
- 신규 구축이나 증설 직전에 OS 설정과 서버·VM 구성이 권고에 맞는지 한 번 훑을 때 (성능 판정은 실제 부하가 있을 때 나옵니다)
- 폐쇄망이라 외부 도구를 들이기 어려울 때

이럴 때는 쓰지 마세요.

- 디스크가 원인이라고 이미 확인한 뒤의 튜닝 작업. 이 도구는 원인을 가리는 데까지만 씁니다
- 상시 감시 목적. 위 표를 봐 주세요
- 용량 사이징 근거. 이 도구는 부하를 걸어 최대 성능을 재지 않고, 운영 중인 실제 부하만 봅니다
- 디스크 밖의 성능 문제. CPU, heap, GC, 쿼리 튜닝은 범위가 아닙니다. 다만 "디스크가 원인이 아니다"까지는 판정합니다
- 컨테이너(ECK, Docker) 안에서 실행. 그 노드(호스트)에서 실행하세요. 호스트에서 돌리면 컨테이너 안 ES를 자동으로 찾고,
  ES 프로세스의 mount 정보로 data가 올라간 장치(로컬 PV, Ceph RBD, 클라우드 볼륨 등)까지 따라갑니다

### 서버에서 하는 일과 하지 않는 일

하지 않습니다.

- 시스템 설정 변경. sysctl, /sys, mount 옵션, ES 설정 어느 것도 바꾸지 않습니다
- ES에 쓰기 요청. 조회 API(`GET`)만 씁니다. 인덱스 생성, 설정 변경, 재시작 없습니다
- `drop_caches`, 강제 `sync`, raw device 접근
- 외부 네트워크 통신. 지정한 ES 주소 외에는 아무 곳에도 접속하지 않습니다
- 디스크에 부하를 거는 테스트. 운영 서버에는 이미 실제 부하가 있으므로 그것을 그대로 잽니다
- 파일 삭제. 서버의 어떤 파일도 지우지 않습니다. 자기가 만든 결과 파일도 지우지 않습니다

합니다.

- `/proc`, `/sys` 읽기, ES 조회 API 호출, 결과 디렉터리(기본 `/tmp`, ES data 와 같은 디스크면 다른 디스크로 자동 변경) 한 곳에만 파일 쓰기
- ES 서버 로그와 sar 기록의 끝부분 읽기 (아래 부하 표를 봐 주세요)
- 자기 자신의 우선순위를 낮추는 `renice 19`, `ionice idle`. 프로세스가 끝나면 사라집니다
- NVMe 온도 읽기. `/sys/class/nvme/*/hwmon` 을 읽으면 커널이 장치에 SMART log 를 한 번 요청합니다. 읽기 전용 명령이고 실행당 1회입니다

bare-metal에서는 하드웨어 상태 조회를 자동으로 합니다. VM에서는 가상 장치라 의미가 없어 하지 않습니다.
모두 조회(show) 명령이고, smartd나 모니터링 에이전트(Prometheus storcli exporter 등)가 주기적으로 실행하는 것과 같은 수준입니다.
`--no-hw` 로 끌 수 있습니다.

| 조회 | 명령 | 조건 |
|---|---|---|
| SMART | `smartctl -H -A -i -n standby` (잠든 HDD는 깨우지 않음, 장치당 15초 상한) | smartmontools 설치, RAID 컨트롤러 뒤가 아닌 디스크 |
| Broadcom·Dell RAID | `storcli64` 또는 `perccli64` 의 `/call show all J`, `/call/vall show all J`, `/call/eall/sall show all J`, `/call show patrolread J`, `/call show cc J` | megaraid_sas·mpt3sas 드라이버, 도구 설치 |
| Broadcom MegaRAID 96xx·Dell PERC 12 이후 | `storcli2` 또는 `perccli2` 의 `/call show all J`, `/call/vall show all J`, `/cN/eall/sall show all J`, `/cN/sall show all J`, patrolread·cc | mpi3mr 드라이버, 도구 설치. JSON 키가 공개 문서로 확정되지 않아 해석을 못 하면 원문만 번들에 남기고 알림 |
| HPE RAID | `ssacli ctrl all show config detail` (SR 컨트롤러). HPE MR 컨트롤러는 storcli | hpsa·smartpqi 드라이버, 도구 설치 |
| Microchip·Adaptec RAID | `arcconf getconfig <n> AL` | aacraid·smartpqi 드라이버, 도구 설치 |
| AWS EBS 한도 초과 | `nvme amzn stats` (nvme-cli amzn 플러그인) 또는 `ebsnvme stats -j`, 수집 시작·끝 두 번 | EBS NVMe 볼륨, 도구가 있을 때 (Amazon Linux 는 기본 포함) |

벤더 도구는 실행한 디렉터리에 로그 파일(storcli.log, UcliEvt.log)을 남기는 것이 있어 결과 디렉터리 안의 임시 위치에서 실행하고 지웁니다.
명령마다 30초 상한이 있습니다. 도구가 없으면 건너뛰고, 리포트에 "설치하면 캐시·배터리까지 자동으로 본다"고 안내합니다.

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

샤드가 많으면(이 노드 2,000개 이상 또는 클러스터 20,000개 이상) 인덱스 수에 비례하는 조회는 자동으로 뺍니다.
기준은 실무 기준입니다. 그보다 작아도 빼고 싶으면 `--no-index-stats`, 클러스터 조회를 전부 생략하려면 `--no-cluster`를 쓰세요.
`_snapshot/_status`는 인자 없이 호출하므로 실행 중인 snapshot만 보는 가벼운 형태입니다.
저장소를 읽는 무거운 형태가 아닙니다.

ES 프로세스에 직접 닿는 동작은 하나입니다. `/proc/<pid>/maps`를 실행할 때마다 한 번 읽습니다.
매핑이 4만 개인 프로세스에서 약 17ms 걸렸고, 그동안 그 프로세스의 `mmap_lock`을 read 모드로 잡습니다.
ES가 segment를 열거나 닫을 때(`mmap`, `munmap`) 잠깐 경합하는 정도입니다. 이것도 피하려면 `--light`를 쓰세요.
나머지 `/proc/<pid>/io`, `/proc/<pid>/task/*/stat` 읽기는 lock을 잡지 않습니다.

결과 저장 위치는 ES data와 다른 디스크여야 합니다. 같은 파일시스템이면 측정 대상 디스크에 쓰기를 더하게 되고
그만큼 측정값이 오염됩니다. `-o`를 안 주면 도구가 ES data와 다른 디스크를 골라 씁니다.
`-o`로 직접 준 위치가 같은 디스크면 경고하고 리포트에도 표시합니다.

실행할 때마다 그 실행의 실측 부하가 리포트의 "이 진단이 서버에 준 부하" 섹션에 찍힙니다.
리포트를 받은 사람이 직접 확인할 수 있게 넣었습니다.

처음 쓰실 때는 사내 노드나 개발 노드에서 한 번 돌려 보고 출력을 확인하시면 좋습니다.


| 원칙 | 구현 |
|---|---|
| 플랫폼 자동 판별 | VMware, bare-metal(로컬·SAN), 그 밖의 hypervisor·클라우드를 구분해 기준값과 담당자를 바꿈. [플랫폼별 판정](#플랫폼별-판정) |
| 시스템을 바꾸지 않음 | `/proc`, `/sys` 읽기와 ES 조회 API(GET)만. 쓰기는 결과 디렉터리 안에만 |
| 서비스에 영향 없음 | 실측 CPU 0.97초(300초 측정 시 CPU 1개의 0.32%), 메모리 4.2MB, 디스크 읽기 12MB, `nice 19` + `ionice idle`. [부하 실측 상세](#이-도구가-서버에-주는-부하) |
| 부하를 걸지 않음 | 부하 테스트 도구가 없음. 운영 중 실제 부하를 측정. 부하가 낮으면 성능 판정을 스스로 보류 |
| 지우지 않음 | 어떤 스크립트에도 삭제 동작(`rm` 등)이 없음. 벤더 도구가 남기는 로그도 결과 디렉터리 안에 두고 번들에 포함 |
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

# 데이터 노드에서 피크 시간대에 실행 (기본 300초, 5초 간격). 옵션 없이 실행하면 됩니다
sudo ./es_disk_collect.sh
```

ES가 인증을 요구하면 사용자와 비밀번호를 물어봅니다. 조회 전용 권한(`monitor`)이면 충분합니다.
끝나면 셸(awk)이 만든 요약 판정이 바로 화면에 나옵니다. Python 3가 없는 서버(RHEL 7 등)에서도 같습니다.

```
=== es-disk-probe 요약 판정 (셸) ===
호스트 es-warm-01 · bare-metal · 대상 디스크 sdb
판정 기준 SSD (추정) · 응답시간 주의 3 / 경고 6 / 위험 15 ms

판정: 디스크 성능 저하 징후가 있습니다

응답시간 p95  읽기 1.20 ms · 쓰기 2.00 ms  → 정상
부하 p95      IOPS 2700.0 · 49.2 MB/s · aqu-sz 3.0 / queue_depth 256 · %util 70.0
flush         초당 40.0회 · 평균 9.00 ms
포화          PSI io full p95 0.4% · ES D 상태 스레드 p95 0

확인할 항목 (심각한 순)
  [경고] flush(장치 캐시 비우기) 평균 9.00 ms, 초당 40.0회
         → 전원 차단 보호가 있는 SSD, 배터리 보호 RAID 캐시인지 확인
  [경고] 커널 로그: RAID 컨트롤러 이벤트 1건 (최근 7일)
         → 번들의 static/klog_io 원문 시각을 담당자에게 전달
  [경고] LVM thin pool vg-pool-tpool 데이터 92% 사용
         → pool 확장(lvextend) 또는 정리. 가득 차면 쓰기 중단
  [주의] ES 가 아닌 프로세스의 디스크 I/O 비중 62% (상위: backup-agent(pid 777) 501MB)
         → 그 프로세스가 ES data 디스크를 쓰는지 확인
```

같은 내용이 번들 안 `summary.txt` 로 남습니다. 서버에 Python 3.6+ 가 있으면 HTML 리포트도 같이 만듭니다.
HTML 리포트에는 병목 위치 판정, 클러스터 비교, 인덱스별 분포, 항목마다 근거와 출처가 더 들어 있습니다.

### 알아서 판단하는 것

사용자가 정할 필요가 없도록 아래는 도구가 자동으로 정합니다. 자동 판단이 틀렸을 때만 옵션으로 바꾸세요.

| 항목 | 자동으로 하는 일 | 바꾸는 옵션 |
|---|---|---|
| 플랫폼 | VMware, bare-metal(로컬·SAN), 그 밖의 VM·클라우드 판별 | `--platform` |
| 판정 기준 매체 | NVMe·SSD·HDD, RAID 뒤 매체(컨트롤러 도구), SAN, Ceph RBD, 클라우드 볼륨 판별. VMware 데이터스토어 종류만은 Guest에서 알 수 없어 공통 기준이 기본 | `-s` |
| 하드웨어 상태 | bare-metal이면 SMART와 RAID 컨트롤러(storcli·perccli, ssacli, arcconf)를 조회. 도구가 있는 것만 | `--no-hw` |
| 컨테이너 안 ES | 호스트에서 실행하면 컨테이너 안 ES를 찾아 mount 정보로 data 장치까지 따라감 | `-p` |
| ES 주소 | ES 프로세스가 실제로 열어 둔 포트를 찾아 http, https 순서로 접속. `network.host` 를 IP로 묶은 경우도 찾음 | `--es-url` |
| ES 인증 | 401이면 터미널에서 사용자·비밀번호를 물어봄. 비밀번호가 셸 history에 남지 않음 | `--es-user` + `ES_PASSWORD`, `ES_API_KEY` |
| ES data 경로 | ES 프로세스, `elasticsearch.yml` 의 `path.data` 에서 찾음 (ES가 내려가 있어도) | `-p` |
| 결과 저장 위치 | `/tmp` 가 ES data와 같은 디스크면 `/var/tmp`, `/root` 등 다른 디스크로 자동 변경 | `-o` |
| 인덱스별 조회 | 이 노드 샤드 2,000개 이상이거나 클러스터 샤드 20,000개 이상이면 생략 | `--no-index-stats` |
| 클러스터 조회 | 권한이 없으면(403) 이 노드 결과만으로 리포트 | `--no-cluster` |
| 리포트 생성 | 서버에 Python 3.6 이상이 있으면 바로 HTML 생성 (RHEL 8 platform-python 포함) | `--no-render` |

서버에 `python3`가 없으면 번들만 옮겨 PC에서 만듭니다.

```bash
python3 es_disk_render.py esdisk_es-hot-01_20260923_142031.tar.gz
```

---

## 요구사항

| 구분 | 내용 |
|---|---|
| OS | RHEL / CentOS / Rocky 7·8·9, Ubuntu 20.04+ |
| 수집기·요약 판정 | bash 4.2+, awk(gawk·mawk), coreutils, util-linux, procps. `curl`은 ES 조회할 때만. 모두 OS 기본 설치 |
| HTML 분석기 | Python 3.6+ 표준 라이브러리만 (RHEL 8의 `/usr/libexec/platform-python` 자동 인식). 서버에 없으면 번들을 PC로 옮겨 실행 |
| 권한 | root 권장 (ES 프로세스 I/O, 커널 로그, VMware 정보) |
| ES 권한 | `cluster monitor` (`monitoring_user` 수준). 관리자 계정 필요 없음 |
| 선택 (있으면 더 봄) | `sysstat`(과거 이력), `ethtool`(NIC ring), `open-vm-tools`(VMware 자원), `smartmontools`(SMART), RAID 컨트롤러 도구(`storcli`·`perccli`, `ssacli`, `arcconf`) |

### 서버에서 쓰는 명령

선택 도구가 하나도 없어도 모든 판정의 기본은 동작합니다. 선택 도구는 같은 항목을 더 자세히 볼 때만 씁니다.

| 명령 | 패키지 | 기본 설치 | 용도 |
|---|---|---|---|
| bash, awk, grep, sed, sort, find, stat, df, tar, timeout, date | bash, gawk/mawk, coreutils, findutils, tar | 예 | 수집, 요약 판정 |
| lsblk, lscpu, ionice, dmesg | util-linux | 예 | 장치 구성, 우선순위 낮추기, 커널 로그 |
| pgrep, sysctl, renice | procps | 예 | ES 프로세스, 커널 설정 |
| journalctl, systemd-detect-virt | systemd | 예 | 커널 로그, 플랫폼 판별 |
| dmsetup | device-mapper (lvm2) | 예 (RHEL, Ubuntu 서버) | LVM·multipath·thin pool 구성과 상태 |
| curl | curl | 예 | ES 조회 API |
| python3 | python3 / platform-python | RHEL 8·9, Ubuntu 예. RHEL 7 아니오 | HTML 리포트 (없으면 셸 요약만) |

`es_disk_collect.sh`와 `es_cluster_probe.sh`는 기본적으로 `curl -k`로 동작합니다.
자체 서명 인증서 환경을 감안한 기본값입니다. 수집기는 localhost만 보므로 그대로 두어도 무리가 없습니다.
원격으로 조회하는 `es_cluster_probe.sh`에서 인증서 검증이 필요하면 `--strict-tls`나 `--cacert <파일>`을 쓰세요.

---

## 옵션

```
-d SEC        측정 시간 (기본 300)
-i SEC        샘플 간격 (기본 5)
-p PATH       ES data 경로 (여러 번 지정 가능, 안 주면 자동 탐지)
-o DIR        결과 저장 위치 (기본 /tmp. ES data 와 같은 디스크면 다른 곳으로 자동 변경)
-s TYPE       스토리지 유형 (기본 auto)
              VMware: allflash | hybrid (vSAN), vmfs (SAN·NFS 데이터스토어)
              bare-metal·SAN: nvme | ssd | hdd  (RAID 컨트롤러 도구가 없어 매체를 못 읽을 때 지정)
--platform P  플랫폼 강제 지정: auto | vmware | baremetal | vm (기본 auto)
--no-hw       하드웨어 상태 조회를 끔 (bare-metal 에서 자동으로 하는 SMART, RAID 컨트롤러 조회)
--smart       VM 에서도 SMART 를 읽음 (보통 필요 없음)
--es-url URL  ES 주소 (기본 자동 탐지)
--es-user U   ES 사용자 (비밀번호는 환경변수 ES_PASSWORD, API Key는 ES_API_KEY)
--no-es       ES API 조회 생략
--no-cluster  클러스터 전체 조회 생략 (이 노드만)
--no-render   HTML 생성 생략 (번들만)

부하를 줄이는 옵션 (위 "이 도구가 서버에 주는 부하" 참조)
--light           디스크를 읽는 부가 수집 전부 생략. 디스크 읽기 0
--no-eslog        ES 서버 로그 읽기만 생략. 읽기량의 대부분
--no-index-stats  인덱스 수에 비례하는 ES 조회 생략 (샤드가 많으면 자동으로 생략)
```

환경변수로 세부 조정할 수 있습니다.
`ESLOG_TAIL_MB`(최신 로그 읽기 상한, 기본 8), `ESLOG_OLD_MB`(직전 로그, 기본 2),
`ESLOG_MAX_FILES`(대상 파일 수, 기본 3), `KLOG_MAX_LINES`(커널 로그 줄 상한, 기본 20000).

클러스터 조회에 실패해도 경고만 남기고 로컬 결과로 리포트를 만듭니다.

`-s` 와 `--platform` 은 분석기(`es_disk_render.py --storage ... --platform ...`)에도 있습니다.
번들을 다시 수집하지 않고 기준만 바꿔 리포트를 다시 만들 수 있습니다.

---

## 구성

| 파일 | 역할 | 서버 영향 |
|---|---|---|
| `es_disk_collect.sh` | 수집 전부. 로컬 + 클러스터 + 인덱스별 분포 | 읽기만. 설정 변경 없음 |
| `es_disk_summary.sh` | 셸(awk)만으로 핵심 판정 요약. 수집기가 끝날 때 자동 실행, `summary.txt` | 번들만 읽음 |
| `es_disk_render.py` | 전체 분석, 판정, HTML 생성 (서버 또는 PC) | 서버에서 안 돌려도 됨 |
| `es_cluster_probe.sh` | (선택) 노드 접속 없이 클러스터만 원격 조회 | 조회 API GET만 |
| `GUARDLINE.md` | 설계, 구성, 상시 감시 기준과 변경 원칙 | 문서 |
| `tests/` | 합성 번들 생성기와 판정 테스트 (플랫폼·매체·RAID 도구별 32개 시나리오) | 서버에서 안 돌림 |

---

## 자동으로 점검하는 항목 전체

"어떤 환경에서든 디스크 부하가 어느 수준인지, 무엇이 문제이고, 누가 어떻게 조치해야 하는지"를 한 번 실행으로 판정하려고
아래 항목을 봅니다. 출처 열에서 굵게 표시하지 않은 것은 모두 OS 기본 도구와 `/proc`·`/sys` 입니다.

| 영역 | 점검 항목 | 출처 |
|---|---|---|
| 부하 수준 | 응답시간 p95(읽기·쓰기), IOPS, 처리량, 요청 크기, 병합 비율, 대기 I/O(aqu-sz), inflight, %util | /proc/diskstats |
| 포화 | PSI io some/full, ES 스레드 D 상태, iowait, 큐 사용률(aqu-sz ÷ queue_depth), 한도에 걸린 모양(IOPS·처리량 평평 + 대기 증가) | /proc/pressure, /proc/&lt;pid&gt;/task, /sys/block |
| fsync 비용 | flush 요청 수와 평균 시간 (커널 5.5+), ES flush 평균 시간 | /proc/diskstats, ES node stats |
| 원인 위치 | 병목 위치(플랫폼별), 쓰기만 느림, 묶음 안 한 장치만 느림, 여러 노드 동시 고부하 | 위 지표 조합, _nodes/stats |
| 옆집 부하 | ES 가 아닌 프로세스의 디스크 I/O 상위 목록 | /proc/&lt;pid&gt;/io |
| 오류 | 커널 로그(I/O error, abort/reset, timeout, hung task, FS 오류, 컨트롤러·PCIe, RAID 컨트롤러 이벤트, multipath, md, thin pool), 장치 상태·타임아웃·오류 카운터, PCIe AER, NVMe 컨트롤러 상태 | journalctl/dmesg, /sys |
| 하드웨어 | RAID 레벨(hpsa·smartpqi), raid_class 볼륨 상태, 컨트롤러 펌웨어 크래시, md degraded·resync·mismatch, NVMe 온도·PCIe 링크, CPU governor, SMART, 컨트롤러 캐시·배터리·구성 디스크 | /sys, /proc/mdstat, **smartctl**, **storcli·perccli·ssacli·arcconf** |
| 블록 장치 설정 | readahead, scheduler, iostats, wbt, SCSI timeout, queue_depth, 파티션 정렬, 쓰기 캐시 보고값 | /sys/block |
| 저장 구조 | LVM linear·stripe, thin pool 사용률, LVM snapshot, dm-crypt, dm-cache, multipath, OS·swap·snapshot 저장소·로그와 같은 디스크인지 | dmsetup table/status, /proc/swaps, ES 설정 |
| 파일시스템 | 종류(NFS 등), atime, discard, barrier, sync, data=journal, 사용률·watermark, inode | /proc/mounts, df |
| 메모리 | swap 사용·설정, heap 비중, page cache 여유, major fault, dirty page, VMware balloon·host swap | /proc/meminfo, /proc/vmstat, **vmware-toolbox-cmd** |
| ES | 인덱싱 스로틀, write·search 거절, indexing pressure, flush·refresh·merge 시간, translog durability, merge 스레드, store type, 파일 핸들, mmap 여유, ES 로그의 스로틀·watermark·flush 실패 | ES 조회 API, /proc/&lt;pid&gt;, ES 로그 |
| 클러스터 | 노드 간 쏠림(같은 tier), watermark 근접, 복구·이동·snapshot 진행, awareness, 샤드 쏠림, 인덱스별 쓰기 집중, ILM phase | ES 조회 API |
| 플랫폼 | VMware(컨트롤러, NIC, 예약·limit), 그 밖의 VM(steal), 클라우드 볼륨, Ceph RBD, 컨테이너 안 ES | systemd-detect-virt, DMI, mountinfo |
| 과거 | 최근 7일 응답시간 이력 | **sar** (sysstat 이 이미 기록한 것) |

## 한 번 실행으로 얻는 것

| 관점 | 내용 |
|---|---|
| 로컬 (커널) | 응답시간 p95, aqu-sz, PSI, D 상태, 설정 전수, 커널 로그, VMware 자원 또는 하드웨어 상태 |
| 클러스터 | 노드별 디스크 사용량 비교, 샤드·용량 쏠림, recovery·snapshot 같은 클러스터발 부하 |
| 인덱스 | 이 노드 샤드의 인덱스별 쓰기, merge, 검색 분포, ILM phase |

클러스터 스냅샷은 로컬 측정과 같은 시간대로 찍습니다.

교차 판정으로 아래를 가려냅니다.

- 디스크가 느린 게 아니라 이 노드에 샤드가 몰린 경우
- 한 노드가 아니라 여러 노드가 동시에 느려진 경우 (공용 스토리지 의심)
- 쓰기가 특정 인덱스 하나에 집중된 경우

---

## 리포트 구성

맨 위에 한 문장 판정과 9개 항목 요약(지연, 포화, 오류, ES 영향, 메모리·캐시, 설정, 플랫폼 자원,
네트워크, 클러스터)이 오고, 이어서 담당자를 표시한 "먼저 할 일" 최대 3개가 나옵니다.
그다음은 아래 순서입니다.

1. 핵심 수치, 시간대별 흐름. 응답시간, IOPS, aqu-sz, PSI 차트 (외부 CDN 없음)
2. 판정 근거와 조치 안내. 담당자별 묶음, 근거·이유·조치·출처
3. Best practice 대조표. 통과한 항목까지 전부 (약 30개)
4. Elasticsearch 쪽 영향. 측정 구간의 지표 변화량
5. 한계 추정. 부하 테스트 없이 계산한 큐 기준 이론 상한과 현재 사용률
6. 클러스터 관점, 이 노드 디스크를 쓰는 인덱스
7. 디바이스별 상세, 과거 7일 이력(sar)
8. 이 진단이 서버에 준 부하. 그 실행의 실측값
9. 측정 범위와 한계. 상시 수집 지표와의 차이, Guest에서 못 보는 것
10. 부록. 커널·메모리 설정 원본, 커널 로그 발췌

클러스터 조회나 sar 기록이 없으면 해당 섹션은 빠집니다.

샘플 리포트: `docs/sample_node_report.html`(VMware Guest), `docs/sample_baremetal_report.html`(bare-metal, HDD RAID5 에서 CacheVault 이상으로 쓰기 캐시가 write-through 로 떨어진 사례),
`docs/sample_cluster_report.html`(클러스터 원격 조회). 모두 합성 데이터입니다.

기준값을 최신 문서와 대조한 기록과 남은 업데이트 항목은 `docs/UPDATE_NOTES.md` 에 있습니다.

---

## 판정 읽는 법

| 판정 | 의미 |
|---|---|
| 디스크는 정상입니다 | 응답시간, 포화, 오류가 모두 기준 안이고 불리한 설정도 없음 |
| 지금은 버티지만 위험 요인이 있습니다 | 측정값은 괜찮으나 부하가 늘거나 호스트가 경합하면 문제 될 설정이 있음 |
| ES에 처리 지연 신호가 있지만 디스크 응답은 정상 | 원인이 디스크 밖(CPU, heap, bulk, 샤드)일 가능성 |
| 성능 저하 징후 | 측정 중에 디스크 지연, 포화, 오류를 관측 |
| 성능 판정 보류 | 측정한 시간대의 부하가 낮아 판단 근거가 부족. 인덱싱·검색 피크 때 다시 측정 |

병목 위치는 응답시간이 기준을 넘었을 때만 판정합니다.
큐 사용률(aqu-sz ÷ queue_depth 합계)로 세 구간으로 나눕니다. 구간은 같고, 각 구간의 뜻과 담당자는 플랫폼마다 다릅니다.

| 큐 사용률 | VMware Guest | bare-metal 로컬 | bare-metal SAN |
|---|---|---|---|
| 80% 이상 | Guest 쪽 큐가 가득 참. 서버 담당자: VMDK 분할 + 별도 PVSCSI 컨트롤러, queue depth 상향 | 디스크 구성이 동시 처리 한계. 서버 담당자: 디스크 추가 후 stripe, 더 빠른 매체 | 서버 LUN 큐가 가득 참. 서버 담당자: LUN 분할 후 stripe, 경로 확인 |
| 40 ~ 80% | 단정하지 않음. esxtop DAVG와 KAVG 분리 확인 | 단정하지 않음. 커널 로그·SMART·컨트롤러 이벤트 확인 | 단정하지 않음. 어레이 쪽 응답시간과 비교 |
| 40% 미만 | VM 바깥. VMware 관리자: vSAN, 호스트 경합, resync | 디스크·컨트롤러 자체가 느림. 하드웨어 담당자: 불량 디스크, RAID 재구성, 캐시·배터리 | 어레이·SAN 경로. 스토리지 관리자: 어레이 부하, 포트 오류 |
| queue_depth 없음 | root 권한으로 다시 측정 | NVMe는 큐 기준 대신 온도·링크·SMART로 안내 | root 권한으로 다시 측정 |

그 밖의 VM(KVM, Hyper-V, 클라우드)은 VMware와 같은 구도로 판정하되 백엔드를 특정하지 않습니다.
virtio-blk처럼 queue_depth가 없는 가상 디스크는 권한 문제가 아니라는 점을 구분해 안내합니다.

`aqu-sz`는 block layer에서 대기하는 요청까지 포함한 시간 평균이라 queue_depth를 넘을 수 있습니다.
그래서 장치에 실제로 넘어간 I/O 수(`inflight`)를 함께 표시하고, 둘 중 하나라도 80%를 넘으면 큐 포화로 봅니다.
구간 경계인 40%와 80%는 실무 기준이고 공식 수치가 아닙니다.

---

## 기준값 출처

리포트의 모든 항목에 출처 등급을 표시합니다.

| 항목 | 기준 | 출처 |
|---|---|---|
| readahead | 128KiB (LVM·RAID는 수 MiB로 커질 수 있음) | [Elastic 공식] Tune for search speed |
| vm.max_map_count | 최소 262144, 권장 1048576 (8.16부터) | [Elastic 공식] Bootstrap checks |
| swap | 비활성 > memory_lock > swappiness=1 | [Elastic 공식] Disable swapping |
| JVM heap | 자동 설정 권장. 직접 정하면 RAM 50% 이하, compressed oops 한도 이하(대부분 26GB, 일부 30GB). 노드가 알려 주는 compressed oops 사용 여부로 판정 | [Elastic 공식] JVM settings |
| 파일 핸들 | 65535 이상 | [Elastic 공식] File descriptors |
| disk watermark | 기본 85/90/95%. 비율을 직접 지정하지 않았으면 max_headroom(200/150/100GB)도 적용해 실제 경계를 계산 (8.5+) | [Elastic 공식] Cluster-level shard allocation and routing settings |
| 스토리지 종류 | 직결 로컬 스토리지가 일반적으로 더 빠름 [Elastic 공식] Tune for indexing speed. 네트워크 파일시스템(NFS·SMB) 위 data 경로는 위험으로 판정 | [실무 기준] |
| translog durability | 기본 request는 요청마다 fsync, async는 sync_interval(기본 5s) 단위 | [Elastic 공식] Translog settings |
| merge 스레드 수 | 기본은 프로세서 수의 절반(9.3 이하·8.x 는 최대 4), 회전 디스크면 1로 낮춤 | [Elastic 공식] Merge settings |
| index.store.type | 기본 hybridfs | [Elastic 공식] Store |
| 인덱싱용 스토리지 | SSD 권장, RAID 0 stripe, 원격 스토리지 회피 | [Elastic 공식] Tune for indexing speed |
| vSAN 지연 | vSAN 성능 화면 기준 flash 5ms / hybrid 20ms 미만을 정상으로 제시 (vSAN 7·8. ESA 별도 수치 없음) | [VMware 공식] Broadcom KB 389082 |
| vSAN 지연 (장치 관점) | NVMe 0.5ms 미만, SSD 1ms 이하, HDD 10~20ms | [VMware 공식] Broadcom KB 424485 |
| bare-metal·SAN 지연 (주의 선) | NVMe 1ms, 엔터프라이즈 SSD 3ms, HDD 25ms 초과 | [VMware 공식] Broadcom KB 424485 의 장치별 경보 기준. HDD 30ms 초과는 KB가 critical로 제시 |
| bare-metal·SAN 지연 (경고·위험) | NVMe 3/10ms, SSD 6/15ms, HDD 30/50ms | [실무 기준] |
| 그 밖의 VM 지연 | 5 / 10 / 20ms | [실무 기준] KB 389082 flash 수치를 공통 기준으로 차용 |
| I/O scheduler (bare-metal) | 고성능 SSD·NVMe none/kyber, 기존 HDD mq-deadline/bfq | [Red Hat 공식] Disk schedulers for different use cases |
| tuned profile (bare-metal) | throughput-performance. 설치 시 컴퓨트 노드에 자동 선택, 절전 기능을 끔 | [Red Hat 공식] TuneD profiles |
| merge 스레드 (bare-metal HDD) | 회전 디스크면 `max_thread_count` 1 | [Elastic 공식] Merge settings |
| 로컬 대 원격 스토리지 | 직결 로컬 스토리지가 일반적으로 더 빠르고, 일부 원격 스토리지는 ES 부하에서 매우 느림 | [Elastic 공식] Tune for indexing/search speed |
| NVMe 온도 | hwmon temp1_max(현재 과열 임계값, 기본 WCTEMP) 도달 시 경고 | Linux nvme hwmon, NVMe 규격 |
| Guest와 VMDK 지연 차이 | queue depth 낮은 컨트롤러의 큐 고갈 가능성 | [VMware 공식] Troubleshooting vSAN Performance |
| PVSCSI 큐 | 기본 64(device) / 254(adapter), ring_pages 8에서 32로 | [VMware 공식] Broadcom KB 343323 (구 2053145) |
| 가상 SCSI 컨트롤러 | 레거시 어댑터는 queue depth 32, PVSCSI는 64 | [VMware 공식] Troubleshooting vSAN Performance |
| NIC | VMXNET3 | [VMware 공식] Broadcom KB 321259 (구 1001805) |
| vSAN 네트워크 | 패킷 손실 2%면 스토리지 성능 32% 저하 | [VMware 공식] Troubleshooting vSAN Performance (VCF 9.1판) |
| esxtop DAVG·KAVG·GAVG | 10ms 넘는 상태가 이어지면 문제 | [VMware 공식] Broadcom KB 344099 |
| AWS EBS 한도 초과 | Nitro 가 보고하는 볼륨·인스턴스 한도 초과 누적 시간. 측정 시간의 1% 이상이면 주의, 10% 이상이면 경고 | [AWS 공식] EBS detailed performance statistics. 구간은 [실무 기준] |
| I/O scheduler | mq-deadline 또는 none | [Red Hat 공식] Setting the disk scheduler |
| tuned profile | VM은 virtual-guest. throughput-performance 기반 (swappiness 30·dirty_ratio 30, throughput-performance 는 10·40) | [Red Hat 공식] TuneD profiles (RHEL 10) |
| 응답시간 3단계 구분 | 주의 / 경고 / 위험 | [실무 기준] KB 389082(vSAN), KB 424485(장치)를 기준으로 단계화. Elastic 공식 수치 없음 |
| 큐 사용률 구간 | 40% / 80% | [실무 기준] 공식 수치 없음 |
| PSI 단계 | 5% / 20% | [실무 기준] 커널 문서에 임계값 제시 없음 |
| THP | madvise 또는 never | [참고] DB 벤더 운영 관행. Elastic 필수 항목 아님 |

지연 기준이 둘인 이유가 있습니다. 이 도구가 재는 것은 OS에서 본 지연입니다.
VMware Guest에서는 여기에 vSAN 백엔드와 hypervisor, 가상 SCSI를 지나온 시간이 모두 들어 있어 VM 관점 수치(KB 389082)를 씁니다.
bare-metal에는 그 계층이 없어 장치 관점 수치(KB 424485)가 곧 기대치가 됩니다.
다만 OS에서 본 await 에는 block layer에서 기다린 시간도 들어 있어 부하가 몰리면 장치 자체 지연보다 크게 나옵니다.
그래서 장치의 "정상 범위"가 아니라 KB가 제시한 "경보 기준"을 주의 선으로 두었습니다.

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

## OS에서 볼 수 없는 것

리포트의 "원리상 볼 수 없는 것" 표에 플랫폼별로 따로 출력합니다.

| 플랫폼 | 항목 |
|---|---|
| VMware (vSAN) | vSAN 스토리지 정책(RAID, FTT, stripe, IOPS 제한), ES replica와 vSAN 복제가 겹쳐 생기는 쓰기 증폭, VM snapshot, ES 노드의 호스트 배치(anti-affinity), vSAN 네트워크와 resync, 캐시 사용률, 물리 디스크 상태, vNUMA |
| VMware (SAN·NFS 데이터스토어) | 데이터스토어 종류와 뒤의 어레이, 어레이 쪽 응답시간, Storage I/O Control·디스크 IOPS 한도, VM snapshot, 호스트·데이터스토어 배치, ESXi 경로 정책 |
| bare-metal 로컬 | BIOS 전원 정책. RAID 컨트롤러 도구가 없으면 캐시 정책·배터리, RAID 레벨·재구성 일정, 구성 디스크 상태도 여기에 들어감 |
| bare-metal SAN | 어레이 쪽 응답시간과 컨트롤러 부하, 같은 어레이의 다른 서버, 볼륨 QoS 한도, SAN 스위치 포트 오류, 어레이 복제·스냅샷 일정 |
| 그 밖의 VM·클라우드 | 호스트 스토리지 백엔드와 캐시, 볼륨 IOPS·처리량 한도(AWS EBS 는 nvme-cli 가 있으면 한도 초과 시간을 직접 읽음), 같은 호스트의 다른 VM, ES 노드 배치 |

RAID 컨트롤러 캐시는 커널의 `queue/write_cache` 값으로 판단하지 않습니다.
배터리로 보호되는 캐시를 "write through"로 보고하는 컨트롤러가 있어서, 그 값만으로는 캐시가 켜졌는지 알 수 없습니다.
컨트롤러 도구의 현재 캐시 정책(설정값과 실제 적용값)으로 판정합니다.

---

## 테스트

실제 서버 없이 플랫폼·매체·부하 조합별 판정을 확인할 수 있습니다. Python 표준 라이브러리만 씁니다.

```bash
python3 tests/run_tests.py                 # 32개 시나리오 기대 판정 + 셸·HTML 판정 일치 검사
./es_disk_summary.sh <번들 디렉터리>         # 셸 요약 판정만 따로
python3 tests/run_tests.py --dump /tmp/t   # 시나리오별 HTML 리포트와 판정 목록(JSON) 저장
python3 tests/make_bundle.py --list        # 시나리오 목록
```

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
| 셸 요약 판정 | 합성 번들 32종 모두 HTML 판정과 결론 일치, 매체 추정 여부도 일치. `tests/run_tests.py` 가 매번 비교 (mawk·gawk 모두 확인) |
| 플랫폼별 판정 | 합성 번들 32종 통과: VMware 6(vSAN All-Flash·Hybrid·기본·VMFS), bare-metal NVMe 2, HDD RAID, md SSD stripe, FC SAN, Ceph RBD, RAID 도구 8, KVM, AWS EBS 4, ECK, 컨테이너, RHEL 서비스(PrivateTmp), OS 기본 점검, 저부하 보류, ES merge·벡터 direct IO, 인덱스 설정 |
| RAID 도구 출력 해석 | storcli JSON 키는 Prometheus storcli exporter 가 쓰는 키, ssacli·arcconf 는 공개된 출력 레이블로 만든 합성 출력으로 검증. storcli2·perccli2 는 키 이름·값 표기를 바꾼 변형 3종(공백·snake_case·개수 필드 동반)과 해석 불가 1종으로 검증. 실제 장비 출력은 미확인 |
| AWS EBS 통계 해석 | JSON(한 줄·여러 줄), ebsnvme 텍스트, "이름 : 값" 표, 단위(us) 붙은 텍스트 4종으로 검증. 실제 nvme-cli 출력은 미확인 |
| 0.9.x → 0.10 회귀 | VMware 합성 번들 3종의 판정 목록이 0.9.5와 동일 |
| 실제 Linux VM + 실제 ES | UTM(QEMU, Apple Silicon) Rocky Linux 9.8 aarch64 + Elasticsearch 8.19.21. 무부하와 bulk 인덱싱 부하(약 12만 docs/s) 두 번 측정해 셸 요약·HTML 판정 일치 확인. 여기서 찾은 수집 문제 6건 수정 (CHANGELOG) |
| 실제 vSphere Guest | 미검증 |
| 실제 bare-metal (NVMe, RAID, SAN), 클라우드, Kubernetes | 미검증 |
| 운영 클러스터 `fs.io_stats` | 단일 노드 실제 ES 8.19 에서 확인. 다중 노드 운영 클러스터는 미검증 |

---

## 트러블슈팅

**ES API 접속 실패 (http=401)**
`--es-user`와 `ES_PASSWORD`, 또는 `ES_API_KEY`를 지정하세요. 없으면 OS 레벨만 수집합니다.

**클러스터 조회 실패 (http=403)**
`cluster monitor` 권한이 필요합니다. 권한이 없으면 `--no-cluster`로 실행하면 경고가 나오지 않습니다.

**"성능 판정 보류"로 나옴**
측정한 시간대의 부하가 낮았다는 뜻입니다. 인덱싱이나 검색 피크 시간대에 `-d 600` 이상으로 다시 실행하세요.

**PSI 미지원 표시 (RHEL 8)**
RHEL 8·9·10 커널에 들어 있지만 기본 비활성입니다. 부트 파라미터에 `psi=1`을 넣고 재부팅하면 포화 판정이 정확해집니다.
없어도 D 상태, 큐, 지연으로 판정합니다.

**플랫폼이 다르게 판별됨, 또는 "플랫폼 미확정"**
리포트 부록의 "플랫폼 판정 근거"를 확인하세요. `systemd-detect-virt` 가 없는 오래된 배포판이나 DMI를 못 읽는 환경에서 생깁니다.
`--platform baremetal|vmware|vm` 으로 지정하면 됩니다. 번들만 있으면 `python3 es_disk_render.py <번들> --platform baremetal` 로 다시 만들 수 있습니다.

**SSD RAID인데 HDD 기준으로 판정됨**
RAID 컨트롤러가 논리 디스크의 `rotational` 을 1로 보고하는 경우입니다. 리포트에 "추정"으로 표시됩니다.
`-s ssd` 로 다시 수집하거나, 번들에 `es_disk_render.py --storage ssd` 로 다시 분석하세요.

**리포트가 생성되지 않음**
서버에 Python 3.6 이상이 없는 경우입니다. 번들(`.tar.gz`)을 PC로 옮겨 `es_disk_render.py`를 실행하세요.

---

## 로드맵

- [ ] 실제 vSphere 환경과 bare-metal(NVMe, 하드웨어 RAID, FC SAN) 검증 후 v1.0.0
- [x] AWS EBS 한도 초과 시간 판정 (nvme amzn stats). Azure·GCP 는 VM 안에서 볼 지표가 없어 모양 판정만
- [x] RAID 컨트롤러 벤더 도구(storcli·perccli·storcli2·perccli2, ssacli, arcconf) 캐시·배터리·구성 디스크 판정
- [ ] esxtop 출력 대조 가이드
- [ ] baseline 비교 모드. 이전 번들과의 차이 표시
- [ ] 리포트 영문 출력 옵션

---

## 참고사항

- Elastic이나 VMware의 공식 제품이 아닙니다.
- 진단 결과와 조치 안내만 제공하고 설정은 바꾸지 않습니다. 조치는 담당자가 검토하고 적용합니다.
- 부하를 걸거나 파일을 지우는 동작은 없습니다. 결과 디렉터리에 파일을 쓰는 것이 이 도구가 서버에 남기는 유일한 흔적입니다.

## 라이선스

MIT License
