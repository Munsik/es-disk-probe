#!/usr/bin/env bash
# =============================================================================
# es_disk_collect.sh  (v0.10.0)
# Elasticsearch 노드 Disk I/O 진단: 데이터 수집기 (READ-ONLY)
#
#  - 시스템 설정을 바꾸지 않습니다. /proc, /sys 읽기와 ES 조회 API 호출만 합니다.
#  - 외부 패키지 불필요 (bash, awk, coreutils). iostat/sysstat 없어도 동작합니다.
#  - 샘플링 1회당 프로세스 fork는 awk 1개뿐입니다. 자기 자신은 nice 19 / ionice idle.
#  - drop_caches, fio, dd, sync 같은 부하·캐시 파괴 동작은 하지 않습니다.
#
# 사용법:
#   sudo ./es_disk_collect.sh [옵션]
#     -d SEC        측정 시간 (기본 300초. 피크 시간대에 실행 권장)
#     -i SEC        샘플 간격 (기본 5초, 최소 1초)
#     -p PATH       ES data 경로 (여러 번 지정 가능, 미지정 시 자동 탐지)
#     -o DIR        결과 저장 위치 (기본 /tmp. ES data 와 같은 디스크면 다른 디스크로 자동 변경)
#     -s TYPE       스토리지 유형 (기본 auto: 플랫폼과 장치를 보고 자동 판정)
#                   VMware: allflash | hybrid (vSAN), vmfs (SAN·NFS 데이터스토어)
#                   bare-metal·SAN: nvme | ssd | hdd (RAID 컨트롤러 뒤라 매체를 못 읽을 때 지정)
#     --platform P  플랫폼 강제 지정: auto | vmware | baremetal | vm (기본 auto)
#     --es-url URL  ES 주소 (기본 자동: ES 프로세스가 열어 둔 포트를 http → https 순서로 시도)
#     --es-user U   ES 사용자 (비밀번호는 환경변수 ES_PASSWORD)
#                   둘 다 안 주고 ES 가 인증을 요구하면 터미널에서 물어봅니다
#                   API Key 사용 시 환경변수 ES_API_KEY (base64 인코딩 값)
#     --no-es       ES API 조회 생략 (OS 레벨만 수집)
#     --no-cluster  클러스터 전체 조회 생략 (이 노드만)
#     --no-render   HTML 생성 생략 (수집 번들만 만들기)
#     --light       디스크를 읽는 부가 수집을 모두 생략 (ES 로그·커널 로그·sar·mmap 목록)
#                   → 디스크 읽기량이 1MB 미만이 됩니다. 판정 근거는 줄어듭니다
#     --no-eslog    ES 서버 로그 읽기만 생략 (부가 수집 중 읽기량이 가장 큰 항목)
#     --no-index-stats  인덱스 수에 비례해 커지는 ES 조회 생략 (샤드가 많으면 자동 생략)
#                   (_nodes/_local/stats?level=indices, _ilm/explain)
#     --no-hw       하드웨어 상태 조회를 끔 (bare-metal 에서 자동으로 하는 SMART, RAID 컨트롤러 조회)
#                   조회는 모두 읽기 전용이고 모니터링 에이전트가 주기적으로 하는 것과 같은 명령입니다
#     --smart       VM 에서도 SMART 를 읽음 (보통은 필요 없음. 가상 디스크의 SMART 는 의미가 없음)
#
# 끝나면:
#   - 화면에 셸 요약 판정을 바로 보여 주고 summary.txt 로 남깁니다 (bash + awk 만 사용. Python 불필요)
#   - Python 3.6+ 가 있으면 HTML 리포트(es_disk_report.html)도 만듭니다
#   - 번들(tar.gz)을 PC 로 가져가 es_disk_render.py 로 HTML 을 다시 만들 수 있습니다
#
# 부하 (실측, 300초/5초 간격 기준):
#   CPU 약 1.2초 (측정 시간 대비 CPU 1개의 0.4%), 메모리 12MB 미만,
#   디스크 읽기 최대 약 13MB (--light 사용 시 1MB 미만), 쓰기 1MB 미만.
#   자세한 내역은 README "이 도구가 서버에 주는 부하" 참고.
#
# 예:
#   sudo ./es_disk_collect.sh            # 대부분 이것으로 충분 (플랫폼·주소·경로·기준 자동)
#   sudo ./es_disk_collect.sh -d 600     # 피크 시간대에 10분
# =============================================================================
set -u
umask 077
export LC_ALL=C

VERSION="0.10.0"
DUR=300; INT=5; OUT_BASE="/tmp"; OUT_GIVEN=0; STORAGE="auto"; PLATFORM="auto"; SMART=0; HW=1
ES_URL=""; ES_USER=""; NO_ES=0; NO_RENDER=0; NO_CLUSTER=0
NO_ESLOG=0; NO_KLOG=0; NO_SAR=0; NO_MAPS=0; NO_IDXSTATS=0
USER_PATHS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DUR="$2"; shift 2 ;;
    -i) INT="$2"; shift 2 ;;
    -p) USER_PATHS+=("$2"); shift 2 ;;
    -o) OUT_BASE="$2"; OUT_GIVEN=1; shift 2 ;;
    -s) STORAGE="$2"; shift 2 ;;
    --es-url)  ES_URL="$2"; shift 2 ;;
    --es-user) ES_USER="$2"; shift 2 ;;
    --no-es)      NO_ES=1; shift ;;
    --no-cluster) NO_CLUSTER=1; shift ;;
    --no-render) NO_RENDER=1; shift ;;
    --no-eslog)  NO_ESLOG=1; shift ;;
    --light)     NO_ESLOG=1; NO_KLOG=1; NO_SAR=1; NO_MAPS=1; NO_IDXSTATS=1; shift ;;
    --no-index-stats) NO_IDXSTATS=1; shift ;;
    --platform)  PLATFORM="$2"; shift 2 ;;
    --smart)     SMART=1; shift ;;
    --no-hw)     HW=0; shift ;;
    -h|--help) awk 'NR>1 && /^#/{print;next} NR>1{exit}' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done
ES_PASSWORD="${ES_PASSWORD:-}"; ES_API_KEY="${ES_API_KEY:-}"

[[ "$INT" =~ ^[0-9]+$ && "$INT" -ge 1 ]] || { echo "-i 는 1 이상 정수"; exit 1; }
[[ "$DUR" =~ ^[0-9]+$ && "$DUR" -ge $((INT*3)) ]] || { echo "-d 는 간격의 3배 이상"; exit 1; }
case "$STORAGE" in auto|allflash|hybrid|vmfs|nvme|ssd|hdd) ;; *) echo "-s 는 auto|allflash|hybrid|vmfs|nvme|ssd|hdd"; exit 1 ;; esac
case "$PLATFORM" in auto|vmware|baremetal|vm) ;; *) echo "--platform 은 auto|vmware|baremetal|vm"; exit 1 ;; esac
ES_URL="${ES_URL%/}"        # 뒤 슬래시 제거. 붙어 있으면 //_cluster/health 로 요청이 나감

msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# ── 자기 자신의 우선순위를 최저로 (ES와 경합 방지) ─────────────────────────
renice -n 19 -p $$ >/dev/null 2>&1 || true
command -v ionice >/dev/null 2>&1 && ionice -c 3 -p $$ >/dev/null 2>&1 || true

IS_ROOT=0; [[ $EUID -eq 0 ]] && IS_ROOT=1
[[ $IS_ROOT -eq 0 ]] && msg "⚠ root가 아닙니다. ES 프로세스 I/O, dmesg, 가상화·장치 정보 일부가 빠질 수 있습니다."

# ── ES 프로세스와 data 경로를 먼저 찾는다 ─────────────────────────────────
# 결과 저장 위치를 고르고 ES 주소를 찾는 데 쓴다. 사용자가 경로·주소를 몰라도 되게 하려는 것
# pgrep -f 는 패턴 문자열을 포함한 다른 명령(tail, 셸 등)도 잡으므로 java 프로세스만 고른다
ES_PID=""
for p in $(pgrep -f 'org\.elasticsearch\.bootstrap\.Elasticsearch' 2>/dev/null); do
  [[ "$p" == "$$" ]] && continue
  a0=$(tr '\0' '\n' < /proc/$p/cmdline 2>/dev/null | head -1)
  c=$(cat /proc/$p/comm 2>/dev/null)
  if [[ "$c" == "java" || "${a0##*/}" == java* ]]; then ES_PID=$p; break; fi
done
N_ES=$(for p in $(pgrep -x java 2>/dev/null); do grep -qa 'org.elasticsearch.bootstrap.Elasticsearch' /proc/$p/cmdline 2>/dev/null && echo $p; done | wc -l)
[[ "$N_ES" -gt 1 ]] && msg "⚠ 이 서버에 ES 노드가 ${N_ES}개 떠 있습니다. 첫 번째(pid $ES_PID) 기준으로 수집합니다. 다른 노드는 -p 로 data 경로를 지정하세요"
ES_CMDLINE=""; [[ -n "$ES_PID" ]] && ES_CMDLINE=$(tr '\0' ' ' < /proc/$ES_PID/cmdline 2>/dev/null)
CONF_DIR=$(printf '%s' "$ES_CMDLINE" | grep -oE 'es\.path\.conf=[^ ]+' | head -1 | cut -d= -f2)
CONF_DIR=${CONF_DIR:-${ES_PATH_CONF:-/etc/elasticsearch}}
# ES 가 컨테이너(ECK, Docker) 안에 있으면 경로는 컨테이너 기준이다. 호스트에서는 /proc/<pid>/root 를 거쳐 읽는다
ES_ROOT=""
if [[ -n "$ES_PID" && "$(readlink /proc/$ES_PID/ns/mnt 2>/dev/null)" != "$(readlink /proc/self/ns/mnt 2>/dev/null)" ]]; then
  ES_ROOT="/proc/$ES_PID/root"
  msg "ES 가 컨테이너 안에서 실행 중입니다 (pid $ES_PID). 컨테이너의 mount 정보로 data 디스크를 찾습니다"
fi

# elasticsearch.yml 의 path.data. 한 줄(path.data: /a, [/a, /b])과 중첩(path:\n  data: ...), 목록(- /a) 표기를 모두 읽는다
yml_data_paths() {
  [[ -r "$1" ]] || return 0
  awk '
    /^[[:space:]]*#/ { next }
    function emit(v,   n, i, a) { gsub(/[\[\]"\047]/, "", v); n = split(v, a, ","); for (i = 1; i <= n; i++) { gsub(/^[ \t]+|[ \t]+$/, "", a[i]); if (a[i] != "") print a[i] } }
    /^path\.data[[:space:]]*:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); if (v != "") emit(v); else inlist = 1; next }
    /^path[[:space:]]*:[[:space:]]*$/ { inpath = 1; next }
    inpath && /^[[:space:]]+data[[:space:]]*:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); if (v != "") emit(v); else inlist = 1; next }
    inlist && /^[[:space:]]*-[[:space:]]*/ { v = $0; sub(/^[[:space:]]*-[[:space:]]*/, "", v); emit(v); next }
    /^[^[:space:]]/ { inpath = 0; inlist = 0 }
    /^[[:space:]]+[a-z]/ && !/^[[:space:]]+data/ { inlist = 0 }
  ' "$1" 2>/dev/null
}
DATA_PATHS=()
for p in ${USER_PATHS[@]+"${USER_PATHS[@]}"}; do DATA_PATHS+=("$p"); done
while read -r p; do [[ -n "$p" ]] && DATA_PATHS+=("$p"); done < <(
  printf '%s' "$ES_CMDLINE" | grep -oE 'path\.data=[^ ]+' | cut -d= -f2 | tr ',' '\n'
  yml_data_paths "$ES_ROOT$CONF_DIR/elasticsearch.yml")
# 기본 경로: 패키지 설치는 /var/lib/elasticsearch, 공식 컨테이너 이미지(ECK 포함)는 /usr/share/elasticsearch/data
if [[ ${#DATA_PATHS[@]} -eq 0 ]]; then
  for dflt in /var/lib/elasticsearch /usr/share/elasticsearch/data; do
    [[ -d "$ES_ROOT$dflt" ]] && { DATA_PATHS+=("$dflt"); break; }
  done
fi

# 결과 저장 위치: -o 를 안 줬으면 ES data 와 다른 파일시스템이면서 여유가 50MB 이상인 곳을 고른다.
# 측정 대상 디스크에 결과를 쓰면 그만큼 측정값이 오염되기 때문이다
same_fs_as_data() {
  local d dev; dev=$(stat -c %d "$1" 2>/dev/null) || return 1
  for d in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do
    [[ -e "$ES_ROOT$d" && "$(stat -c %d "$ES_ROOT$d" 2>/dev/null)" == "$dev" ]] && return 0
  done
  return 1
}
if [[ $OUT_GIVEN -eq 0 ]] && same_fs_as_data "$OUT_BASE"; then
  for cand in /var/tmp /root "${HOME:-/root}" /opt /home; do
    [[ -d "$cand" && -w "$cand" ]] || continue
    same_fs_as_data "$cand" && continue
    [[ "$(df -Pk "$cand" 2>/dev/null | awk 'NR==2{print $4}')" -ge 51200 ]] 2>/dev/null || continue
    msg "결과 저장 위치를 $cand 로 정했습니다 ($OUT_BASE 는 ES data 와 같은 디스크)"
    OUT_BASE="$cand"; break
  done
fi

HOST=$(hostname 2>/dev/null || echo unknown)
TS=$(date +%Y%m%d_%H%M%S)
OUT="$OUT_BASE/esdisk_${HOST}_${TS}"
S="$OUT/static"
mkdir -p "$S" || { echo "출력 디렉터리 생성 실패: $OUT"; exit 1; }

# 출력 위치 여유 공간 (50MB 미만이면 중단. 서비스 디스크를 채우지 않으려고)
AVAIL_KB=$(df -Pk "$OUT_BASE" | awk 'NR==2{print $4}')
[[ "${AVAIL_KB:-0}" -lt 51200 ]] && { echo "출력 경로 여유 공간 부족 (<50MB): $OUT_BASE"; rm -rf "$OUT"; exit 1; }

cat > "$OUT/meta" <<EOF
tool_version=$VERSION
host=$HOST
start_wall=$(date '+%Y-%m-%d %H:%M:%S %z')
start_epoch=$(date +%s)
duration=$DUR
interval=$INT
storage=$STORAGE
platform=$PLATFORM
is_root=$IS_ROOT
user_paths=${USER_PATHS[*]:-}
EOF
read -r UP0 _ < /proc/uptime; echo "start_uptime=$UP0" >> "$OUT/meta"
# -p 로 준 경로는 공백이 들어갈 수 있으므로 한 줄에 하나씩 따로 저장 (meta 는 호환용)
: > "$OUT/user_paths"
for p in ${USER_PATHS[@]+"${USER_PATHS[@]}"}; do printf '%s\n' "$p" >> "$OUT/user_paths"; done
# 수집기가 찾은 data 경로 후보 (ES 가 내려가 있어도 분석기가 장치를 특정할 수 있게)
for p in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do printf '%s\n' "$p"; done > "$S/data_paths"
# data 경로가 실제로 올라가 있는 블록 장치. ES 프로세스(없으면 이 셸)의 mountinfo 에서 major:minor 를 읽어
# /sys/dev/block 으로 장치 이름을 찾는다. 컨테이너처럼 경로가 호스트와 달라도 장치는 정확히 잡힌다
MI="/proc/${ES_PID:-self}/mountinfo"; [[ -r "$MI" ]] || MI=/proc/self/mountinfo
for p in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do
  awk -v p="$p" '
    { mp = $5; gsub(/\\040/, " ", mp)
      if (p == mp || index(p, (mp == "/" ? "/" : mp "/")) == 1) {
        if (length(mp) > bl) { bl = length(mp); best = $3 "|" mp; for (i = 7; i <= NF; i++) if ($i == "-") { best = best "|" $(i+1) "|" $(i+2); break } }
      } }
    END { if (best != "") print best }' "$MI" 2>/dev/null | while IFS='|' read -r mm mp fst src; do
      kn=$(readlink "/sys/dev/block/$mm" 2>/dev/null); kn=${kn##*/}
      printf 'DATADEV|%s|%s|%s|%s|%s|%s\n' "$p" "$mm" "${kn:-?}" "$fst" "$src" "$mp"
    done
done > "$S/datadev" 2>/dev/null
[[ -n "$ES_ROOT" ]] && echo "es_in_container=1" >> "$OUT/meta"

msg "수집 시작 → $OUT  (측정 ${DUR}s / 간격 ${INT}s)"

# ── 헬퍼 ──────────────────────────────────────────────────────────────────
save()  { local f="$1"; shift; "$@" > "$S/$f" 2>&1 || true; }
catf()  { [[ -r "$1" ]] && cat "$1" 2>/dev/null; }

# =============================================================================
# 1. 정적 스냅샷 (설정·구성)
# =============================================================================
msg "[1/4] 시스템 구성 스냅샷"
save uname      uname -a
save os-release cat /etc/os-release
save nproc      nproc
save lscpu      lscpu
save meminfo    cat /proc/meminfo
save mounts     cat /proc/mounts
save df         df -Pk
save df_i       df -Pi
save lsmod      lsmod
save swaps      cat /proc/swaps
save cmdline    cat /proc/cmdline
save lsblk      lsblk -o NAME,KNAME,TYPE,SIZE,RA,ROTA,SCHED,MOUNTPOINT,FSTYPE

# sysfs: 블록 디바이스 전체 (loop/ram/sr 제외)
{
  for d in /sys/block/*; do
    n=${d##*/}
    case "$n" in loop*|ram*|sr*|zram*) continue ;; esac
    for f in size queue/scheduler queue/rotational queue/nr_requests queue/read_ahead_kb \
             queue/max_sectors_kb queue/physical_block_size queue/logical_block_size \
             queue/minimum_io_size queue/optimal_io_size queue/nomerges queue/rq_affinity \
             queue/write_cache queue/discard_max_bytes queue/add_random \
             queue/wbt_lat_usec queue/iostats queue/max_hw_sectors_kb queue/io_poll \
             device/queue_depth device/timeout device/vendor device/model device/raid_level \
             device/state device/ioerr_cnt device/iotmo_cnt device/iorequest_cnt \
             dm/name md/level md/array_state md/sync_action md/mismatch_cnt md/degraded md/raid_disks; do
      [[ -r "$d/$f" ]] && printf 'ATTR|%s|%s|%s\n' "$n" "$f" "$(tr -d '\n' < "$d/$f" 2>/dev/null)"
    done
    for s in "$d"/slaves/*;  do [[ -e "$s" ]] && printf 'SLAVE|%s|%s\n'  "$n" "${s##*/}"; done
    for h in "$d"/holders/*; do [[ -e "$h" ]] && printf 'HOLDER|%s|%s\n' "$n" "${h##*/}"; done
    real=$(readlink -f "$d/device" 2>/dev/null || true)
    host=$(echo "$real" | grep -oE '/host[0-9]+/' | head -1 | tr -d '/')
    [[ -n "$host" ]] && printf 'SCSIHOST|%s|%s\n' "$n" "$host"
    # SCSI 주소 H:C:T:L (RAID 컨트롤러의 논리 디스크 번호와 OS 장치를 잇는 보조 근거)
    hctl=${real##*/}; [[ "$hctl" =~ ^[0-9]+:[0-9]+:[0-9]+:[0-9]+$ ]] && printf 'HCTL|%s|%s\n' "$n" "$hctl"
    # SCSI 디스크 캐시 모드(커널이 장치에서 받은 값)와 FUA 지원
    for sd in "$d"/device/scsi_disk/*; do
      [[ -d "$sd" ]] || continue
      printf 'ATTR|%s|cache_type|%s\n' "$n" "$(catf "$sd/cache_type")"
      printf 'ATTR|%s|FUA|%s\n' "$n" "$(catf "$sd/FUA")"
    done
    # 장치가 매달린 PCIe 장치(HBA·RAID 컨트롤러·NVMe)의 AER 오류 카운터 (커널 4.17+)
    pci=$(echo "$real" | grep -oE '[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]' | tail -1)
    if [[ -n "$pci" && -r "/sys/bus/pci/devices/$pci/aer_dev_fatal" ]]; then
      printf 'AER|%s|%s|cor=%s|nonfatal=%s|fatal=%s\n' "$n" "$pci" \
        "$(awk '/TOTAL_ERR_COR/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_correctable" 2>/dev/null)" \
        "$(awk '/TOTAL_ERR_NONFATAL/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_nonfatal" 2>/dev/null)" \
        "$(awk '/TOTAL_ERR_FATAL/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_fatal" 2>/dev/null)"
    fi
    # 파티션
    for p in "$d"/"$n"*; do
      [[ -r "$p/start" ]] && printf 'PART|%s|%s|%s\n' "${p##*/}" "$n" "$(cat "$p/start")"
    done
  done
  for h in /sys/class/scsi_host/host*; do
    [[ -r "$h/proc_name" ]] && printf 'HOSTDRV|%s|%s\n' "${h##*/}" "$(cat "$h/proc_name")"
    # RAID 컨트롤러 드라이버가 sysfs 로 내주는 상태 (megaraid: 펌웨어 크래시, hpsa·smartpqi: 펌웨어 버전)
    for f in fw_crash_state fw_version firmware_revision; do
      [[ -r "$h/$f" ]] && printf 'HOSTATTR|%s|%s|%s\n' "${h##*/}" "$f" "$(tr -d '\n' < "$h/$f" 2>/dev/null)"
    done
  done
  # 커널 raid_class (mpt2sas·mpt3sas IR 볼륨 등): 레벨, 상태, resync 진행
  for r in /sys/class/raid_devices/*; do
    [[ -d "$r" ]] || continue
    printf 'RAIDDEV|%s|level=%s|state=%s|resync=%s|dev=%s\n' "${r##*/}" "$(catf "$r/level")" "$(catf "$r/state")" \
      "$(catf "$r/resync")" "$(ls "$r/device/block" 2>/dev/null | head -1)"
  done
} > "$S/sysfs" 2>/dev/null

# sysctl / 커널 메모리 설정
{
  for k in vm.swappiness vm.max_map_count vm.dirty_ratio vm.dirty_background_ratio \
           vm.dirty_bytes vm.dirty_background_bytes vm.dirty_expire_centisecs \
           vm.dirty_writeback_centisecs vm.zone_reclaim_mode vm.overcommit_memory \
           vm.min_free_kbytes fs.file-max kernel.hung_task_timeout_secs; do
    printf '%s=%s\n' "$k" "$(sysctl -n "$k" 2>/dev/null)"
  done
  printf 'thp.enabled=%s\n' "$(catf /sys/kernel/mm/transparent_hugepage/enabled)"
  printf 'thp.defrag=%s\n'  "$(catf /sys/kernel/mm/transparent_hugepage/defrag)"
  [[ -r /proc/pressure/io ]] && echo "psi=available" || echo "psi=unavailable"
} > "$S/sysctl"

# 플랫폼 판별 원자료 / VMware
# 판정은 분석기가 한다. 여기서는 판단 근거가 되는 값만 모은다 (번들을 PC 에서 다시 렌더링해도 같은 결과가 나오게)
# systemd-detect-virt 는 가상화가 없으면 "none" 을 찍고 1 로 끝난다. 명령이 없을 때만 unknown
{
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    echo "detect_virt=$(systemd-detect-virt 2>/dev/null)"
    echo "detect_virt_vm=$(systemd-detect-virt -v 2>/dev/null)"
    echo "detect_virt_container=$(systemd-detect-virt -c 2>/dev/null)"
  else
    echo "detect_virt=unknown"
  fi
  echo "sys_vendor=$(catf /sys/class/dmi/id/sys_vendor)"
  echo "product_name=$(catf /sys/class/dmi/id/product_name)"
  echo "board_vendor=$(catf /sys/class/dmi/id/board_vendor)"
  echo "bios_vendor=$(catf /sys/class/dmi/id/bios_vendor)"
  echo "chassis_asset_tag=$(catf /sys/class/dmi/id/chassis_asset_tag)"
  echo "sys_hypervisor=$(catf /sys/hypervisor/type)"
  # CPUID hypervisor 비트. 가상 머신이면 1. detect-virt 가 없는 오래된 배포판의 보조 근거
  echo "cpu_hypervisor_flag=$(grep -m1 -cE '^flags.*[[:space:]]hypervisor([[:space:]]|$)' /proc/cpuinfo 2>/dev/null)"
  # 컨테이너 안에서 실행 중인지 (detect-virt 가 없을 때의 보조 근거)
  [[ -f /.dockerenv ]] && echo "dockerenv=1"
  [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] && echo "kubernetes=1"
  # CPU 주파수 정책. bare-metal 에서 powersave 면 I/O 완료 처리까지 늦어진다 (VM 에는 보통 없음)
  echo "cpu_governor=$(catf /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
  echo "cpu_scaling_driver=$(catf /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver)"
  if command -v vmware-toolbox-cmd >/dev/null 2>&1; then
    echo "tools_version=$(vmware-toolbox-cmd -v 2>/dev/null)"
    for k in balloon swap memlimit memres cpulimit cpures; do
      echo "stat_${k}=$(vmware-toolbox-cmd stat "$k" 2>/dev/null | head -1)"
    done
  else
    echo "tools_version=absent"
  fi
  for p in /sys/module/vmw_pvscsi/parameters/*; do
    [[ -r "$p" ]] && echo "pvscsi_${p##*/}=$(cat "$p")"
  done
} > "$S/virt" 2>/dev/null

# 스토리지 연결 방식과 장치 상태 (bare-metal·SAN 판정용). 전부 sysfs/proc 읽기
{
  # NVMe 컨트롤러: 모델, 펌웨어, 연결 방식(pcie 가 아니면 NVMe-oF, 즉 원격), 온도, PCIe 링크
  # hwmon 온도를 읽으면 커널이 장치에 SMART log 를 한 번 요청한다. 읽기 전용이고 실행당 1회
  for c in /sys/class/nvme/nvme*; do
    [[ -d "$c" ]] || continue
    n=${c##*/}
    for f in model firmware_rev transport state numa_node; do
      [[ -r "$c/$f" ]] && printf 'NVME|%s|%s|%s\n' "$n" "$f" "$(tr -d '\n' < "$c/$f" 2>/dev/null | sed 's/ *$//')"
    done
    for hw in "$c"/hwmon*/ "$c"/device/hwmon/hwmon*/; do
      [[ -r "${hw}temp1_input" ]] || continue
      for f in temp1_input:temp temp1_max:temp_max temp1_crit:temp_crit temp1_alarm:temp_alarm; do
        [[ -r "${hw}${f%%:*}" ]] && printf 'NVME|%s|%s|%s\n' "$n" "${f##*:}" "$(cat "${hw}${f%%:*}" 2>/dev/null)"
      done
      break
    done
    for f in current_link_speed:link_speed max_link_speed:max_link_speed current_link_width:link_width max_link_width:max_link_width; do
      [[ -r "$c/device/${f%%:*}" ]] && printf 'NVME|%s|%s|%s\n' "$n" "${f##*:}" "$(cat "$c/device/${f%%:*}" 2>/dev/null)"
    done
  done
  # FC HBA 포트 (SAN 판정)
  for h in /sys/class/fc_host/host*; do
    [[ -d "$h" ]] || continue
    printf 'FCHOST|%s|port_state=%s|speed=%s\n' "${h##*/}" "$(catf "$h/port_state")" "$(catf "$h/speed")"
  done
  # iSCSI 세션 수
  n_is=$(ls -d /sys/class/iscsi_session/session* 2>/dev/null | wc -l)
  echo "ISCSI|sessions|$n_is"
} > "$S/storage" 2>/dev/null
# 소프트웨어 RAID 상태 (resync·degraded). 메모리에서 만들어지는 값
[[ -r /proc/mdstat ]] && cat /proc/mdstat > "$S/mdstat" 2>/dev/null

# ── 하드웨어 상태 (bare-metal 에서 자동, --no-hw 로 끔) ─────────────────────
# 가상 머신의 디스크는 가상 장치라 SMART·RAID 정보가 의미가 없어 건너뛴다.
# 아래 명령은 모두 조회(show) 전용이다. 설정을 바꾸거나 self-test·재구성을 시작하는 명령은 쓰지 않는다.
# smartd, 모니터링 에이전트(Prometheus storcli exporter 등)가 주기적으로 실행하는 것과 같은 수준이다.
IS_BARE=0
case "$PLATFORM" in
  baremetal) IS_BARE=1 ;;
  auto)
    vv=$(systemd-detect-virt -v 2>/dev/null)
    if [[ "$vv" == "none" ]] || { [[ -z "$vv" ]] && ! grep -qE '^flags.*[[:space:]]hypervisor([[:space:]]|$)' /proc/cpuinfo 2>/dev/null; }; then
      IS_BARE=1
    fi ;;
esac
[[ $HW -eq 1 && $IS_BARE -eq 1 ]] && SMART=1
[[ $HW -eq 0 ]] && SMART=0
DRVS=" $(cat /sys/class/scsi_host/host*/proc_name 2>/dev/null | sort -u | tr '\n' ' ') "
find_tool() {
  local t
  for t in "$@"; do
    if [[ "$t" == /* ]]; then [[ -x "$t" ]] && { echo "$t"; return 0; }
    else command -v "$t" 2>/dev/null && return 0; fi
  done
  return 1
}
# 벤더 도구는 실행 디렉터리에 로그 파일을 남기는 것이 있어(storcli.log, UcliEvt.log) 임시 디렉터리에서 돌리고 지운다
hw_run() {  # $1=출력 파일, 나머지=명령
  local out="$1"; shift
  mkdir -p "$OUT/.hwtmp"
  { echo "#CMD $*"; (cd "$OUT/.hwtmp" && timeout 30 "$@" 2>&1 | head -c 4194304); echo; } >> "$out"
}
RAID_TOOLS=""
if [[ $HW -eq 1 && $IS_BARE -eq 1 ]]; then
  if [[ "$DRVS" == *" megaraid_sas "* || "$DRVS" == *" mpt3sas "* ]]; then
    T=$(find_tool storcli64 storcli perccli64 perccli /opt/MegaRAID/storcli/storcli64 /opt/MegaRAID/perccli/perccli64 \
                  /opt/lsi/storcli/storcli /opt/dell/perccli/perccli64 /usr/local/sbin/storcli64)
    if [[ -n "$T" ]]; then
      for c in "/call show all J" "/call/vall show all J" "/call/eall/sall show all J" "/call show patrolread J" "/call show cc J"; do
        # shellcheck disable=SC2086
        hw_run "$S/raid_storcli" "$T" $c
      done
      RAID_TOOLS+="storcli "
    elif [[ "$DRVS" == *" megaraid_sas "* ]]; then
      echo "#TOOL_ABSENT storcli/perccli" > "$S/raid_storcli"
    fi
  fi
  if [[ "$DRVS" == *" hpsa "* || "$DRVS" == *" smartpqi "* ]]; then
    T=$(find_tool ssacli hpssacli /usr/sbin/ssacli /opt/smartstorageadmin/ssacli/bin/ssacli)
    if [[ -n "$T" ]]; then hw_run "$S/raid_ssacli" "$T" ctrl all show config detail; RAID_TOOLS+="ssacli "
    else echo "#TOOL_ABSENT ssacli" > "$S/raid_ssacli"; fi
  fi
  if [[ "$DRVS" == *" aacraid "* || ( "$DRVS" == *" smartpqi "* && -z "$RAID_TOOLS" ) ]]; then
    T=$(find_tool arcconf /usr/sbin/arcconf /usr/Arcconf/arcconf)
    if [[ -n "$T" ]]; then
      for n in 1 2 3 4; do
        hw_run "$S/raid_arcconf" "$T" getconfig "$n" AL
        grep -qiE 'Invalid controller|not found' "$S/raid_arcconf" && break
      done
      RAID_TOOLS+="arcconf "
    elif [[ "$DRVS" == *" aacraid "* ]]; then echo "#TOOL_ABSENT arcconf" > "$S/raid_arcconf"; fi
  fi
  rm -rf "$OUT/.hwtmp"
fi
echo "raid_tools=$RAID_TOOLS" >> "$OUT/meta"

# SMART: RAID 컨트롤러 뒤 논리 디스크는 컨트롤러 도구가 구성 디스크 상태를 알려 주므로 건너뛴다.
# -n standby: 절전 중인 HDD 는 깨우지 않는다. 장치 하나에 15초 상한
SMART_N=0
if [[ $SMART -eq 1 ]]; then
  if command -v smartctl >/dev/null 2>&1; then
    for d in /sys/block/*; do
      n=${d##*/}
      case "$n" in loop*|ram*|sr*|zram*|dm-*|md*|nbd*|rbd*) continue ;; esac
      hdrv=$(cat "$(readlink -f "$d/device" 2>/dev/null | grep -oE '.*/host[0-9]+')/scsi_host/"*/proc_name 2>/dev/null | head -1)
      case "$hdrv" in megaraid_sas|hpsa|smartpqi|aacraid|arcmsr) continue ;; esac
      dev="/dev/$n"; [[ "$n" == nvme*n* ]] && dev="/dev/${n%n*}"
      [[ $SMART_N -ge 32 ]] && break
      echo "#DEV $n $dev"
      timeout 15 smartctl -H -A -i -n standby "$dev" 2>&1 | head -120
      SMART_N=$((SMART_N+1))
    done > "$S/smart"
  else
    echo "#SMARTCTL_ABSENT" > "$S/smart"
  fi
fi
echo "smart=$SMART" >> "$OUT/meta"
echo "hw=$HW" >> "$OUT/meta"

save tuned  tuned-adm active
save fstrim sh -c "systemctl is-enabled fstrim.timer 2>&1; systemctl is-active fstrim.timer 2>&1"
cp /proc/interrupts "$S/interrupts_start" 2>/dev/null
# 프로세스별 디스크 I/O 누적값 (측정 시작). 끝에서 한 번 더 읽어 차이로 "ES 말고 누가 디스크를 쓰는지"를 본다.
# /proc/<pid>/io 는 메모리에서 만들어지는 값이고, 사라진 프로세스는 grep 이 조용히 건너뛴다
procio() { grep -HE '^(read_bytes|write_bytes):' /proc/[0-9]*/io 2>/dev/null; grep -H . /proc/[0-9]*/comm 2>/dev/null; }
procio > "$S/procio_start"
save dmsetup_table dmsetup table
# thin pool 사용률, snapshot 채움 정도 (device-mapper 상태 조회, 읽기 전용)
save dmsetup_status dmsetup status
save udev_rules sh -c "grep -rhsE 'scheduler|read_ahead|queue/|timeout' /etc/udev/rules.d/ /usr/lib/udev/rules.d/ /lib/udev/rules.d/ 2>/dev/null | grep -v '^#' | head -100"

# 커널 로그: I/O 오류, SCSI 리셋, hung task (최근 7일, 가능한 범위)
# -n 으로 상한을 둔다: 장애가 반복되는 노드는 커널 메시지가 수십만 줄이 될 수 있고,
# 그만큼 journal 파일을 읽으면 그 자체가 디스크 부하가 된다. 최근 것부터 보므로 상한으로 충분.
KLOG_MAX_LINES=${KLOG_MAX_LINES:-20000}
KPAT='I/O error|blk_update_request|Buffer I/O error|critical medium error|Medium Error|rejecting I/O|hung_task|blocked for more than [0-9]+ seconds|remount.*read-only|XFS \(.*\).*(error|shutdown|[Cc]orruption)|EXT4-fs (error|warning)|Sense Key|(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9]|megaraid|mpt3sas|mpt2sas|hpsa|smartpqi|aacraid|qla2xxx|lpfc).*(\<abort|\<reset\>|timed out|timing out|timeout|failed|FATAL|fault)|controller is down|AER:.*(error|Error)|md/raid.*(Disk failure|not operational)|multipath.*(Failing path|remaining active paths: 0)|megaraid_sas.*/0x[0-9a-fA-F]+/(FATAL|CRIT|DEAD|WARN)|(megaraid|mpt3sas|hpsa|smartpqi|aacraid).*([Bb]attery|BBU|CacheVault|degraded|[Dd]egraded|offline|lockup|[Pp]redictive|[Rr]ebuild)|thin.*(out of data space|switching pool to|read-only mode)|device-mapper: snapshots: Invalidating'
if [[ $NO_KLOG -eq 0 ]]; then
  {
    command -v journalctl >/dev/null 2>&1 && journalctl -k --since "7 days ago" -n "$KLOG_MAX_LINES" -o short-iso --no-pager 2>/dev/null
  } | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io" 2>/dev/null
  # journal 이 없거나 결과가 비면 dmesg (메모리 ring buffer라 디스크를 읽지 않음)
  [[ -s "$S/klog_io" ]] || { dmesg -T 2>/dev/null | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io"; }
else
  : > "$S/klog_io"
fi

# 네트워크 (ES transport, 참고용)
{
  for i in /sys/class/net/*; do
    n=${i##*/}; [[ "$n" == "lo" ]] && continue
    drv=$(readlink -f "$i/device/driver" 2>/dev/null); drv=${drv##*/}
    echo "IF|$n|driver=${drv:-virtual}|mtu=$(catf "$i/mtu")|speed=$(catf "$i/speed")|state=$(catf "$i/operstate")"
    if command -v ethtool >/dev/null 2>&1; then
      ethtool -g "$n" 2>/dev/null | awk -v n="$n" 'NF==2 && $1 ~ /^(RX|TX):$/ {printf "RING|%s|%s|%s\n", n, $1, $2}'
    fi
  done
} > "$S/net" 2>/dev/null

# sar 이력: sysstat이 이미 기록해 둔 과거 데이터를 읽는다 (새로 수집하지 않음)
# sa 파일을 한 번만 읽는다. 이전에는 -d 와 -u 로 같은 파일을 두 번 읽었는데
# sar_u 는 리포트에서 쓰이지 않아 읽는 만큼이 그대로 낭비였다.
SAR_BYTES=0
if [[ $NO_SAR -eq 0 ]] && command -v sar >/dev/null 2>&1; then
  SAR_FILES=$(find /var/log/sa /var/log/sysstat -maxdepth 1 -type f -name 'sa[0-9]*' -mtime -8 2>/dev/null | sort)
  for f in $SAR_FILES; do
    echo "#FILE $f"
    S_TIME_FORMAT=ISO sar -d -p -f "$f" 2>/dev/null
  done > "$S/sar_d" 2>/dev/null
  for f in $SAR_FILES; do SAR_BYTES=$((SAR_BYTES + $(stat -c %s "$f" 2>/dev/null || echo 0))); done
fi
echo "read_sar_bytes=$SAR_BYTES" >> "$OUT/meta"

# ── ES 프로세스 (앞에서 찾은 값을 기록) ───────────────────────────────────
echo "es_instances=$N_ES" >> "$OUT/meta"
echo "es_pid=$ES_PID" >> "$OUT/meta"
if [[ -n "$ES_PID" && -d /proc/$ES_PID ]]; then
  tr '\0' '\n' < /proc/$ES_PID/cmdline > "$S/es_cmdline" 2>/dev/null
  catf /proc/$ES_PID/limits  > "$S/es_limits"
  catf /proc/$ES_PID/status  > "$S/es_status"
  catf /proc/$ES_PID/cgroup  > "$S/es_cgroup"
  ls /proc/$ES_PID/fd 2>/dev/null | wc -l > "$S/es_fdcount"
  # cgroup I/O 제한 (v2: io.max, v1: blkio.throttle)
  {
    cg=$(awk -F: '$1=="0"{print $3}' /proc/$ES_PID/cgroup 2>/dev/null)
    [[ -n "$cg" && -r "/sys/fs/cgroup$cg/io.max" ]] && sed 's/^/io.max: /' "/sys/fs/cgroup$cg/io.max"
    for f in /sys/fs/cgroup/blkio/system.slice/*elastic*/blkio.throttle.*_device; do
      [[ -s "$f" ]] && sed "s|^|${f##*/}: |" "$f"
    done
  } > "$S/es_cgroup_io" 2>/dev/null
  # elasticsearch.yml: 필요한 키만 추출 (비밀정보 제외)
  if [[ -r "$CONF_DIR/elasticsearch.yml" ]]; then
    grep -vE '^\s*#' "$CONF_DIR/elasticsearch.yml" | grep -viE 'password|secret|token|key' \
      | grep -E '^\s*(path|data|bootstrap|node\.roles|node\.attr|index\.store|cluster\.routing|- )' > "$S/es_yml" 2>/dev/null
  fi
fi

# ── ES 로그: 디스크와 직접 연결되는 메시지만 추출 ────────────────────────
ES_LOG_DIR=$(grep -oE 'es\.path\.logs=[^ ]+' "$S/es_cmdline" 2>/dev/null | head -1 | cut -d= -f2)
ES_LOG_DIR=${ES_LOG_DIR:-/var/log/elasticsearch}
ESLOGPAT='now throttling indexing|stopped throttling|disk watermark|flood stage|failed to flush|Too many open files|failed to write|translog.*(error|corrupt|recover)|overhead, spent|\[gc\]\[|shard failed|failed to recover|Data too large|timed out after'
# 로그 읽기는 이 도구의 디스크 부하 중 가장 큰 항목이다. 읽은 만큼 page cache 가 밀려나고,
# ES 노드에서는 밀려난 자리가 segment 캐시라 ES 가 그만큼 디스크를 더 읽게 된다.
# 그래서 상한을 둔다: 최신 로그에 예산을 집중하고(기본 8MB), 직전 로그 2개는 2MB 씩만 본다.
# 최종 출력은 어차피 tail -100 이므로 대부분의 경우 이 범위에서 충분하다.
# 더 과거까지 봐야 하면 ESLOG_TAIL_MB 를 올린다 (그만큼 부하도 커진다).
ESLOG_TAIL_MB=${ESLOG_TAIL_MB:-8}
ESLOG_OLD_MB=${ESLOG_OLD_MB:-2}
ESLOG_MAX_FILES=${ESLOG_MAX_FILES:-3}
ESLOG_BYTES=0
if [[ $NO_ESLOG -eq 0 && -d "$ES_LOG_DIR" ]]; then
  # 최신 수정 시각 순으로 정렬해 앞쪽(최신)에 큰 예산을 준다. gc/deprecation/audit/slowlog 제외
  i=0
  while read -r lf; do
    [[ -n "$lf" ]] || continue
    i=$((i+1))
    if [[ $i -eq 1 ]]; then mb=$ESLOG_TAIL_MB; else mb=$ESLOG_OLD_MB; fi
    sz=$(stat -c %s "$lf" 2>/dev/null || echo 0)
    want=$((mb*1048576)); [[ "$sz" -lt "$want" ]] && want=$sz
    ESLOG_BYTES=$((ESLOG_BYTES + want))
    echo "#FILE $lf (끝 ${mb}MB)"
    tail -c "$((mb*1048576))" "$lf" 2>/dev/null | grep -Eia "$ESLOGPAT" | tail -100
  done < <(find -H "$ES_LOG_DIR" -maxdepth 1 -name '*.log' -mtime -7 -size -2G -printf '%T@\t%p\n' 2>/dev/null \
            | grep -vE '(gc|deprecation|audit|slowlog|index_search|index_indexing)[^/]*\.log$' \
            | sort -rn | head -"$ESLOG_MAX_FILES" | cut -f2-) > "$S/es_log" 2>/dev/null
fi
echo "read_eslog_bytes=$ESLOG_BYTES" >> "$OUT/meta"

# ── ES 프로세스의 mmap 사용량 (max_map_count 대비 여유 확인) ───────────────
# ES 프로세스의 매핑 목록을 1회 읽는다. 읽는 동안 대상 프로세스의 mmap_lock 을 read 모드로
# 잡으므로 ES 의 mmap/munmap(segment 열기·닫기)과만 짧게 경합한다. 실측: 매핑 4만 개에 약 17ms.
# 매핑이 매우 많은 노드에서 이조차 피하고 싶으면 --light 또는 NO_MAPS 로 생략한다.
if [[ $NO_MAPS -eq 0 && -n "$ES_PID" && -r /proc/$ES_PID/maps ]]; then
  MAPS_T0=$(date +%s%N)
  wc -l < /proc/$ES_PID/maps > "$S/es_mapcount" 2>/dev/null
  echo "maps_read_ms=$(( ($(date +%s%N) - MAPS_T0) / 1000000 ))" >> "$OUT/meta"
fi

# ── ES API (조회 전용, 로컬 노드만) ────────────────────────────────────────
cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # curl -K 값 이스케이프 (\ 와 ")
ES_CALLS="$OUT/es_calls"; : > "$ES_CALLS"
es_get() {  # $1=path $2=outfile  → http code 출력
  local cfg="" code=""
  [[ -n "$ES_API_KEY" ]] && cfg="header = \"Authorization: ApiKey $(cfgesc "$ES_API_KEY")\""
  [[ -z "$ES_API_KEY" && -n "$ES_USER" ]] && cfg="user = \"$(cfgesc "${ES_USER}:${ES_PASSWORD}")\""
  code=$(printf '%s\n' "$cfg" | curl -s -k --max-time 10 --connect-timeout 3 -K - \
      -o "$2" -w '%{http_code}' "${ES_URL}/$1" 2>/dev/null || true)
  # ES 조회 비용을 리포트에 고지하기 위해 호출 수와 응답 크기를 기록 (서브셸이라 파일에 누적)
  printf '%s\t%s\t%s\n' "$code" "$(stat -c %s "$2" 2>/dev/null || echo 0)" "${1%%\?*}" >> "$ES_CALLS"
  printf '%s' "$code"
}
# ES 가 실제로 열고 있는 포트를 ES 프로세스의 소켓에서 찾는다.
# network.host 를 특정 IP 로 묶어 localhost 로는 안 붙는 경우, http.port 를 바꾼 경우에도 주소를 몰라도 되게.
# /proc/<pid>/net/tcp 는 ES 프로세스의 network namespace 기준이라 컨테이너 안 ES 도 맞게 읽힌다
es_listen_addrs() {
  local pid=$1 inodes
  [[ -n "$pid" && -d /proc/$pid/fd ]] || return 0
  inodes=$(ls -l /proc/$pid/fd 2>/dev/null | sed -n 's/.*socket:\[\([0-9]*\)\].*/\1/p' | tr '\n' ' ')
  [[ -n "$inodes" ]] || return 0
  awk -v inodes=" $inodes " '
    function h2d(h,   i, v) { v = 0; h = toupper(h); for (i = 1; i <= length(h); i++) v = v * 16 + index("0123456789ABCDEF", substr(h, i, 1)) - 1; return v }
    function v4(h) { return h2d(substr(h, 7, 2)) "." h2d(substr(h, 5, 2)) "." h2d(substr(h, 3, 2)) "." h2d(substr(h, 1, 2)) }
    FNR > 1 && $4 == "0A" && index(inodes, " " $10 " ") {
      split($2, a, ":"); ip = a[1]; host = ""
      if (length(ip) == 8) host = (ip == "00000000") ? "localhost" : v4(ip)
      else if (ip ~ /^0+$/ || ip == "00000000000000000000000001000000") host = "localhost"
      else if (substr(ip, 1, 24) == "0000000000000000FFFF0000") host = v4(substr(ip, 25, 8))
      if (host != "") print h2d(a[2]), host
    }' /proc/$pid/net/tcp /proc/$pid/net/tcp6 2>/dev/null | sort -n -u | head -8
}

ES_OK=0
if [[ $NO_ES -eq 0 ]] && command -v curl >/dev/null 2>&1; then
  c=""
  if [[ -z "$ES_URL" ]]; then
    CANDS=()
    while read -r port host; do
      [[ -n "$port" ]] && CANDS+=("$host:$port")
    done < <(es_listen_addrs "$ES_PID")
    CANDS+=("localhost:9200")
    for hp in "${CANDS[@]}"; do
      for sch in http https; do
        ES_URL="$sch://$hp"; c=$(es_get "" "$S/es_root.json")
        [[ "$c" == "200" || "$c" == "401" ]] && break 2
      done
    done
    [[ "$c" == "200" || "$c" == "401" ]] && msg "ES 주소 자동 탐지: $ES_URL"
  fi
  [[ -n "$c" ]] || c=$(es_get "" "$S/es_root.json")
  # 인증이 필요한데 계정을 안 줬으면, 터미널에서 실행 중일 때 직접 물어본다.
  # 비밀번호가 셸 history 나 프로세스 목록에 남지 않는다
  if [[ "$c" == "401" && -z "$ES_USER" && -z "$ES_API_KEY" && -t 0 && -r /dev/tty ]]; then
    msg "ES 가 인증을 요구합니다. 조회 전용 권한(monitor)이면 충분합니다. 빈 값으로 Enter 를 누르면 ES 조회 없이 진행합니다"
    read -r -p "  ES 사용자: " ES_USER < /dev/tty
    if [[ -n "$ES_USER" ]]; then
      read -rs -p "  비밀번호: " ES_PASSWORD < /dev/tty; echo >&2
      c=$(es_get "" "$S/es_root.json")
      [[ "$c" == "401" ]] && msg "⚠ 인증 실패 (401). OS 레벨만 수집합니다"
    fi
  fi
  echo "es_url=$ES_URL" >> "$OUT/meta"; echo "es_http=$c" >> "$OUT/meta"
  if [[ "$c" == "200" ]]; then
    ES_OK=1
    es_get "_nodes/_local?filter_path=nodes.*.name,nodes.*.version,nodes.*.roles,nodes.*.process.mlockall,nodes.*.settings.path,nodes.*.jvm.mem,nodes.*.os.allocated_processors" "$S/es_nodeinfo.json" >/dev/null
    es_get "_cluster/settings?include_defaults=true&flat_settings=true&filter_path=**.cluster.routing.allocation.disk*,**.cluster.routing.allocation.awareness*" "$S/es_cluster_settings.json" >/dev/null
    es_get "_cluster/health?filter_path=status,number_of_nodes,active_shards,relocating_shards,initializing_shards,unassigned_shards" "$S/es_health.json" >/dev/null
  elif [[ "$c" == "401" ]]; then
    msg "⚠ ES 인증 필요 (401). --es-user + ES_PASSWORD 또는 ES_API_KEY 지정 시 ES 지표 포함"
  else
    msg "⚠ ES API 접속 실패 (http=$c). OS 레벨만 수집합니다"
  fi
fi
NODE_STATS_PATH="_nodes/_local/stats/indices,fs,thread_pool,jvm,indexing_pressure?filter_path=nodes.*.timestamp,nodes.*.name,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.store,nodes.*.indices.segments,nodes.*.indices.translog,nodes.*.fs,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.jvm.gc,nodes.*.indexing_pressure,nodes.*.indices.shard_stats"
# 디스크와 직결되는 인덱스 설정. include_defaults 를 쓰지 않으므로 "명시적으로 바꾼 인덱스"만 응답에 들어온다
IDX_SETTINGS_PATH="_all/_settings?flat_settings=true&filter_path=**.index.translog.durability,**.index.translog.sync_interval,**.index.translog.flush_threshold_size,**.index.merge.scheduler.max_thread_count,**.index.store.type,**.index.store.preload,**.index.refresh_interval"
IDX_STATS_PATH="_nodes/_local/stats/indices?level=indices&filter_path=nodes.*.indices.*.indexing.index_total,nodes.*.indices.*.indexing.index_time_in_millis,nodes.*.indices.*.merges.total_time_in_millis,nodes.*.indices.*.merges.total_size_in_bytes,nodes.*.indices.*.refresh.total,nodes.*.indices.*.store.size_in_bytes,nodes.*.indices.*.segments.count,nodes.*.indices.*.search.query_total"
CLUSTER_STATS_PATH="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

if [[ $ES_OK -eq 1 ]]; then
  es_get "$NODE_STATS_PATH" "$S/es_stats_start.json" >/dev/null
  # 인덱스 수에 비례해 커지는 조회(인덱스별 통계, _all/_settings, _ilm/explain)는 규모가 크면 알아서 뺀다.
  # 사용자가 클러스터 규모를 보고 --no-index-stats 를 판단하지 않아도 되게. 기준은 실무 기준
  if [[ $NO_IDXSTATS -eq 0 ]]; then
    NODE_SHARDS=$(grep -oE '"total_count": *[0-9]+' "$S/es_stats_start.json" 2>/dev/null | head -1 | tr -dc '0-9')
    CL_SHARDS=$(grep -oE '"active_shards": *[0-9]+' "$S/es_health.json" 2>/dev/null | head -1 | tr -dc '0-9')
    if [[ "${NODE_SHARDS:-0}" -ge 2000 || "${CL_SHARDS:-0}" -ge 20000 ]]; then
      NO_IDXSTATS=1; echo "index_stats_auto_skip=1" >> "$OUT/meta"
      msg "샤드가 많아(이 노드 ${NODE_SHARDS:-?}개, 클러스터 ${CL_SHARDS:-?}개) 인덱스별 조회는 생략합니다"
    fi
  fi
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "$IDX_STATS_PATH" "$S/es_idx_start.json" >/dev/null
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "$IDX_SETTINGS_PATH" "$S/es_idx_settings.json" >/dev/null
  # ── 클러스터 전체: 로컬 측정과 같은 창으로 1차 스냅샷 ────────────────
  if [[ $NO_CLUSTER -eq 0 ]]; then
    C="$S/cluster"; mkdir -p "$C"
    CC=$(es_get "$CLUSTER_STATS_PATH" "$C/node_stats_1.json")
    if [[ "$CC" == "200" ]]; then
      echo "cluster=ok" >> "$OUT/meta"
    else
      echo "cluster=failed_$CC" >> "$OUT/meta"
      msg "⚠ 클러스터 조회 실패 (http=$CC). 이 노드 결과만으로 리포트를 만듭니다"
      msg "  필요 권한: cluster monitor. 권한이 없으면 --no-cluster 로 경고 없이 실행하세요."
      NO_CLUSTER=1
    fi
  fi
fi

# =============================================================================
# 2. 샘플링 루프: 한 번에 awk 1회 (fork 1개)
# =============================================================================
msg "[2/4] 샘플링 ${DUR}초 (Ctrl+C 시 그때까지의 데이터로 진행)"
STOP=0; trap 'STOP=1' INT TERM

FILES=(/proc/diskstats /proc/stat /proc/vmstat /proc/meminfo /proc/net/dev /proc/net/snmp)
[[ -r /proc/pressure/io ]]     && FILES+=(/proc/pressure/io)
[[ -r /proc/pressure/memory ]] && FILES+=(/proc/pressure/memory)

if [[ $ES_OK -eq 1 && $NO_CLUSTER -eq 0 ]]; then
  es_get "_cat/nodes?format=json&h=name,node.role,master,disk.used_percent,disk.avail,disk.total,heap.percent,ram.percent,cpu,load_1m,version" "$C/cat_nodes.json" >/dev/null
  es_get "_cat/allocation?format=json&bytes=b&h=node,shards,disk.indices,disk.used,disk.avail,disk.total,disk.percent" "$C/cat_allocation.json" >/dev/null
  es_get "_cluster/health" "$C/health.json" >/dev/null
  es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$C/cat_recovery.json" >/dev/null
  es_get "_cat/pending_tasks?format=json" "$C/pending_tasks.json" >/dev/null
  es_get "_snapshot/_status" "$C/snapshot_status.json" >/dev/null
  es_get "_cluster/settings?include_defaults=true&flat_settings=true&filter_path=**.disk.watermark*,**.disk.threshold*,**.indices.recovery*,**.node_concurrent*,**.cluster_concurrent_rebalance*,**.allocation.awareness*" "$C/cluster_settings.json" >/dev/null
  es_get "_nodes?filter_path=nodes.*.name,nodes.*.roles,nodes.*.attributes,nodes.*.host,nodes.*.ip,nodes.*.settings.path" "$C/nodes_info.json" >/dev/null
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "_ilm/explain?only_managed=true&filter_path=indices.*.phase,indices.*.policy,indices.*.action" "$C/ilm_explain.json" >/dev/null
fi

SAMPLES="$OUT/samples.raw"
END=$((SECONDS + DUR))
N=0
while [[ $STOP -eq 0 && $SECONDS -lt $END ]]; do
  read -r UP _ < /proc/uptime
  EXTRA=()
  if [[ -n "$ES_PID" && -d /proc/$ES_PID ]]; then
    EXTRA=(/proc/$ES_PID/io /proc/$ES_PID/stat /proc/$ES_PID/task/*/stat)
  fi
  printf '#T %s\n' "$UP" >> "$SAMPLES"
  awk -v pid="${ES_PID:-none}" '
    FILENAME ~ /\/task\/[0-9]+\/stat$/ {
      s=$0; sub(/.*\) /, "", s); st=substr(s,1,1); tot++; if (st=="D") d++; next
    }
    FNR==1 { print "==> " FILENAME }
    FILENAME=="/proc/vmstat"  && $1 !~ /^(pgmajfault|pswpin|pswpout|pgpgin|pgpgout)$/ { next }
    FILENAME=="/proc/meminfo" && $1 !~ /^(MemAvailable|Cached|Dirty|Writeback|MemFree):$/ { next }
    FILENAME=="/proc/stat"    && $1 !~ /^(cpu|procs_blocked|procs_running)$/ { next }
    FILENAME=="/proc/diskstats" && $3 ~ /^(loop|ram|sr|zram)/ { next }
    FILENAME=="/proc/net/snmp" && $1 != "Tcp:" { next }
    { print }
    END { print "==> DSTATE"; print d+0, tot+0 }
  ' "${FILES[@]}" "${EXTRA[@]}" >> "$SAMPLES" 2>/dev/null
  N=$((N+1))
  sleep "$INT"
done
trap - INT TERM
echo "samples=$N" >> "$OUT/meta"
read -r UP1 _ < /proc/uptime; echo "end_uptime=$UP1" >> "$OUT/meta"
echo "end_wall=$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$OUT/meta"

# =============================================================================
# 3. 종료 스냅샷
# =============================================================================
msg "[3/4] 종료 스냅샷"
if [[ $ES_OK -eq 1 ]]; then
  es_get "$NODE_STATS_PATH" "$S/es_stats_end.json" >/dev/null
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "$IDX_STATS_PATH"  "$S/es_idx_end.json"   >/dev/null
  if [[ $NO_CLUSTER -eq 0 ]]; then
    es_get "$CLUSTER_STATS_PATH" "$C/node_stats_2.json" >/dev/null
    es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$C/cat_recovery_end.json" >/dev/null
    printf 'gap=%s\nstart_wall=%s\n' "$DUR" "$(date '+%Y-%m-%d %H:%M:%S %z')" > "$C/meta"
  fi
fi
cat /proc/meminfo > "$S/meminfo_end" 2>/dev/null
cp /proc/interrupts "$S/interrupts_end" 2>/dev/null
procio > "$S/procio_end"

# ── 수집기 자체 자원 사용량 ────────────────────────────────────────────────
# bash 내장 times: 자신 / 자식 프로세스의 user·sys CPU 누적
times > "$OUT/self_overhead" 2>/dev/null
# 최대 RSS (자식 포함). ru_maxrss 를 셸에서 볼 방법이 없어 /usr/bin/time 이 있으면만 기록
du -sk "$OUT" | awk '{print "output_kb="$1}' >> "$OUT/meta"
# ES 조회 비용
awk -F'\t' '{n++; b+=$2} END{printf "es_api_calls=%d\nes_api_bytes=%d\n", n+0, b+0}' "$ES_CALLS" >> "$OUT/meta" 2>/dev/null
{
  echo "light_mode=$(( NO_ESLOG & NO_KLOG & NO_SAR & NO_MAPS ))"
  echo "skipped=$( [[ $NO_ESLOG -eq 1 ]] && printf 'eslog '; [[ $NO_KLOG -eq 1 ]] && printf 'klog '; \
                   [[ $NO_SAR -eq 1 ]] && printf 'sar '; [[ $NO_MAPS -eq 1 ]] && printf 'maps '; \
                   [[ $NO_IDXSTATS -eq 1 ]] && printf 'index-stats%s ' "$(grep -q '^index_stats_auto_skip=1' "$OUT/meta" && echo '(자동)')" )"
} >> "$OUT/meta"

# 결과를 ES data 와 같은 파일시스템에 쓰고 있으면 경고.
# 측정 대상 디스크에 쓰기를 더하는 셈이고 그만큼 측정값이 오염된다
OUT_DEV=$(df -Pk "$OUT_BASE" 2>/dev/null | awk 'NR==2{print $1}')
for dp in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do
  [[ -d "$dp" ]] || continue
  if [[ "$(df -Pk "$dp" 2>/dev/null | awk 'NR==2{print $1}')" == "$OUT_DEV" ]]; then
    msg "⚠ 결과 저장 위치($OUT_BASE)가 ES data 경로($dp)와 같은 파일시스템입니다."
    msg "  측정 대상 디스크에 쓰기를 더하게 됩니다. 다음부터는 -o 로 다른 디스크를 지정하세요."
    echo "out_on_data_fs=1" >> "$OUT/meta"
    break
  fi
done

# es_disk_bench.sh 로 이 서버에서 잰 결과가 있으면 번들에 함께 넣는다 (최근 180일, 가장 최신 1건).
# 리포트에 "최대 능력 대비 사용률"이 자동으로 나오고, --bench 로 따로 연결하지 않아도 된다
BENCH_SRC=$(for d in "$OUT_BASE" /tmp /var/tmp; do
              find "$d" -maxdepth 1 -type d -name "esbench_${HOST}_*" -mtime -180 -printf '%T@\t%p\n' 2>/dev/null
            done | sort -rn | head -1 | cut -f2-)
if [[ -n "$BENCH_SRC" ]] && ls "$BENCH_SRC"/*.json >/dev/null 2>&1; then
  mkdir -p "$OUT/bench" && cp "$BENCH_SRC"/*.json "$OUT/bench/" 2>/dev/null
  echo "bench_src=$BENCH_SRC" >> "$OUT/meta"
  msg "벤치 결과를 함께 넣었습니다: $BENCH_SRC"
fi

# =============================================================================
# 4. 번들 + HTML
# =============================================================================
msg "[4/4] 요약 판정과 번들 생성"
HERE="$(cd "$(dirname "$0")" && pwd)"
# 셸(awk)만으로 만드는 요약 판정. Python 이 없는 서버에서도 결과를 바로 본다 (summary.txt 로도 남김)
echo >&2
if [[ -f "$HERE/es_disk_summary.sh" ]]; then
  bash "$HERE/es_disk_summary.sh" "$OUT" >&2 || true
fi
echo >&2
tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null

PY=""
for c in python3 /usr/libexec/platform-python python; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [[ $NO_RENDER -eq 0 && -n "$PY" && -f "$HERE/es_disk_render.py" ]]; then
  "$PY" "$HERE/es_disk_render.py" "$OUT" -o "$OUT/es_disk_report.html" >/dev/null && \
    msg "HTML 리포트: $OUT/es_disk_report.html"
else
  msg "이 서버에는 Python 3.6+ 가 없어 HTML 리포트는 만들지 않았습니다. 위 요약이 셸 판정 결과입니다."
  msg "전체 리포트는 번들을 PC 로 옮겨: python3 es_disk_render.py $(basename "$OUT").tar.gz"
fi
msg "요약: $OUT/summary.txt"
msg "완료. 번들: $OUT.tar.gz"
