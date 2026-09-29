# 업데이트 필요 사항

마지막 대조: 2026-09-29
대조 기준: Elasticsearch 9.5.4, VMware Cloud Foundation·vSphere 9.1, RHEL 10, Linux kernel master 문서

이 문서는 두 가지를 정리합니다.
하나는 아직 끝나지 않은 업데이트 항목이고, 다른 하나는 새 버전이 나올 때 다시 확인해야 할 기준값과 그 출처입니다.
이미 반영한 변경은 `CHANGELOG.md` 0.10.0 의 "최신 공식 문서 대조" 항목에 있습니다.

---

## 1. 남은 업데이트 항목

우선순위 순서입니다. "필요한 것"이 갖춰지면 바로 반영할 수 있습니다.

| # | 항목 | 현재 상태 | 필요한 것 | 반영 위치 |
|---|---|---|---|---|
| 1 | storcli2·perccli2 JSON 해석 | 조회는 하지만 키 이름이 공개 문서로 확정되지 않았습니다. 해석을 못 하면 원문을 번들에 남기고 리포트에 알립니다 | MegaRAID 96xx 또는 Dell PERC 12(H965i 등) 장비에서 받은 `storcli2 /call show all J`, `/call/vall show all J`, `/c0/eall/sall show all J` 출력 | `es_disk_render.py` parse_storcli, `es_disk_summary.sh` raid 블록, `tests/make_bundle.py` |
| 2 | 실제 장비 출력으로 RAID 파서 검증 | storcli·ssacli·arcconf 파서는 공개 레이블로 만든 합성 출력으로만 검증했습니다 | 각 도구의 실제 출력 (정상 1건, 캐시·배터리 이상 1건이면 충분) | 같은 위치, 테스트 시나리오 교체 |
| 3 | AWS `nvme amzn stats` 출력 형식 | JSON(`-o json`)과 ebsnvme 사람이 읽는 형식을 모두 읽게 만들었습니다. nvme-cli 텍스트 형식은 실물을 보지 못했습니다 | EBS 를 쓰는 EC2 에서 `nvme amzn stats /dev/nvme1n1` 과 `-o json` 출력 | `parse_ebs_stats`, 셸 요약의 ebs 블록 |
| 4 | HPE Gen11·Gen12 도구 매핑 | SR 컨트롤러는 ssacli, MR 컨트롤러는 storcli 로 봅니다. MR Gen11 의 드라이버(megaraid_sas 또는 mpi3mr)와 Gen12 도구는 확인하지 못했습니다 | HPE QuickSpecs 또는 실제 서버의 `/sys/class/scsi_host/*/proc_name` | 수집기 RAID 블록 |
| 5 | Lenovo·Supermicro RAID | Broadcom 기반이라 storcli 로 동작한다고 보고 있지만 확인하지 않았습니다 | 벤더 문서 또는 실제 장비 | README RAID 도구 표 |
| 6 | Microchip arcconf 와 smartpqi 조합 | SmartRAID 3100·3200 은 smartpqi 드라이버와 arcconf 를 쓴다고 보고 있지만 1차 문서로 확인하지 못했습니다 | Microchip 사용자 가이드 | 수집기 arcconf 조건 |
| 7 | GCP Hyperdisk 모델명 | NVMe 로 붙는 PD 는 `nvme_card-pd` 입니다. Hyperdisk 도 같은 이름인지 확인하지 못했습니다 | C3·H3 인스턴스의 `/sys/block/nvme*/device/model` | `CLOUD_BLOCK_MODEL` |
| 8 | Azure·GCP 한도 초과 지표 | VM 안에서 볼 수 있는 한도 초과 지표를 찾지 못했습니다. 지금은 "한도에 걸린 모양" 추정 판정만 합니다 | 새 게스트 도구나 문서가 나오면 반영 | 한도 판정 블록 |
| 9 | vSAN ESA 전용 지연 기준 | Broadcom 이 ESA 수치를 따로 내지 않아 flash 기준(5ms)을 씁니다. 문서는 "ESA 는 성능 한계가 훨씬 높다"고만 합니다 | Broadcom 이 ESA 기준을 내면 교체 | `LAT_TH`, `LAT_SRC` |
| 10 | vSAN OSA hybrid 지원 종료 | VCF 9.0 공지에서 "향후 릴리스에서 중단" 예정입니다. 중단되면 `-s hybrid` 안내를 레거시로 표시합니다 | VCF 릴리스 노트 | README, 리포트 문구 |
| 11 | ES 벡터 direct IO | 9.1 의 `vector.rescoring.directio`, 9.3 의 `on_disk_rescore` 는 page cache 를 거치지 않고 디스크를 직접 읽습니다. 벡터 검색 노드에서는 캐시 적중 없이 읽기가 늘 수 있어 해석이 달라집니다. 아직 판정에 넣지 않았습니다 | 대상 노드의 JVM 옵션과 매핑 정보 수집 방식 결정 | 수집기 ES 조회, 리포트 참고 항목 |
| 12 | merge 스레드 풀 판정 | `thread_pool.merge` 는 수집만 하고 판정에는 쓰지 않습니다 | 시작·끝 두 시점 값으로 적체를 판단할 기준 정하기 | `es_disk_render.py` ES 영향 판정 |

---

## 2. 새 버전이 나올 때 다시 확인할 기준값

아래는 버전에 따라 바뀐 적이 있거나 바뀔 수 있는 항목입니다.
Elasticsearch 마이너 버전, vSphere·VCF 업데이트, RHEL 메이저 버전이 나올 때 이 표를 따라 한 번씩 확인합니다.

### Elasticsearch

| 기준 | 지금 값 | 바뀐 이력 | 확인할 곳 |
|---|---|---|---|
| vm.max_map_count | 최소 262144, 권장 1048576 | 권장값 상향 8.16 | Bootstrap checks, `vm-max-map-count` 문서 |
| disk watermark | 85/90/95%, max_headroom 200/150/100GB | headroom 추가 8.5 | Cluster-level shard allocation and routing settings |
| merge 디스크 watermark | `indices.merge.disk.watermark.high` 95%, 여유 100GB | 새 설정 | Merge settings |
| merge 스레드 기본값 | 프로세서 수의 절반 | 최대 4 제한 삭제 9.4, JVM 프로세서 수 기준 9.4.7·9.5.1 | Merge settings, `MergeSchedulerConfig` 소스 |
| merge 스레드 풀 | `thread_pool.merge` | 추가 9.1, 8.19 | Thread pools |
| JVM heap | 자동 설정 권장, compressed oops 한도(대부분 26GB, 일부 30GB) | | JVM settings |
| 복구 속도 | 40mb, 전용 cold·frozen 은 메모리에 따라 최대 250mb | | Index recovery settings |
| 다중 data path | 7.13부터 deprecated, 9.5 까지 유지 | | Path settings |
| readahead | 128KiB | 9.2부터 파일 종류별 read advice 사용(벡터는 RANDOM) | Tune for search speed, `FsDirectoryFactory` 소스 |
| 스로틀 로그 문구 | `now throttling indexing` | 메시지 뒤 형식 변경 9.1, 8.19 | `InternalEngine` 소스 |
| 상시 감시 필드 | `linux.iostat.*` | `system.diskio.iostat.*` 제거 8.0 | Linux integration 패키지 |

### VMware (Broadcom)

| 기준 | 지금 값 | 확인할 곳 |
|---|---|---|
| vSAN 지연 | flash 5ms, hybrid 20ms 미만 정상 (vSAN 7·8 대상) | Broadcom KB 389082 |
| 장치 지연 | NVMe 0.5ms, SSD 1ms 이하, HDD 10~20ms. 경보 NVMe 1ms, SSD 3ms, HDD 25ms, 30ms 초과는 critical | Broadcom KB 424485 |
| esxtop | DAVG·KAVG·GAVG 10ms 지속이면 문제 | Broadcom KB 344099 |
| PVSCSI 큐 | 기본 64/254, 상향 시 cmd_per_lun 254, ring_pages 32 | Broadcom KB 343323 (구 2053145) |
| NIC | VMXNET3 | Broadcom KB 321259 (구 1001805) |
| 컨트롤러 | PVSCSI, 디스크가 여럿이면 최대 4개로 분산, ESA 는 vNVMe | Broadcom KB 313507, 392848, Troubleshooting vSAN Performance |
| 전반 | | Performance Best Practices for VMware vSphere (현재 9.1판) |

Broadcom 은 KB 번호를 옮긴 적이 있습니다. 인용한 KB 가 열리지 않으면 제목으로 다시 찾습니다.

### Linux·RHEL

| 기준 | 지금 값 | 확인할 곳 |
|---|---|---|
| diskstats 필드 | 1~11 기본, 12~15 discard(4.19+), 16~17 flush(5.5+) | kernel `Documentation/admin-guide/iostats.rst` |
| 스케줄러 권고 | HDD mq-deadline·bfq, 고성능 SSD none·kyber, VM mq-deadline(다중 큐 HBA 는 none) | RHEL "Setting the disk scheduler" |
| TuneD | 물리 서버 throughput-performance, VM virtual-guest | RHEL "Optimizing system performance with TuneD" |
| PSI | RHEL 8·9·10 기본 비활성, `psi=1` | Red Hat 문서, kernel `PSI_DEFAULT_DISABLED` |
| systemd-detect-virt | vm-other, container-other 포함 | systemd `src/basic/virt.c` |
| NVMe 상태·온도 | state 문자열, temp1_max = 현재 과열 임계값 | kernel `drivers/nvme/host/sysfs.c`, `hwmon.c` |

### RAID·클라우드

| 기준 | 지금 값 | 확인할 곳 |
|---|---|---|
| Broadcom 새 세대 | MegaRAID 96xx 는 storcli2, 드라이버 mpi3mr | Broadcom StorCLI2 User Guide |
| Dell | PERC 11 perccli, PERC 12·13 perccli2(mpi3mr) | Dell PERC CLI Reference Guide |
| megaraid_sas SCSI 주소 | channel 0·1 물리 디스크, 2 이상 논리 디스크 | kernel `megaraid_sas.h` |
| AWS | EBS·instance store 모델명, `nvme amzn stats` 필드 | AWS EBS detailed performance statistics |
| Azure | MSFT NVMe Accelerator v1, Microsoft NVMe Direct Disk v1·v2, SCSI 는 Msft Virtual Disk | Azure `azure-vm-utils` disk identification |
| GCP | nvme_card-pd, nvme_card(N) | GoogleCloudPlatform `guest-configs` udev 규칙 |

---

## 3. 업데이트 절차

1. 위 표의 출처를 열어 값이 바뀌었는지 봅니다.
2. 바뀐 값은 코드(`es_disk_render.py` 판정과 출처 문구, `es_disk_summary.sh` 같은 판정), `README.md` 기준값 출처 표, `GUARDLINE.md` 를 함께 고칩니다.
3. 새 동작에는 `tests/make_bundle.py` 에 시나리오를 추가하고 `tests/expectations.py` 에 기대 판정을 적습니다.
4. `python3 tests/run_tests.py` 로 확인합니다. 셸 요약과 HTML 판정이 다르면 실패로 나옵니다.
5. `CHANGELOG.md` 에 무엇을 왜 바꿨는지 적고, 이 문서의 "마지막 대조" 날짜와 표를 갱신합니다.
