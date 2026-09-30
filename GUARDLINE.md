# Elasticsearch 노드 디스크 Guardline (VMware · bare-metal · SAN)

이 문서는 디스크 때문에 Elasticsearch가 불안정해지는 일을 **미리 막기 위한 기준**입니다.
진단 도구(`es_disk_collect.sh`)이 점검하는 항목과 기준값이 같습니다. 리포트의 "Best practice 대조표"와 나란히 보면 됩니다.

기준마다 출처 종류를 붙였습니다.

- **[Elastic]** Elastic 공식 문서의 권고
- **[VMware]** VMware/Broadcom 문서의 권고
- **[OS]** 커널·배포판 문서
- **[실무]** 공식 수치가 없어 운영 경험으로 정한 기준. 환경에 맞게 조정 가능

1장은 VMware, 1-B장은 bare-metal 설계 기준입니다. 2장부터는 공통이고, 플랫폼마다 다른 항목은 표 안에 따로 적었습니다.

---

## 1. 설계 단계: VM과 vSAN (VMware 관리자 영역)

Guest OS에서는 바꿀 수 없고, 나중에 바꾸려면 VM 정지나 데이터 이전이 필요한 항목입니다. 구축 전에 합의해 두는 것이 가장 쌉니다.

| 항목 | 기준 | 이유 | 출처 |
|---|---|---|---|
| 메모리 예약 | VM 메모리 100% 예약 | 예약 안 된 만큼은 호스트가 부족할 때 balloon·swap으로 회수됩니다. 가장 먼저 page cache가 줄어 검색이 디스크를 더 읽게 됩니다 | [VMware] |
| 메모리·CPU limit | 설정하지 않음 | limit을 넘는 메모리는 항상 balloon·swap 대상입니다 | [VMware] |
| 가상 디스크 컨트롤러 | PVSCSI. vSAN ESA면 가상 NVMe(vNVMe)도 권장 | 같은 I/O를 더 적은 CPU로 처리하고 큐를 깊게 씁니다. 디스크가 여럿이면 컨트롤러 최대 4개로 나눕니다 | [VMware] KB 392848, Troubleshooting vSAN Performance |
| 디스크 분리 | OS 디스크와 ES data 디스크를 별도 VMDK로. data VMDK는 별도 PVSCSI 컨트롤러에 | 컨트롤러 하나의 큐를 OS 로그와 ES가 나눠 쓰지 않게 합니다 | [VMware] |
| NIC | VMXNET3 | 복제본 쓰기·샤드 복구가 ES 노드 간 네트워크로 오갑니다 | [VMware] |
| 스토리지 정책 | 쓰기가 많은 hot 노드는 RAID-1(미러) 우선 검토. vSAN ESA는 VMware가 RAID-5/6도 RAID-1 수준 성능이라고 설명 | OSA의 RAID-5/6은 쓰기마다 읽기-수정-쓰기가 생겨 쓰기 지연이 늘어납니다 | [VMware] |
| 정책의 IOPS limit | 설정하지 않음 | 걸려 있으면 Guest에서는 "이유 없이 IOPS가 평평하게 막히는" 현상으로만 보입니다 | [VMware] |
| 스냅샷 | 장기 보관 금지 | 스냅샷이 남아 있으면 쓰기가 delta 파일로 가서 느려집니다 | [VMware] |
| 호스트 분산 | ES 노드 VM끼리 DRS anti-affinity | 호스트 한 대 장애로 primary와 replica를 동시에 잃지 않게 합니다 | [VMware][Elastic] |
| vSAN 여유 공간 | vSAN 버전별 권고 여유(operations / host rebuild reserve) 유지 | 여유가 없으면 resync·재구성이 느려지고 그 동안 쓰기 지연이 커집니다 | [VMware] |
| CPU hot-add | 끄기 | 켜면 vNUMA가 꺼져 메모리 접근이 느려질 수 있습니다 | [VMware] |

**쓰기 증폭 계산을 설계에 넣으세요.** ES replica 1 + vSAN FTT=1(RAID-1)이면 문서 하나가 물리적으로 4벌 기록됩니다. ES와 vSAN이 각자 가용성을 보장하므로, 용량과 쓰기 대역폭 산정 때 이 배수를 반드시 포함해야 합니다. replica를 줄일지 여부는 가용성 요구와 함께 판단할 문제이고, 디스크만 보고 결정할 일은 아닙니다.

**VMware 데이터스토어가 SAN(VMFS)·NFS라면** 위 표의 vSAN 항목 대신 어레이 쪽 기준을 봅니다. 볼륨 RAID 레벨, 어레이 쓰기 캐시와 복제 방식,
Storage I/O Control·디스크 IOPS 한도, ESXi 경로 정책(Round Robin 등)입니다. 진단 도구에는 `-s vmfs` 로 알려 주면 안내가 맞춰집니다.

---

## 1-B. 설계 단계: bare-metal 서버와 스토리지 (서버·하드웨어 담당자 영역)

나중에 바꾸려면 데이터 이전이나 장비 교체가 필요한 항목입니다.

| 항목 | 기준 | 이유 | 출처 |
|---|---|---|---|
| 매체 | SSD, 가능하면 NVMe. HDD는 warm·cold 계층에만 | SSD가 회전 디스크보다 일반적으로 빠릅니다. ES는 여러 파일을 동시에 순차·무작위로 읽고 씁니다 | [Elastic] |
| 연결 | 로컬 직결 스토리지 우선. SAN을 쓰면 실제 부하로 벤치마크 | 직결 스토리지가 지연이 낮습니다. 일부 원격 스토리지는 ES 부하에서 매우 느립니다 | [Elastic] |
| 여러 디스크 묶기 | RAID 0 또는 LVM stripe | ES replica가 이중화를 맡습니다. RAID 1/10과 겹치면 같은 문서를 여러 벌 씁니다 | [Elastic] |
| RAID 5/6 | hot 노드에는 피함 | 쓰기마다 읽기-수정-쓰기가 생깁니다 | [실무] |
| 컨트롤러 캐시 | 배터리(또는 flash) 보호 write-back. 배터리 상태 정기 점검 | 캐시가 write-through로 바뀌면 fsync마다 디스크까지 가서 쓰기 지연이 크게 늘어납니다 | [실무] |
| RAID 관리 도구 | 벤더 도구(storcli·perccli, MegaRAID 96xx·PERC 12 이후는 storcli2·perccli2, ssacli, arcconf)를 OS에 설치해 둠 | 진단 도구가 캐시·배터리·구성 디스크 상태를 자동으로 봅니다. 장애 때 확인 시간도 줄어듭니다 | [실무] |
| NVMe 냉각 | 드라이브 베이 공기 흐름 확보, 빈 슬롯 블랭크 장착 | 과열 임계값(기본 경고 온도 WCTEMP)을 넘으면 장치가 스스로 성능을 낮춥니다 | [OS] NVMe 규격 |
| NVMe 슬롯 | 장치가 지원하는 PCIe 세대·레인 수로 연결되는 슬롯 | 링크가 낮게 잡히면 최대 처리량이 그만큼 줄어듭니다 | [OS] |
| BIOS 전원 정책 | 성능 우선 프로파일 검토 (벤더 가이드 확인) | 절전 상태에서 깨어나는 시간이 I/O 완료 처리에 더해집니다 | [실무] |
| 랙·전원 분산 | ES 노드를 여러 랙·전원 계통에 나누고 awareness 설정 | 한 랙 장애로 primary와 replica를 동시에 잃지 않게 합니다 | [Elastic] |

---

## 2. OS 구성 (서버 담당자 영역)

| 항목 | 기준 | 확인 명령 | 출처 |
|---|---|---|---|
| readahead | ES data 장치 128KiB 이하 (LVM이면 dm 장치도) | `lsblk -o NAME,RA` | [Elastic] |
| I/O 스케줄러 | VM: mq-deadline 또는 none (cfq/bfq 피함). bare-metal: NVMe·SSD none 또는 kyber, HDD mq-deadline 또는 bfq | `cat /sys/block/sdX/queue/scheduler` | [OS] Red Hat |
| tuned profile | VM: virtual-guest. bare-metal: throughput-performance | `tuned-adm active` | [OS] Red Hat |
| CPU governor | bare-metal: performance (throughput-performance가 설정) | `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` | [OS] Red Hat |
| SCSI 타임아웃 | VMware: open-vm-tools 설치 시 180초로 자동 설정(60초 미만은 경고, [실무]). bare-metal 로컬 디스크는 커널 기본 30초, SAN은 multipath 벤더 권고 | `cat /sys/block/sdX/device/timeout` | [VMware] |
| I/O 통계 | `queue/iostats` = 1 | `cat /sys/block/sdX/queue/iostats` | [OS] |
| 파일시스템 | xfs 또는 ext4, 로컬 블록 장치. NFS 금지 | `findmnt -T <path.data>` | [Elastic] |
| 마운트 옵션 | noatime 또는 relatime. `discard` 대신 `fstrim.timer` | `findmnt -o OPTIONS` | [OS] |
| 파티션 정렬 | 1MiB(2048 섹터) 단위 | `parted /dev/sdX unit s print` | [OS] |
| 여러 VMDK 묶기 | LVM stripe (`lvcreate -i <디스크 수> -I 256k`). linear 금지 | `lvs -o +stripes` | [OS] |
| vm.max_map_count | 최소 262144, 권장 1048576 | `sysctl vm.max_map_count` | [Elastic] |
| swap | 비활성 권장. 불가하면 `bootstrap.memory_lock: true`, 최소한 `vm.swappiness=1` | `swapon --show` | [Elastic] |
| 파일 핸들 | 65535 이상 | `cat /proc/<pid>/limits` | [Elastic] |
| open-vm-tools | VMware만. 설치 | `vmware-toolbox-cmd -v` | [VMware] |
| 소프트웨어 RAID 점검 일정 | bare-metal md: 정기 check를 서비스 피크 밖으로 | `cat /proc/mdstat`, `/etc/cron.d/raid-check` | [OS] |
| PSI | RHEL 8·9·10은 기본 비활성. 부트 파라미터 `psi=1`로 켜 두면 포화 진단이 정확해짐 | `cat /proc/pressure/io` | [OS] |
| write barrier | `nobarrier`, `barrier=0` 금지. 전원이 끊기면 파일시스템이 깨질 수 있음 (xfs 는 커널 4.19부터 옵션 자체가 없어짐) | `findmnt -o OPTIONS -T <path.data>` | [OS] |
| LVM thin pool | ES data 는 thick LV 권장. thin 이면 데이터·메타 사용률 80% 전에 확장, 100%면 쓰기가 멈춤 | `lvs -o lv_name,data_percent,metadata_percent` | [OS] Red Hat |
| LVM snapshot | ES data LV 에 snapshot 을 오래 두지 않음. 원본에 쓸 때마다 복사가 일어나 쓰기가 느려짐 | `lvs -o lv_name,origin` | [OS] Red Hat |
| 같은 디스크 공유 | swap, path.repo(스냅샷 저장소), path.logs 는 ES data 와 다른 디스크 | `swapon --show`, `findmnt -T <경로>` | [Elastic] |
| 장치 오류 카운터 | `iotmo_cnt`·`ioerr_cnt` 가 늘면 경로·장치 점검 | `cat /sys/block/sdX/device/iotmo_cnt` | [OS] |

**PVSCSI queue depth 상향(cmd_per_lun=254, ring_pages=32)은 VMware에서도 기본 적용 대상이 아닙니다.** 진단 리포트에서 "Guest 큐 포화"가 확인됐을 때만 적용합니다. 재부팅이 필요합니다. [Broadcom KB 343323 (구 2053145)]

---

## 3. Elasticsearch 설정 (ES 운영자 영역)

| 항목 | 기준 | 이유 | 출처 |
|---|---|---|---|
| JVM heap | 자동 설정(기본값) 권장. 직접 정하면 RAM의 50% 이하, compressed oops 한도 이하(대부분 26GB 까지 안전, 일부 30GB) | 나머지 RAM이 page cache가 되어 segment 읽기를 받아 줍니다 | [Elastic] |
| path.data | 경로 하나 (여러 디스크는 OS에서 LVM stripe로 묶음) | 다중 data path는 7.13부터 deprecated | [Elastic] |
| disk watermark | 기본값(85/90/95%) 유지, 사용률은 high보다 10%p 이상 여유. 8.5부터 비율을 직접 지정하지 않으면 여유 공간 기준(max_headroom 200/150/100GB)도 함께 적용돼 큰 디스크는 더 늦게 걸림 | high를 넘으면 샤드 이동 자체가 큰 디스크 부하가 됩니다 | [Elastic] |
| shard allocation awareness | VMware: ESXi 호스트 단위. bare-metal: 랙·전원 계통 단위. 클라우드: 가용 영역 단위 | ES는 노드가 어느 호스트·랙에 있는지 모릅니다 | [Elastic] |
| 복구 속도 | `indices.recovery.max_bytes_per_sec` 기본 40mb(전용 cold·frozen 노드는 메모리에 따라 최대 250mb) 유지. 복구가 서비스를 방해할 때만 조정 | 올리면 복구는 빨라지지만 그동안 디스크를 더 씁니다 | [Elastic] |
| 인덱싱 위주 인덱스 | `refresh_interval` 연장 검토 (예: 30s) | refresh마다 작은 segment가 생기고 merge 부하가 늘어납니다 | [Elastic] |
| translog | `durability: request`(기본) 유지 | `async`는 fsync를 줄이지만 장애 시 최근 데이터를 잃을 수 있습니다. 디스크 문제를 이 설정으로 덮지 않습니다 | [Elastic] |
| merge 스레드 | HDD(bare-metal) 또는 Hybrid vSAN이면 `index.merge.scheduler.max_thread_count: 1`. 기본값은 프로세서 수의 절반(9.3 이하·8.x 는 최대 4) | 회전 디스크에서는 동시 merge가 오히려 느립니다 | [Elastic] |
| 샤드 크기 | 샤드당 10~50GB 범위 | 너무 작으면 segment·mmap이 늘고, 너무 크면 복구가 오래 걸립니다 | [Elastic] |

---

## 4. 상시 감시: 이 도구와 같은 기준으로 경보 걸기

진단 도구는 점검할 때만 돌립니다. 평소에는 Elastic 자체 모니터링이 같은 기준으로 경보를 내도록 연결해 두는 것이 guardline의 마지막 단계입니다.

### 4-1. OS 디스크 지표

응답시간·대기 I/O 는 **Linux integration**(또는 Metricbeat `linux` 모듈)의 `iostat` 데이터셋에만 있습니다. System integration 과 Metricbeat `system` 모듈의 `diskio` 에는 이 값이 없습니다.

| 수집 방식 | 켜야 하는 것 | 필드 |
|---|---|---|
| Elastic Agent (Fleet) | Linux integration 의 iostat (GA, 1.1.0부터 TSDS) | `linux.iostat.*` |
| Metricbeat | `linux` 모듈의 `iostat` metricset | `linux.iostat.*` |

예전 문서의 `system.diskio.iostat.*` 는 Metricbeat 7.17 까지만 있었고 8.0에서 없어졌습니다. 두 방식 모두 이 도구가 `/proc/diskstats`에서 계산하는 값과 같은 원본입니다.

| 경보 | 필드 | 주의 | 경고 | 출처 |
|---|---|---|---|---|
| 읽기 응답시간 (vSAN) | `linux.iostat.read.await` | flash 5ms / hybrid 10ms | 10ms / 20ms | [실무] Broadcom KB 389082 기반 |
| 읽기 응답시간 (bare-metal·SAN) | 위와 같음 | NVMe 1ms / SSD 3ms / HDD 25ms | 3ms / 6ms / 30ms | [실무] Broadcom KB 424485 장치별 경보 기준 기반 |
| 쓰기 응답시간 | `linux.iostat.write.await` | 읽기와 같음 | 읽기와 같음 | [실무] |
| 대기 I/O | `linux.iostat.queue.avg_size` | queue_depth의 50% | 80% | [실무] |

- 5분 평균처럼 창을 두고 걸어야 순간 튐에 경보가 난무하지 않습니다.
- `busy`(%util)에는 경보를 걸지 않습니다. vSAN, NVMe, RAID처럼 병렬 처리하는 장치는 100%여도 여유가 있을 수 있습니다.
- stripe로 묶은 디스크는 묶음 장치(md, dm)와 구성원 디스크 양쪽에 경보를 거세요. 한 디스크만 느린 경우는 묶음 장치 지표에서 희석됩니다.

### 4-2. ES 지표 (Stack Monitoring)

| 경보 | 기준 | 비고 |
|---|---|---|
| Disk usage | 기본 규칙 사용 (80%) | Kibana Stack Monitoring 기본 제공 |
| Thread pool write/search rejections | 기본 규칙 사용 | 기본 제공 |
| 인덱싱 스로틀 | `indices.indexing.throttle_time_in_millis` 증가 | ES 로그의 "now throttling indexing"과 함께 확인 |
| merge 적체 | `thread_pool.merge` 의 queue·active 가 계속 높음 (9.1+, 8.19+) | merge 가 디스크를 못 따라가는 직접 신호. 인덱싱 스로틀로 이어짐 |
| merge 디스크 여유 | `indices.merge.disk.watermark.high` (기본 95%, 여유 100GB) | 넘으면 새 merge 를 멈춰 segment 가 쌓이고 인덱싱이 느려짐 |
| 노드 간 쏠림 | 특정 노드의 디스크 사용 시간이 중앙값의 2배 이상 | `es_cluster_probe.sh`로 주기 확인 가능 |

---

## 5. 설정을 바꿀 때의 원칙

이 도구킷은 아무것도 바꾸지 않습니다. 리포트의 조치 안내를 적용할 때는 아래 원칙을 지키세요.

1. **한 번에 하나씩** 바꿉니다. 여러 개를 동시에 바꾸면 어느 것이 효과였는지 알 수 없습니다.
2. 바꾸기 전과 후에 같은 시간대로 이 도구를 돌려 비교합니다. 측정 없는 변경은 추측입니다.
3. **되돌리는 방법을 먼저 적어 둡니다.** sysctl·udev·마운트 옵션 모두 이전 값을 기록한 뒤 바꿉니다.
4. **재부팅이 필요한 항목**(PVSCSI 파라미터, psi=1, 컨트롤러 변경)은 롤링 재시작 계획과 함께 진행합니다. 한 노드씩, 클러스터가 green으로 돌아온 걸 확인한 뒤 다음 노드로 넘어갑니다.
5. **진단 때문에 부하를 만들지 않습니다.** 이 도구는 운영 중 실제 부하를 측정합니다. 성능 판정이 보류되면 부하 테스트 대신 인덱싱·검색 피크 시간대에 다시 수집합니다.

---

## 6. 점검 주기

| 시점 | 할 일 |
|---|---|
| ES 설치 전 | `es_disk_collect.sh --no-es` 로 OS 설정·구성 점검 (성능 판정은 부하가 붙은 뒤) |
| 서비스 투입 후 첫 피크 | 수집기 실행, 기준선(baseline) 리포트 보관 |
| 분기 1회 | 피크 시간대에 수집기 + 클러스터 프로브 실행, 기준선과 비교 |
| 설정 변경 전후 | 같은 시간대로 수집기 실행해 비교 |
| 노드 증설·용량 증설 전 | 클러스터 프로브로 쏠림 확인, 새 노드는 `--no-es` 로 설정·구성 점검 |
| 장애·지연 발생 시 | 즉시 수집기 실행 + 리포트의 "과거 7일 이력"으로 시작 시점 확인 |

---

## 7. VMware 관리자 요청 체크리스트 (복사해서 사용)

Guest에서 원리상 볼 수 없는 항목입니다. 진단 리포트와 함께 전달하세요.

```
[ES 노드 VM: ______________]  측정 시각: ______________

□ 스토리지 정책: RAID ___ / FTT ___ / stripe ___ / IOPS limit 유무 ___
□ VM 스냅샷 존재 여부
□ 메모리 예약 ___% / limit 유무
□ ES 노드 VM 간 DRS anti-affinity 규칙 유무
□ 측정 시각 esxtop: DAVG ___ / KAVG ___ / GAVG ___ (ms)
□ 측정 시각 vSAN 성능 서비스: VM 지연 ___ / 디스크 그룹 지연 ___
□ 측정 시각 resync 진행 여부
□ vSAN 네트워크: 전용 대역 / 속도 ___ / 재전송·지연 이상 유무
□ Skyline Health 경고 유무
```

---

## 7-B. 하드웨어 담당자 요청 체크리스트 (bare-metal, 복사해서 사용)

RAID 컨트롤러 뒤 상태처럼 OS에서 볼 수 없는 항목입니다. 진단 리포트와 함께 전달하세요.

```
[ES 노드 서버: ______________]  측정 시각: ______________

□ RAID 컨트롤러 모델 ___ / 펌웨어 ___ / 드라이버 ___
□ 논리 디스크 RAID 레벨 ___ / stripe 크기 ___
□ 캐시 정책: 현재 write-back / write-through (설정값과 실제 적용값 모두)
□ 배터리·캐시 모듈 상태, 학습 주기(learn cycle) 일정
□ 측정 시각 전후 컨트롤러 이벤트: 재구성, 일관성 검사, patrol read, 디스크 오류
□ 구성원 물리 디스크 상태: predictive failure, media error 수
□ BIOS System Profile (성능 / 절전)
□ BMC(iDRAC·iLO·XCC) 온도·전원 이벤트
```

## 7-C. 스토리지 관리자 요청 체크리스트 (SAN, 복사해서 사용)

```
[ES 노드 서버: ______________]  측정 시각: ______________  LUN/볼륨: ______________

□ 측정 시각 볼륨 응답시간(읽기/쓰기) ___ / 호스트 포트 응답시간 ___
□ 어레이 컨트롤러 사용률 ___ / 같은 시각 다른 호스트 부하
□ 볼륨 QoS(IOPS·처리량 한도) 유무 ___
□ SAN 스위치 포트 오류(CRC, 버퍼 부족) 유무
□ 어레이 복제(동기/비동기)·스냅샷 일정
□ 경로 수와 multipath 정책 권고값
```
