# Elasticsearch on VMware vSAN — 디스크 Guardline

이 문서는 디스크 때문에 Elasticsearch가 불안정해지는 일을 **미리 막기 위한 기준**입니다.
진단 툴(`es_disk_collect.sh`)이 점검하는 항목과 기준값이 같습니다. 툴 리포트의 "Best practice 대조표"와 나란히 보면 됩니다.

기준마다 출처 종류를 붙였습니다.

- **[Elastic]** Elastic 공식 문서의 권고
- **[VMware]** VMware/Broadcom 문서의 권고
- **[OS]** 커널·배포판 문서
- **[실무]** 공식 수치가 없어 운영 경험으로 정한 기준. 환경에 맞게 조정 가능

---

## 1. 설계 단계 — VM과 vSAN (VMware 관리자 영역)

Guest OS에서는 바꿀 수 없고, 나중에 바꾸려면 VM 정지나 데이터 이전이 필요한 항목입니다. 구축 전에 합의해 두는 것이 가장 쌉니다.

| 항목 | 기준 | 이유 | 출처 |
|---|---|---|---|
| 메모리 예약 | VM 메모리 100% 예약 | 예약 안 된 만큼은 호스트가 부족할 때 balloon·swap으로 회수됩니다. 가장 먼저 page cache가 줄어 검색이 디스크를 더 읽게 됩니다 | [VMware] |
| 메모리·CPU limit | 설정하지 않음 | limit을 넘는 메모리는 항상 balloon·swap 대상입니다 | [VMware] |
| 가상 SCSI 컨트롤러 | PVSCSI | 같은 I/O를 더 적은 CPU로 처리하고 큐를 깊게 씁니다 | [VMware] |
| 디스크 분리 | OS 디스크와 ES data 디스크를 별도 VMDK로. data VMDK는 별도 PVSCSI 컨트롤러에 | 컨트롤러 하나의 큐를 OS 로그와 ES가 나눠 쓰지 않게 합니다 | [VMware] |
| NIC | VMXNET3 | 복제본 쓰기·샤드 복구가 ES 노드 간 네트워크로 오갑니다 | [VMware] |
| 스토리지 정책 | 쓰기가 많은 hot 노드는 RAID-1(미러) 우선 검토. vSAN ESA는 VMware가 RAID-5/6도 RAID-1 수준 성능이라고 설명 | OSA의 RAID-5/6은 쓰기마다 읽기-수정-쓰기가 생겨 쓰기 지연이 늘어납니다 | [VMware] |
| 정책의 IOPS limit | 설정하지 않음 | 걸려 있으면 Guest에서는 "이유 없이 IOPS가 평평하게 막히는" 현상으로만 보입니다 | [VMware] |
| 스냅샷 | 장기 보관 금지 | 스냅샷이 남아 있으면 쓰기가 delta 파일로 가서 느려집니다 | [VMware] |
| 호스트 분산 | ES 노드 VM끼리 DRS anti-affinity | 호스트 한 대 장애로 primary와 replica를 동시에 잃지 않게 합니다 | [VMware][Elastic] |
| vSAN 여유 공간 | vSAN 버전별 권고 여유(operations / host rebuild reserve) 유지 | 여유가 없으면 resync·재구성이 느려지고 그 동안 쓰기 지연이 커집니다 | [VMware] |
| CPU hot-add | 끄기 | 켜면 vNUMA가 꺼져 메모리 접근이 느려질 수 있습니다 | [VMware] |

**쓰기 증폭 계산을 설계에 넣으세요.** ES replica 1 + vSAN FTT=1(RAID-1)이면 문서 하나가 물리적으로 4벌 기록됩니다. ES와 vSAN이 각자 가용성을 보장하므로, 용량과 쓰기 대역폭 산정 때 이 배수를 반드시 포함해야 합니다. replica를 줄일지 여부는 가용성 요구와 함께 판단할 문제이고, 디스크만 보고 결정할 일은 아닙니다.

---

## 2. Guest OS 구성 (서버 담당자 영역)

| 항목 | 기준 | 확인 명령 | 출처 |
|---|---|---|---|
| readahead | ES data 장치 128KiB 이하 (LVM이면 dm 장치도) | `lsblk -o NAME,RA` | [Elastic] |
| I/O 스케줄러 | mq-deadline 또는 none (cfq/bfq 피함) | `cat /sys/block/sdX/queue/scheduler` | [OS] |
| SCSI 타임아웃 | 60초 이상. open-vm-tools 설치 시 180초로 자동 설정 | `cat /sys/block/sdX/device/timeout` | [VMware] |
| I/O 통계 | `queue/iostats` = 1 | `cat /sys/block/sdX/queue/iostats` | [OS] |
| 파일시스템 | xfs 또는 ext4, 로컬 블록 장치. NFS 금지 | `findmnt -T <path.data>` | [Elastic] |
| 마운트 옵션 | noatime 또는 relatime. `discard` 대신 `fstrim.timer` | `findmnt -o OPTIONS` | [OS] |
| 파티션 정렬 | 1MiB(2048 섹터) 단위 | `parted /dev/sdX unit s print` | [OS] |
| 여러 VMDK 묶기 | LVM stripe (`lvcreate -i <디스크 수> -I 256k`). linear 금지 | `lvs -o +stripes` | [OS] |
| vm.max_map_count | 최소 262144, 권장 1048576 | `sysctl vm.max_map_count` | [Elastic] |
| swap | 비활성 권장. 불가하면 `bootstrap.memory_lock: true`, 최소한 `vm.swappiness=1` | `swapon --show` | [Elastic] |
| 파일 핸들 | 65535 이상 | `cat /proc/<pid>/limits` | [Elastic] |
| open-vm-tools | 설치 | `vmware-toolbox-cmd -v` | [VMware] |
| PSI | RHEL 8은 부트 파라미터 `psi=1`로 켜 두면 포화 진단이 정확해짐 | `cat /proc/pressure/io` | [OS] |

**PVSCSI queue depth 상향(cmd_per_lun=254, ring_pages=32)은 기본 적용 대상이 아닙니다.** 진단 리포트에서 "Guest 큐 포화"가 확인됐을 때만 적용합니다. 재부팅이 필요합니다. [VMware KB 2053145]

---

## 3. Elasticsearch 설정 (ES 운영자 영역)

| 항목 | 기준 | 이유 | 출처 |
|---|---|---|---|
| JVM heap | RAM의 50% 이하, 약 31GB 이하 | 나머지 RAM이 page cache가 되어 segment 읽기를 받아 줍니다 | [Elastic] |
| path.data | 경로 하나 (여러 디스크는 OS에서 LVM stripe로 묶음) | 다중 data path는 7.13부터 deprecated | [Elastic] |
| disk watermark | 기본값(85/90/95%) 유지, 사용률은 high보다 10%p 이상 여유 | high를 넘으면 샤드 이동 자체가 큰 디스크 부하가 됩니다 | [Elastic] |
| shard allocation awareness | ESXi 호스트 단위 attribute로 설정 | ES는 VM이 어느 호스트에 있는지 모릅니다 | [Elastic] |
| 복구 속도 | `indices.recovery.max_bytes_per_sec` 기본 40mb 유지. 복구가 서비스를 방해할 때만 조정 | 올리면 복구는 빨라지지만 그동안 디스크를 더 씁니다 | [Elastic] |
| 인덱싱 위주 인덱스 | `refresh_interval` 연장 검토 (예: 30s) | refresh마다 작은 segment가 생기고 merge 부하가 늘어납니다 | [Elastic] |
| translog | `durability: request`(기본) 유지 | `async`는 fsync를 줄이지만 장애 시 최근 데이터를 잃을 수 있습니다. 디스크 문제를 이 설정으로 덮지 않습니다 | [Elastic] |
| 샤드 크기 | 샤드당 10~50GB 범위 | 너무 작으면 segment·mmap이 늘고, 너무 크면 복구가 오래 걸립니다 | [Elastic] |

---

## 4. 상시 감시 — 툴과 같은 기준으로 경보 걸기

진단 툴은 점검할 때만 돌립니다. 평소에는 Elastic 자체 모니터링이 같은 기준으로 경보를 내도록 연결해 두는 것이 guardline의 마지막 단계입니다.

### 4-1. OS 디스크 지표

수집 방식에 따라 필드 위치가 다릅니다.

| 수집 방식 | 데이터 위치 | 상태 |
|---|---|---|
| Metricbeat `system` 모듈의 `diskio` | `system.diskio.iostat.*` | 기존 방식 |
| Elastic Agent (Fleet) | **Linux integration**의 `linux.iostat.*` (System integration에는 없음) | beta |

두 방식 모두 이 툴이 `/proc/diskstats`에서 계산하는 값과 같은 원본입니다. 아래 필드명은 Metricbeat 기준이며, Elastic Agent는 앞부분만 `linux.iostat.`으로 바뀝니다.

| 경보 | 필드 | 주의 | 경고 | 출처 |
|---|---|---|---|---|
| 읽기 응답시간 | `system.diskio.iostat.read.await` | All-Flash 5ms / Hybrid 10ms | 10ms / 20ms | [실무] Broadcom KB 389082 기반 |
| 쓰기 응답시간 | `system.diskio.iostat.write.await` | 위와 같음 | 위와 같음 | [실무] |
| 대기 I/O | `system.diskio.iostat.queue.avg_size` | queue_depth의 50% | 80% | [실무] |

- 5분 평균처럼 창을 두고 걸어야 순간 튐에 경보가 난무하지 않습니다.
- `busy`(%util)에는 경보를 걸지 않습니다. vSAN처럼 병렬 처리하는 장치는 100%여도 여유가 있을 수 있습니다.
- Elastic Agent의 Linux iostat은 beta라 필드나 동작이 바뀔 수 있습니다. 운영 경보로 쓰기 전에 사용 중인 버전의 문서를 확인하세요.

### 4-2. ES 지표 (Stack Monitoring)

| 경보 | 기준 | 비고 |
|---|---|---|
| Disk usage | 기본 규칙 사용 (80%) | Kibana Stack Monitoring 기본 제공 |
| Thread pool write/search rejections | 기본 규칙 사용 | 기본 제공 |
| 인덱싱 스로틀 | `indices.indexing.throttle_time_in_millis` 증가 | ES 로그의 "now throttling indexing"과 함께 확인 |
| 노드 간 쏠림 | 특정 노드의 디스크 사용 시간이 중앙값의 2배 이상 | `es_cluster_probe.sh`로 주기 확인 가능 |

---

## 5. 설정을 바꿀 때의 원칙

이 툴킷은 아무것도 바꾸지 않습니다. 리포트의 조치 안내를 적용할 때는 아래 원칙을 지키세요.

1. **한 번에 하나씩** 바꿉니다. 여러 개를 동시에 바꾸면 어느 것이 효과였는지 알 수 없습니다.
2. **바꾸기 전과 후에 같은 시간대로 툴을 돌려** 비교합니다. 측정 없는 변경은 추측입니다.
3. **되돌리는 방법을 먼저 적어 둡니다.** sysctl·udev·마운트 옵션 모두 이전 값을 기록한 뒤 바꿉니다.
4. **재부팅이 필요한 항목**(PVSCSI 파라미터, psi=1, 컨트롤러 변경)은 롤링 재시작 계획과 함께 진행합니다. 한 노드씩, 클러스터가 green으로 돌아온 걸 확인한 뒤 다음 노드로 넘어갑니다.
5. **벤치(`es_disk_bench.sh`)는 서비스 중 실행하지 않습니다.** vSAN은 공용 스토리지라 다른 VM에도 영향이 갑니다. VMware 관리자와 시간을 맞춥니다.

---

## 6. 점검 주기

| 시점 | 할 일 |
|---|---|
| 신규 구축 직후 | 수집기 + 벤치 실행, 기준선(baseline) 리포트 보관 |
| 분기 1회 | 피크 시간대에 수집기 + 클러스터 프로브 실행, 기준선과 비교 |
| 설정 변경 전후 | 같은 시간대로 수집기 실행해 비교 |
| 노드 증설·용량 증설 전 | 클러스터 프로브로 쏠림 확인, 벤치로 새 노드 능력 확인 |
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
