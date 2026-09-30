# Changelog

## [0.10.0] - 2026-09-29

VMware vSAN Guest 전용이던 판정을 bare-metal, SAN, 그 밖의 hypervisor·클라우드까지 넓혔다.
플랫폼을 실행한 서버에서 자동으로 판별하고, 플랫폼마다 기준값과 병목 위치 해석, 담당자를 바꾼다.
VMware 판정은 그대로다 (합성 번들 3종에서 0.9.5 와 판정 목록 동일).

### 플랫폼 판별
- `systemd-detect-virt -v/-c`, DMI, `/sys/hypervisor`, CPU hypervisor 플래그로 vmware / baremetal / vm / 미확정 구분.
  bare-metal 은 "가상화 없음"이 확인될 때만 판정하고, 근거가 없으면 공통 기준으로 판정하며 `--platform` 지정을 안내
- ES data 디스크별 매체와 연결 방식: NVMe(PCIe), NVMe-oF, FC·iSCSI HBA, 어레이 벤더, dm-multipath,
  RAID 컨트롤러 논리 디스크, 로컬 SSD·HDD. RAID 논리 디스크는 매체를 "추정"으로 표시
- 클라우드(AWS, Azure, Google Cloud)와 컨테이너 실행 여부 표시. 컨테이너 안이면 호스트에서 다시 실행하도록 경고

### 판정 기준
- bare-metal·SAN 응답시간: Broadcom KB 424485 장치별 경보 기준을 주의 선으로 (NVMe 1ms, SSD 3ms, HDD 25ms).
  경고·위험 단계는 실무 기준. await 에 block layer 대기가 포함되므로 "정상 범위"가 아니라 "경보 기준"을 씀
- 그 밖의 VM: 백엔드를 모르므로 vSAN All-Flash 수치를 공통 기준으로 차용 [실무 기준]
- `-s` 기본값 auto. `nvme|ssd|hdd` 추가. 매체가 섞이면 가장 느린 매체 기준.
  0.9.x 번들의 `storage=allflash` 는 사용자 선택과 구분이 안 돼 auto 로 해석

### 병목 위치와 담당자
- bare-metal 로컬: 큐가 차면 디스크 구성 포화(서버 담당자), 큐가 비었는데 느리면 디스크·컨트롤러 이상(하드웨어 담당자)
- SAN: 서버 LUN 큐 대 어레이·SAN 경로(스토리지 관리자)
- 그 밖의 VM: VM 안 대 VM 바깥(가상화·클라우드 관리자)
- NVMe 와 virtio-blk 처럼 queue_depth 가 없는 장치를 권한 문제와 구분
- 쓰기만 느림: RAID 컨트롤러 캐시, SSD write cliff, 스토리지 쓰기 경로로 플랫폼별 해석

### 새로 점검하는 항목
- 묶음(RAID 0, LVM stripe, md, multipath 경로) 안에서 한 장치만 느린 경우 (모든 플랫폼).
  stripe 는 가장 느린 구성원 속도로 움직이는데 합산 지표에서는 희석되어 보이지 않았다
- NVMe 온도(hwmon WCTEMP 도달·근접), PCIe 링크 속도·폭 저하
- /proc/mdstat degraded, resync·recovery·check 진행
- CPU governor 절전 정책, FC HBA 포트 상태
- SMART (`--smart`, 기본 꺼짐): 자가 진단 실패, 보류·미정정 섹터, NVMe critical warning 등
- 커널 로그 패턴: RAID·HBA 드라이버 오류, Medium Error, NVMe controller down, PCIe AER, md 디스크 장애, multipath 경로 소실

### VMware 전용으로 한정한 항목
- SCSI timeout 180초, PVSCSI·컨트롤러 분리, open-vm-tools, VMXNET3, vSAN 네트워크, vSAN TRIM, balloon 문구
- 스케줄러는 Red Hat 용도별 권고(HDD mq-deadline/bfq, SSD·NVMe none/kyber), tuned 는 bare-metal 에서 throughput-performance
- merge 스레드 1 권고를 bare-metal HDD 에도 적용
- awareness 안내: ESXi 호스트 / 랙·전원 / 호스트·가용 영역

### 리포트
- 헤더에 플랫폼, 판정 기준 줄에 기준 매체와 임계값
- 장치 종류·연결 방식·근거·모델 표, "원리상 볼 수 없는 것" 표를 플랫폼별로
- 담당자 그룹에 가상화·클라우드 관리자, 하드웨어 담당자, 스토리지 관리자 추가

### 수집기
- 플랫폼 판별 원자료, NVMe·FC·iSCSI·mdstat, CPU governor 수집 (모두 /proc·/sys 읽기)
- `--platform`, `--smart` 옵션. 분석기에도 `--platform`, `--storage auto|allflash|hybrid|nvme|ssd|hdd`

### 사용성: 사람이 정하던 것을 도구가 판단
- ES 주소: localhost:9200 만 시도하던 것을 ES 프로세스가 실제로 LISTEN 중인 포트(/proc/<pid>/net/tcp)에서 찾아
  http, https 순서로 접속. network.host 를 특정 IP 로 묶은 운영 노드에서 --es-url 없이 붙음
- ES 인증: 401 이고 계정을 안 줬으면 터미널에서 사용자·비밀번호를 물어봄. 비밀번호를 환경변수로 넘기지 않아도 되고 셸 history 에 남지 않음
  (es_cluster_probe.sh 도 같음)
- ES data 경로: ES 프로세스 인자와 elasticsearch.yml 의 path.data 를 수집 시작 전에 읽음. ES 가 내려가 있어도 장치를 특정
- 결과 저장 위치: -o 를 안 줬고 /tmp 가 ES data 와 같은 디스크면 /var/tmp, /root 등 다른 디스크로 자동 변경.
  예전에는 끝난 뒤 경고만 했다
- 인덱스별 조회: 이 노드 샤드 2,000개 이상 또는 클러스터 샤드 20,000개 이상이면 자동 생략 [실무 기준]
- 벤치 결과: 같은 서버의 최근 es_disk_bench.sh 결과(180일 이내)를 번들에 자동으로 넣음. --bench 로 따로 연결할 필요 없음
- es_disk_bench.sh: -t 를 안 주면 elasticsearch.yml 의 path.data 를 씀 (하나일 때)
- es_cluster_probe.sh: --es-url 이 없으면 localhost 를 http, https 순서로 시도. 끝나면 HTML 리포트까지 생성
- 분석기가 끝날 때 플랫폼과 판정 기준을 한 줄로 출력. vSAN 종류는 Guest 에서 알 수 없으므로 "자동 판정"이 아니라
  "기본값"으로 표시하고 Hybrid 면 다시 분석하는 방법을 안내
- 한 서버에 ES 노드가 여러 개면 어느 노드 기준으로 수집하는지 알림

### 하드웨어 RAID 컨트롤러 판정 (bare-metal)
- 컨트롤러 도구가 있으면 자동 조회: Broadcom·Dell `storcli`·`perccli` (JSON), HPE `ssacli`, Microchip·Adaptec `arcconf`.
  모두 show 명령, 명령당 30초 상한, 도구가 남기는 로그 파일은 결과 디렉터리 임시 위치에서 지움
- 판정: 컨트롤러 상태, 배터리·CacheVault·ZMM 상태, 컨트롤러 캐시 꺼짐, 논리 디스크 상태(degraded·offline),
  ES data 논리 디스크의 쓰기 캐시가 write-through 인지(설정은 write-back 인데 떨어진 경우 구분),
  패리티 RAID(5/6), 구성 디스크 오류(predictive failure, media error, SMART 경고, failed), rebuild·patrol read·consistency check 진행
- 구성 디스크 매체(HDD·SSD)로 RAID 논리 디스크의 매체를 확정. rotational 값에 기대던 "추정"이 도구가 있으면 사라짐
- OS 장치 연결: storcli 의 OS Drive Name, ssacli 의 Disk Name, 없으면 SCSI 주소(megaraid channel 2 · aacraid channel 0 의 target 번호)
- 커널 sysfs 만으로 보는 것: /sys/class/raid_devices(mpt*sas IR 볼륨 상태·resync), hpsa·smartpqi 의 raid_level, megaraid 펌웨어 크래시 기록
- 도구가 없으면 무엇을 설치하면 되는지 안내
- 쓰기만 느릴 때 안내가 컨트롤러 조회 결과를 반영 (write-back 정상이면 캐시 문제가 아니라고 명시)

### 서버에서는 셸만으로 끝나게 (OS 기본 도구)
- `es_disk_summary.sh` 추가: bash + awk(mawk·gawk 모두)만으로 요약 판정. 수집이 끝나면 자동 실행하고 summary.txt 로 남김.
  Python 이 없는 서버에서도 판정·조치·담당자를 바로 봄. 판정 규칙은 HTML 과 같고, 테스트가 모든 시나리오에서 두 판정이 같은지 매번 확인
- `es_disk_bench.sh`: fio 가 없으면 dd 로 순차 쓰기·읽기(direct I/O)와 4KiB fsync 지연을 잼. 결과는 다음 수집 때 자동 포함
- 수집기의 모든 수집은 /proc, /sys, coreutils, util-linux 기본 명령. 컨트롤러 도구·smartctl 은 있을 때만 씀

### 제로베이스 재점검으로 추가한 항목 (OS 기본 정보만 사용)
- flush(장치 캐시 비우기) 지연: /proc/diskstats 확장 필드(커널 5.5+). fsync 가 느린 원인을 직접 봄
- I/O 모양: 평균 I/O 크기, merge 비율. 작은 랜덤 I/O 위주인지 순차인지
- 이웃 프로세스: /proc/<pid>/io 로 ES 가 아닌 프로세스의 디스크 사용량. 백업·로그 수집기 같은 원인을 짚음
- LVM 계층: thin pool 사용률(데이터·메타), snapshot 원본(쓰기마다 복사), dm-crypt, dm-cache
- 파일시스템: inode 사용률, `nobarrier`·`sync`·`data=journal` 마운트 옵션
- 같은 디스크 공유: swap, path.repo(스냅샷 저장소), path.logs 가 ES data 디스크에 있는지
- 장치 상태: SCSI 장치 state, 명령 타임아웃·오류 카운터(iotmo_cnt·ioerr_cnt), PCIe AER 오류, NVMe controller state
- md: mismatch_cnt. RAID 컨트롤러 커널 로그 이벤트(megaraid AEN FATAL·CRIT 등)는 도구 없이도 판정
- ECK·컨테이너: ES 프로세스의 마운트 네임스페이스로 data 경로를 찾아 호스트 장치와 연결

### 최신 공식 문서 대조 (2026-09 기준: Elasticsearch 9.5, VCF 9.1, RHEL 10, kernel master)
- disk watermark: 8.5부터 있는 max_headroom(high 150GB)을 반영. 비율을 직접 지정하지 않은 큰 디스크는 실제 경계가 90%보다 늦게 옴
- JVM heap: 31GB 고정 경계 대신 노드가 알려 주는 compressed oops 사용 여부로 판정. 권고 문구를 "자동 설정 권장, 대부분 26GB·일부 30GB"로
- merge 스레드 기본값: 9.4부터 최대 4 제한이 없어진 것을 반영. merge 스레드 풀(9.1+, 8.19+)과 merge 디스크 watermark 를 상시 감시 항목에 추가, 수집에 thread_pool.merge 추가
- 복구 속도 기본값: 전용 cold·frozen 노드는 메모리에 따라 최대 250mb
- 상시 감시 필드: `system.diskio.iostat.*`(Metricbeat 8.0에서 제거)를 `linux.iostat.*`로 바꾸고, Linux integration 이 GA 인 것을 반영
- 원격 파일시스템 금지를 Elastic 공식으로 달아 둔 출처를 실무 기준으로 정정 (공식 문서는 "직결 로컬이 일반적으로 더 빠름"까지)
- Broadcom KB 번호 변경 반영: PVSCSI 큐 343323(구 2053145), VMXNET3 321259(구 1001805). KB 1010398 인용 오류를 KB 313507·392848 로 정정
- vSAN 네트워크의 "vSwitch 드롭 0.0001%" 는 현재 문서에서 확인되지 않아 삭제. esxtop 기준은 KB 344099(셋 다 10ms 지속이면 문제)
- KB 389082 는 vSAN 7·8 성능 화면 기준(flash 5ms, hybrid 20ms)으로 문구 정정. ESA 는 가상 NVMe 컨트롤러 권고 추가
- SCSI 타임아웃: "VMware 권고 60초" 출처를 확인하지 못해 open-vm-tools 기본 180초만 공식으로 두고 60초 경계는 실무 기준으로 표시
- RAID: megaraid_sas 논리 디스크는 channel 2 이상(VD = (channel-2)*128 + target)으로 연결 수정. MegaRAID 96xx·PERC 12 이후(mpi3mr)는 storcli2·perccli2 로 조회하고, JSON 을 해석하지 못하면 원문 보존을 알림
- AWS EBS: nvme-cli(amzn 플러그인) 또는 ebsnvme 가 있으면 Nitro 가 보고하는 볼륨·인스턴스 한도 초과 시간을 읽어 판정. 셸 요약에도 반영
- 클라우드 모델명: Azure NVMe 원격 디스크 "MSFT NVMe Accelerator", GCP 다중 컨트롤러 로컬 SSD "nvme_card0" 등 추가
- systemd-detect-virt 새 값(vm-other, container-other, apple, sre 등) 반영
- 문구 정정: TuneD virtual-guest(swappiness 30·dirty_ratio 30), PSI 는 RHEL 8·9·10 모두 psi=1 필요, NVMe temp1_max 는 "현재 과열 임계값(기본 WCTEMP)"
- 셸 요약의 thin pool 파싱을 컬럼 위치 고정 대신 thin-pool 다음 칸 기준으로
- 남은 업데이트 항목과 버전별 재확인 기준을 `docs/UPDATE_NOTES.md` 로 정리

### 읽기 전용 최종 점검: 쓰기·삭제·부하 제거
- `es_disk_bench.sh` 삭제. 진단 도구가 ES data 경로에 테스트 파일을 쓰고 지우는 동작이 원칙에 맞지 않고, 운영 서버에는 이미 실제 부하가 있음.
  부하가 있는 번들 32종(실측 UTM 번들 포함)에 일부러 나쁜 벤치 결과를 넣어도 HTML·셸 판정이 하나도 바뀌지 않는 것을 확인한 뒤 제거.
  함께 없앤 것: 수집기의 벤치 결과 자동 포함, 분석기 `--bench`, "최대 능력 대비 사용률" 표, 구축 전(부하 전) 판정. 큐 기준 이론 상한은 부하 테스트 없이 계산하므로 유지
- 진단 스크립트에서 `rm` 을 모두 없앰
  - 수집기: 여유 공간 확인을 결과 디렉터리를 만들기 전으로 옮겨, 실패해도 지울 것이 없게
  - 수집기: RAID 벤더 도구가 남기는 로그(storcli.log 등)를 지우지 않고 결과 디렉터리의 `hw_tool_logs/` 에 두어 번들에 포함
  - 클러스터 프로브: ES 접속을 먼저 확인하고 성공했을 때만 결과 디렉터리를 만듦
- 점검 결과: ES 호출은 모두 GET, 시스템 설정은 읽기만(sysctl -n, /sys 읽기), 벤더 도구는 show·getconfig·smartctl -H -A -i 만.
  서버에 쓰는 곳은 결과 디렉터리 하나(ES data 와 다른 디스크를 자동 선택). 예외는 수집기가 자기 프로세스의 우선순위만 낮추는 renice·ionice

### 실제 환경 검증에서 찾은 문제 (UTM, Rocky Linux 9.8 aarch64 + Elasticsearch 8.19.21)
- `_cluster/settings` 가 빈 `{}` 로 오던 문제: `flat_settings=true` 에서는 filter_path 가 점이 든 키 이름과 맞지 않음.
  중첩 응답으로 받고 분석기에서 펼치도록 수정. 예전 번들(flat 형식)도 그대로 읽음. watermark·복구·awareness 설정이 이제 실제로 들어옴
- `_ilm/explain` 405: 대상 인덱스가 경로에 있어야 하는 API. `_all/_ilm/explain` 으로 수정
- RHEL 패키지로 설치한 ES 를 컨테이너로 오판: systemd PrivateTmp 때문에 mount namespace 만 다른 경우였음.
  루트 디렉터리(장치·inode)가 다를 때만 컨테이너로 보고, 분석기도 cgroup(system.slice/*.service)으로 한 번 더 확인
- HTML 리포트가 번들(tar.gz)에 빠지던 문제: 번들을 묶기 전에 HTML 을 만들도록 순서 변경
- 셸 요약에 swap 판정 3가지 추가(측정 중 swap 입출력, swap 켜짐 + memory_lock 꺼짐, swap 이 data 디스크에 있음). HTML 과 같은 규칙
- 인덱스별 통계가 빈 응답이던 문제: level=indices 응답은 `nodes.<id>.indices.indices.<인덱스>` 로 한 단계 더 들어감(ES 소스 NodeIndicesStats 확인).
  filter_path 와 해석 모두 수정. 부하를 건 두 번째 실측에서 발견
- 인덱스 설정(`_all/_settings`)도 flat_settings + filter_path 문제로 빈 응답이던 것을 중첩 응답으로 수정. 명시 설정이 없을 때 "미수집"이 아니라 "바꾼 인덱스 없음"으로 표시
- 회귀 테스트: PrivateTmp 서비스 시나리오, 중첩·flat cluster settings 해석, 인덱스별 통계 두 모양, 중첩 인덱스 설정 (35개 시나리오)

### 구축 전 점검과 형식 변형 대응
- 구축 전 점검: ES 부하가 없을 때 `es_disk_bench.sh` 결과로 "부하 전 점검" 판정(충족 / 확인할 항목 있음 / 기준보다 느림).
  동기 쓰기(translog fsync) 한 건, 무작위 읽기(cache miss 검색) 한 건 지연을 매체별 응답시간 기준으로 판정. 셸 요약·HTML 모두
- 벤치에 무작위 읽기 동시성 1 측정(randread_4k_qd1) 추가. 벤치가 끝나면 구축 전이면 바로 `--no-es` 수집을 안내
- 부하가 낮고 벤치도 없을 때의 보류 판정 문구를 HTML 과 셸에서 같게 맞추고, 구축 전이면 벤치를 먼저 돌리라고 안내
- storcli2·perccli2: 키 이름(공백·snake_case)과 값 표기(Optimal·OPTIMAL·Write Back 등) 변형을 storcli 표기로 맞춰 읽음. 셸 요약도 같은 변형을 읽음
- nvme amzn stats: JSON(한 줄·여러 줄), 텍스트 표, 단위 붙은 텍스트까지 읽음
- merge 스레드 풀 대기가 측정 시작·끝 모두 쌓여 있으면 참고 판정
- 벡터 rescoring direct IO(-Dvector.rescoring.directio=true)가 켜진 노드는 읽기가 page cache 를 거치지 않는다고 안내
- vSAN Hybrid(OSA) 기준으로 분석하면 VCF 9.0 공지(향후 중단 예정)를 안내
- 테스트: 33개 시나리오. 셸·HTML 판정 일치에 더해 매체 추정 여부 일치, EBS 형식 변형별 값 일치까지 검사

### SMART
- bare-metal 에서는 기본으로 조회 (`--no-hw` 로 끔). VM 에서는 가상 장치라 건너뜀. RAID 컨트롤러 뒤 디스크는 컨트롤러 도구가 대신 봄

### VMware 데이터스토어 중립화
- VMware 면 vSAN 으로 가정하던 것을 고침. Guest 에서는 데이터스토어 종류를 알 수 없으므로 기본은 공통 기준(5ms)과
  vSAN·SAN·NFS 를 함께 다루는 안내. `-s allflash|hybrid` 면 vSAN, `-s vmfs` 면 SAN·NFS 데이터스토어에 맞춘 안내
- vSAN 에만 해당하는 문구(resync, vSAN 네트워크, vSAN TRIM, 디스크 그룹)를 데이터스토어 종류에 따라 분기

### 클라우드·네트워크 스토리지
- 클라우드 볼륨(EBS, Azure Disk, Persistent Disk)과 인스턴스 로컬 NVMe 를 모델명으로 구분. 로컬 NVMe 는 NVMe 기준, 볼륨은 클라우드 기준
- Ceph RBD·NBD 를 네트워크 블록 장치로 판별. 스토리지 클러스터·네트워크 쪽 안내, 담당자는 스토리지 관리자
- 모든 플랫폼: 요청은 쌓이는데 IOPS·처리량이 같은 값에서 더 오르지 않는 "한도에 걸린 모양" 판정
  (상한 근처 구간 30~90%, 그 구간 대기 I/O 가 나머지의 2배 이상일 때) [실무 기준]

### 컨테이너(ECK, Docker)
- 호스트에서 실행하면 컨테이너 안 ES 를 찾아 /proc/<pid>/root 로 elasticsearch.yml 을 읽고,
  ES 프로세스의 mountinfo 로 data 경로가 올라간 블록 장치를 찾음 (로컬 PV, Ceph RBD, 클라우드 볼륨)
- 컨테이너 안에서 실행해도 data 장치를 찾으면 경고를 참고로 낮춤

### 수정
- --no-index-stats 를 줘도 종료 시점에 인덱스별 통계를 한 번 더 조회하던 문제

### 문서·테스트
- README: 빠른 시작을 옵션 없는 실행으로, "알아서 판단하는 것" 표 추가
- README: 플랫폼별 판정 절, 병목 위치 표를 플랫폼별로, 기준값 출처 보강, 트러블슈팅
- GUARDLINE: bare-metal 설계 기준(1-B), 하드웨어·스토리지 담당자 체크리스트(7-B, 7-C)
- `tests/`: 합성 번들 생성기와 10개 시나리오 판정 테스트

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
