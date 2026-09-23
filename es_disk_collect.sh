#!/usr/bin/env bash
# =============================================================================
# es_disk_collect.sh  (v0.9.0)
# Elasticsearch 노드 Disk I/O 진단 — 데이터 수집기 (READ-ONLY)
#
#  - 시스템 설정을 바꾸지 않습니다. /proc, /sys 읽기와 ES 조회 API 호출만 합니다.
#  - 외부 패키지 불필요 (bash, awk, coreutils). iostat/sysstat 없어도 동작합니다.
#  - 샘플링 1회당 프로세스 fork는 awk 1개뿐입니다. 자기 자신은 nice 19 / ionice idle.
#  - drop_caches, fio, dd, sync 같은 부하·캐시 파괴 동작은 하지 않습니다.
#
# 사용법:
#   sudo ./es_disk_collect.sh [옵션]
#     -d SEC        측정 시간 (기본 300초 — 피크 시간대에 실행 권장)
#     -i SEC        샘플 간격 (기본 5초, 최소 1초)
#     -p PATH       ES data 경로 (여러 번 지정 가능, 미지정 시 자동 탐지)
#     -o DIR        결과 저장 위치 (기본 /tmp)
#     -s TYPE       vSAN 스토리지 유형: allflash | hybrid (기본 allflash)
#     --es-url URL  ES 주소 (기본 자동: http://localhost:9200 → https 순서)
#     --es-user U   ES 사용자 (비밀번호는 환경변수 ES_PASSWORD)
#                   API Key 사용 시 환경변수 ES_API_KEY (base64 인코딩 값)
#     --no-es       ES API 조회 생략 (OS 레벨만 수집)
#     --no-cluster  클러스터 전체 조회 생략 (이 노드만)
#     --no-render   HTML 생성 생략 (수집 번들만 만들기)
#
# 예:
#   sudo ES_PASSWORD='***' ./es_disk_collect.sh -d 600 --es-user elastic
# =============================================================================
set -u
umask 077
export LC_ALL=C

VERSION="0.9.0"
DUR=300; INT=5; OUT_BASE="/tmp"; STORAGE="allflash"
ES_URL=""; ES_USER=""; NO_ES=0; NO_RENDER=0; NO_CLUSTER=0
USER_PATHS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DUR="$2"; shift 2 ;;
    -i) INT="$2"; shift 2 ;;
    -p) USER_PATHS+=("$2"); shift 2 ;;
    -o) OUT_BASE="$2"; shift 2 ;;
    -s) STORAGE="$2"; shift 2 ;;
    --es-url)  ES_URL="$2"; shift 2 ;;
    --es-user) ES_USER="$2"; shift 2 ;;
    --no-es)      NO_ES=1; shift ;;
    --no-cluster) NO_CLUSTER=1; shift ;;
    --no-render) NO_RENDER=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done
ES_PASSWORD="${ES_PASSWORD:-}"; ES_API_KEY="${ES_API_KEY:-}"

[[ "$INT" =~ ^[0-9]+$ && "$INT" -ge 1 ]] || { echo "-i 는 1 이상 정수"; exit 1; }
[[ "$DUR" =~ ^[0-9]+$ && "$DUR" -ge $((INT*3)) ]] || { echo "-d 는 간격의 3배 이상"; exit 1; }
case "$STORAGE" in allflash|hybrid) ;; *) echo "-s 는 allflash|hybrid"; exit 1 ;; esac

msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# ── 자기 자신의 우선순위를 최저로 (ES와 경합 방지) ─────────────────────────
renice -n 19 -p $$ >/dev/null 2>&1 || true
command -v ionice >/dev/null 2>&1 && ionice -c 3 -p $$ >/dev/null 2>&1 || true

IS_ROOT=0; [[ $EUID -eq 0 ]] && IS_ROOT=1
[[ $IS_ROOT -eq 0 ]] && msg "⚠ root가 아닙니다. ES 프로세스 I/O, dmesg, VMware 정보 일부가 빠질 수 있습니다."

HOST=$(hostname 2>/dev/null || echo unknown)
TS=$(date +%Y%m%d_%H%M%S)
OUT="$OUT_BASE/esdisk_${HOST}_${TS}"
S="$OUT/static"
mkdir -p "$S" || { echo "출력 디렉터리 생성 실패: $OUT"; exit 1; }

# 출력 위치 여유 공간 (50MB 미만이면 중단 — 서비스 디스크를 채우지 않기 위함)
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
is_root=$IS_ROOT
user_paths=${USER_PATHS[*]:-}
EOF
read -r UP0 _ < /proc/uptime; echo "start_uptime=$UP0" >> "$OUT/meta"

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
             device/queue_depth device/timeout device/vendor device/model dm/name md/level; do
      [[ -r "$d/$f" ]] && printf 'ATTR|%s|%s|%s\n' "$n" "$f" "$(tr -d '\n' < "$d/$f" 2>/dev/null)"
    done
    for s in "$d"/slaves/*;  do [[ -e "$s" ]] && printf 'SLAVE|%s|%s\n'  "$n" "${s##*/}"; done
    for h in "$d"/holders/*; do [[ -e "$h" ]] && printf 'HOLDER|%s|%s\n' "$n" "${h##*/}"; done
    real=$(readlink -f "$d/device" 2>/dev/null || true)
    host=$(echo "$real" | grep -oE '/host[0-9]+/' | head -1 | tr -d '/')
    [[ -n "$host" ]] && printf 'SCSIHOST|%s|%s\n' "$n" "$host"
    # 파티션
    for p in "$d"/"$n"*; do
      [[ -r "$p/start" ]] && printf 'PART|%s|%s|%s\n' "${p##*/}" "$n" "$(cat "$p/start")"
    done
  done
  for h in /sys/class/scsi_host/host*; do
    [[ -r "$h/proc_name" ]] && printf 'HOSTDRV|%s|%s\n' "${h##*/}" "$(cat "$h/proc_name")"
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

# 가상화 / VMware
{
  echo "detect_virt=$(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "sys_vendor=$(catf /sys/class/dmi/id/sys_vendor)"
  echo "product_name=$(catf /sys/class/dmi/id/product_name)"
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

save tuned  tuned-adm active
save fstrim sh -c "systemctl is-enabled fstrim.timer 2>&1; systemctl is-active fstrim.timer 2>&1"
cp /proc/interrupts "$S/interrupts_start" 2>/dev/null
save dmsetup_table dmsetup table
save udev_rules sh -c "grep -rhsE 'scheduler|read_ahead|queue/|timeout' /etc/udev/rules.d/ /usr/lib/udev/rules.d/ /lib/udev/rules.d/ 2>/dev/null | grep -v '^#' | head -100"

# 커널 로그 — I/O 오류·SCSI 리셋·hung task (최근 7일, 가능한 범위)
KPAT='I/O error|blk_update_request|Buffer I/O error|critical medium error|rejecting I/O|hung_task|blocked for more than [0-9]+ seconds|remount.*read-only|XFS \(.*\).*(error|shutdown|[Cc]orruption)|EXT4-fs (error|warning)|Sense Key|(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9]).*(\<abort|\<reset\>|timed out|timing out|timeout|failed)'
{
  command -v journalctl >/dev/null 2>&1 && journalctl -k --since "7 days ago" -o short-iso --no-pager 2>/dev/null
} | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io" 2>/dev/null
[[ -s "$S/klog_io" ]] || { dmesg -T 2>/dev/null | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io"; }

# 네트워크 (ES transport — 참고용)
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

# sar 이력 (sysstat이 이미 수집해 둔 과거 데이터 — 추가 부하 없음)
if command -v sar >/dev/null 2>&1; then
  for f in $(find /var/log/sa /var/log/sysstat -maxdepth 1 -type f -name 'sa[0-9]*' -mtime -8 2>/dev/null | sort); do
    echo "#FILE $f"
    S_TIME_FORMAT=ISO sar -d -p -f "$f" 2>/dev/null
  done > "$S/sar_d" 2>/dev/null
  for f in $(find /var/log/sa /var/log/sysstat -maxdepth 1 -type f -name 'sa[0-9]*' -mtime -8 2>/dev/null | sort); do
    echo "#FILE $f"
    S_TIME_FORMAT=ISO sar -u -f "$f" 2>/dev/null
  done > "$S/sar_u" 2>/dev/null
fi

# ── ES 프로세스 ─────────────────────────────────────────────────────────────
# pgrep -f 는 패턴 문자열을 포함한 다른 명령(tail, 셸 등)도 잡으므로 java 프로세스만 고른다
ES_PID=""
for p in $(pgrep -f 'org\.elasticsearch\.bootstrap\.Elasticsearch' 2>/dev/null); do
  [[ "$p" == "$$" ]] && continue
  a0=$(tr '\0' '\n' < /proc/$p/cmdline 2>/dev/null | head -1)
  c=$(cat /proc/$p/comm 2>/dev/null)
  if [[ "$c" == "java" || "${a0##*/}" == java* ]]; then ES_PID=$p; break; fi
done
N_ES=$(for p in $(pgrep -x java 2>/dev/null); do grep -qa 'org.elasticsearch.bootstrap.Elasticsearch' /proc/$p/cmdline 2>/dev/null && echo $p; done | wc -l)
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
  # elasticsearch.yml — 필요한 키만 추출 (비밀정보 제외)
  CONF_DIR=$(grep -oE 'es\.path\.conf=[^ ]+' "$S/es_cmdline" 2>/dev/null | head -1 | cut -d= -f2)
  CONF_DIR=${CONF_DIR:-/etc/elasticsearch}
  if [[ -r "$CONF_DIR/elasticsearch.yml" ]]; then
    grep -vE '^\s*#' "$CONF_DIR/elasticsearch.yml" | grep -viE 'password|secret|token|key' \
      | grep -E '^\s*(path|data|bootstrap|node\.roles|node\.attr|index\.store|cluster\.routing|- )' > "$S/es_yml" 2>/dev/null
  fi
fi

# ── ES 로그 — 디스크와 직접 연결되는 메시지만 추출 ────────────────────────
ES_LOG_DIR=$(grep -oE 'es\.path\.logs=[^ ]+' "$S/es_cmdline" 2>/dev/null | head -1 | cut -d= -f2)
ES_LOG_DIR=${ES_LOG_DIR:-/var/log/elasticsearch}
ESLOGPAT='now throttling indexing|stopped throttling|disk watermark|flood stage|failed to flush|Too many open files|failed to write|translog.*(error|corrupt|recover)|overhead, spent|\[gc\]\[|shard failed|failed to recover|Data too large|timed out after'
if [[ -d "$ES_LOG_DIR" ]]; then
  # 파일 전체를 읽으면 그 자체가 디스크 읽기 부하 → 파일당 끝 32MB만 읽음. gc/deprecation/audit/slowlog 제외
  find "$ES_LOG_DIR" -maxdepth 1 -name '*.log' -mtime -7 -size -2G 2>/dev/null \
    | grep -vE '(gc|deprecation|audit|slowlog|index_search|index_indexing)[^/]*\.log$' | while read -r lf; do
    echo "#FILE $lf"; tail -c 33554432 "$lf" 2>/dev/null | grep -Eia "$ESLOGPAT" | tail -100
  done > "$S/es_log" 2>/dev/null
fi

# ── ES 프로세스의 mmap 사용량 (max_map_count 대비 여유 확인) ───────────────
if [[ -n "$ES_PID" && -r /proc/$ES_PID/maps ]]; then
  wc -l < /proc/$ES_PID/maps > "$S/es_mapcount" 2>/dev/null
fi

# ── ES API (조회 전용, 로컬 노드만) ────────────────────────────────────────
cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # curl -K 값 이스케이프 (\ 와 ")
es_get() {  # $1=path $2=outfile  → http code 출력
  local cfg=""
  [[ -n "$ES_API_KEY" ]] && cfg="header = \"Authorization: ApiKey $(cfgesc "$ES_API_KEY")\""
  [[ -z "$ES_API_KEY" && -n "$ES_USER" ]] && cfg="user = \"$(cfgesc "${ES_USER}:${ES_PASSWORD}")\""
  printf '%s\n' "$cfg" | curl -s -k --max-time 10 --connect-timeout 3 -K - \
      -o "$2" -w '%{http_code}' "${ES_URL}/$1" 2>/dev/null || true
}
ES_OK=0
if [[ $NO_ES -eq 0 ]] && command -v curl >/dev/null 2>&1; then
  if [[ -z "$ES_URL" ]]; then
    for u in http://localhost:9200 https://localhost:9200; do
      ES_URL=$u; c=$(es_get "" "$S/es_root.json")
      [[ "$c" == "200" || "$c" == "401" ]] && break
    done
  fi
  c=$(es_get "" "$S/es_root.json")
  echo "es_url=$ES_URL" >> "$OUT/meta"; echo "es_http=$c" >> "$OUT/meta"
  if [[ "$c" == "200" ]]; then
    ES_OK=1
    es_get "_nodes/_local?filter_path=nodes.*.name,nodes.*.version,nodes.*.roles,nodes.*.process.mlockall,nodes.*.settings.path,nodes.*.jvm.mem,nodes.*.os.allocated_processors" "$S/es_nodeinfo.json" >/dev/null
    es_get "_cluster/settings?include_defaults=true&flat_settings=true&filter_path=**.cluster.routing.allocation.disk*,**.cluster.routing.allocation.awareness*" "$S/es_cluster_settings.json" >/dev/null
    es_get "_cluster/health?filter_path=status,number_of_nodes,relocating_shards,initializing_shards,unassigned_shards" "$S/es_health.json" >/dev/null
  elif [[ "$c" == "401" ]]; then
    msg "⚠ ES 인증 필요 (401) — --es-user + ES_PASSWORD 또는 ES_API_KEY 지정 시 ES 지표 포함"
  else
    msg "⚠ ES API 접속 실패 (http=$c) — OS 레벨만 수집합니다"
  fi
fi
NODE_STATS_PATH="_nodes/_local/stats/indices,fs,thread_pool,jvm,indexing_pressure?filter_path=nodes.*.timestamp,nodes.*.name,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.store,nodes.*.indices.segments,nodes.*.indices.translog,nodes.*.fs,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.jvm.gc,nodes.*.indexing_pressure"
IDX_STATS_PATH="_nodes/_local/stats/indices?level=indices&filter_path=nodes.*.indices.*.indexing.index_total,nodes.*.indices.*.indexing.index_time_in_millis,nodes.*.indices.*.merges.total_time_in_millis,nodes.*.indices.*.merges.total_size_in_bytes,nodes.*.indices.*.refresh.total,nodes.*.indices.*.store.size_in_bytes,nodes.*.indices.*.segments.count,nodes.*.indices.*.search.query_total"
CLUSTER_STATS_PATH="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

if [[ $ES_OK -eq 1 ]]; then
  es_get "$NODE_STATS_PATH" "$S/es_stats_start.json" >/dev/null
  es_get "$IDX_STATS_PATH"  "$S/es_idx_start.json"   >/dev/null
  # ── 클러스터 전체: 로컬 측정과 같은 창으로 1차 스냅샷 ────────────────
  if [[ $NO_CLUSTER -eq 0 ]]; then
    C="$S/cluster"; mkdir -p "$C"
    CC=$(es_get "$CLUSTER_STATS_PATH" "$C/node_stats_1.json")
    if [[ "$CC" == "200" ]]; then
      echo "cluster=ok" >> "$OUT/meta"
    else
      echo "cluster=failed_$CC" >> "$OUT/meta"
      msg "⚠ 클러스터 조회 실패 (http=$CC) — 이 노드 결과만으로 리포트를 만듭니다"
      msg "  필요 권한: cluster monitor. 권한이 없으면 --no-cluster 로 경고 없이 실행하세요."
      NO_CLUSTER=1
    fi
  fi
fi

# =============================================================================
# 2. 샘플링 루프 — 틱당 awk 1회 (fork 1개)
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
  es_get "_ilm/explain?only_managed=true&filter_path=indices.*.phase,indices.*.policy,indices.*.action" "$C/ilm_explain.json" >/dev/null
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
  es_get "$IDX_STATS_PATH"  "$S/es_idx_end.json"   >/dev/null
  if [[ $NO_CLUSTER -eq 0 ]]; then
    es_get "$CLUSTER_STATS_PATH" "$C/node_stats_2.json" >/dev/null
    es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$C/cat_recovery_end.json" >/dev/null
    printf 'gap=%s\nstart_wall=%s\n' "$DUR" "$(date '+%Y-%m-%d %H:%M:%S %z')" > "$C/meta"
  fi
fi
cat /proc/meminfo > "$S/meminfo_end" 2>/dev/null
cp /proc/interrupts "$S/interrupts_end" 2>/dev/null

# 수집기 자체 자원 사용량 (bash 내장 times: 자신 / 자식 프로세스 user·sys 누적)
times > "$OUT/self_overhead" 2>/dev/null
du -sk "$OUT" | awk '{print "output_kb="$1}' >> "$OUT/meta"

# =============================================================================
# 4. 번들 + HTML
# =============================================================================
msg "[4/4] 번들 생성"
tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null

HERE="$(cd "$(dirname "$0")" && pwd)"
PY=""
for c in python3 /usr/libexec/platform-python python; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [[ $NO_RENDER -eq 0 && -n "$PY" && -f "$HERE/es_disk_render.py" ]]; then
  "$PY" "$HERE/es_disk_render.py" "$OUT" -o "$OUT/es_disk_report.html" && \
    msg "HTML 리포트: $OUT/es_disk_report.html"
else
  msg "HTML은 생성하지 않았습니다. 번들을 PC로 옮겨 실행하세요:"
  msg "  python3 es_disk_render.py $(basename "$OUT").tar.gz"
fi
msg "완료. 번들: $OUT.tar.gz"
