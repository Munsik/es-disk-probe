#!/usr/bin/env bash
# =============================================================================
# es_disk_collect.sh  (v0.11.0)
# Elasticsearch node Disk I/O diagnostics: data collector (READ-ONLY)
#
#  - Does not change system settings. Only reads /proc and /sys and calls ES read APIs (GET).
#  - Writes files only to one output directory (auto-picked on a disk other than ES data), and deletes nothing.
#  - Runs no tests that put load on the disk. Measures the real production load as is.
#  - No external packages needed (bash, awk, coreutils). Works without iostat/sysstat.
#  - Only one process fork (awk) per sample. Runs itself at nice 19 / ionice idle.
#  - Does nothing that adds load or destroys caches, such as drop_caches, fio, dd, sync.
#
# Usage:
#   sudo ./es_disk_collect.sh [options]
#     -d SEC        Measurement duration (default 300s. Run during peak hours)
#     -i SEC        Sample interval (default 5s, min 1s)
#     -p PATH       ES data path (repeatable, auto-detected if omitted)
#     -o DIR        Output location (default /tmp. Moved to another disk if on the same disk as ES data)
#     -s TYPE       Storage type (default auto: decided from platform and devices)
#                   VMware: allflash | hybrid (vSAN), vmfs (SAN/NFS datastore)
#                   bare-metal/SAN: nvme | ssd | hdd (set when media is unreadable behind a RAID controller)
#     --platform P  Force platform: auto | vmware | baremetal | vm (default auto)
#     --es-url URL  ES address (default auto: tries ports the ES process listens on, http then https)
#     --es-user U   ES user (password in env var ES_PASSWORD)
#                   If neither is given and ES requires auth, prompts on the terminal
#                   For API Key, env var ES_API_KEY (base64-encoded value)
#     --no-es       Skip ES API queries (OS level only)
#     --no-cluster  Skip cluster-wide queries (this node only)
#     --no-render   Skip HTML generation (bundle only)
#     --light       Skip all extra collection that reads the disk (ES logs, kernel logs, sar, mmap list)
#                   → Disk reads drop below 1MB. Less evidence for the verdict
#     --no-eslog    Skip only ES server log reading (largest reader among extra collection)
#     --no-index-stats  Skip ES queries that grow with index count (auto-skipped with many shards)
#                   (_nodes/_local/stats?level=indices, _ilm/explain)
#     --no-hw       Disable hardware status queries (SMART and RAID controller queries run automatically on bare-metal)
#                   All queries are read-only, the same commands monitoring agents run periodically
#     --smart       Read SMART on VMs too (usually unneeded. SMART on a virtual disk is meaningless)
#     --lang L      Screen language: ko | en (default from the locale). Summary and HTML are saved in both
#
# When done:
#   - Shows the shell summary verdict on screen right away and saves it as summary.ko.txt and summary.en.txt (bash + awk only. No Python needed)
#   - If Python 3.6+ is present, also builds the HTML report in both languages (es_disk_report.ko.html, es_disk_report.en.html)
#   - Copy the bundle (tar.gz) to a PC to rebuild the HTML with es_disk_render.py
#
# Overhead (measured, 300s at 5s interval):
#   CPU about 1.2s (0.4% of one CPU over the run), memory under 12MB,
#   disk reads up to about 13MB (under 1MB with --light), writes under 1MB.
#   See README "Load this tool puts on the server" for details.
#
# Examples:
#   sudo ./es_disk_collect.sh            # enough in most cases (platform, address, paths, thresholds auto)
#   sudo ./es_disk_collect.sh -d 600     # 10 min during peak hours
# =============================================================================
set -u
umask 077
# ── Language: --lang ko|en, otherwise ko when the locale starts with ko, else en ──
# Text lives in i18n/<lang>.txt (key = "text"); missing keys fall back to ko.
_LOC0="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
LNG=""; _p=""
for _a in "$@"; do [[ "$_p" == --lang ]] && LNG="$_a"; [[ "$_a" == --lang=* ]] && LNG="${_a#--lang=}"; _p="$_a"; done
[[ "$LNG" == ko || "$LNG" == en ]] || { [[ "$_LOC0" == ko* ]] && LNG=ko || LNG=en; }
HERE="$(cd "$(dirname "$0")" && pwd)"
declare -A _M=()
_catload() {  # $1=file $2=key prefix
  local line k v
  [[ -r "$1" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$2"* && "$line" == *' = "'* ]] || continue
    k="${line%% = \"*}"; [[ -n "${_M[$k]+x}" ]] && continue
    v="${line#* = \"}"; v="${v%\"}"
    v="${v//\\\"/\"}"; v="${v//\\n/$'\n'}"; v="${v//\\\\/\\}"
    _M[$k]="$v"
  done < "$1"
}
t() {  # t key [values...]  → values go into {1}, {2} ...
  local s="${_M[$1]:-$1}" i=1 a
  shift
  for a in "$@"; do s="${s//\{$i\}/$a}"; i=$((i + 1)); done
  printf '%s' "$s"
}
_catload "$HERE/i18n/$LNG.txt" c.; _catload "$HERE/i18n/ko.txt" c.
export LC_ALL=C

VERSION="0.11.0"
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
    --lang) shift 2 ;;
    --lang=*) shift ;;
    -h|--help) t c.help; echo; exit 0 ;;
    *) t c.badopt "$1"; echo; exit 1 ;;
  esac
done
ES_PASSWORD="${ES_PASSWORD:-}"; ES_API_KEY="${ES_API_KEY:-}"

[[ "$INT" =~ ^[0-9]+$ && "$INT" -ge 1 ]] || { t c.bad_i; echo; exit 1; }
[[ "$DUR" =~ ^[0-9]+$ && "$DUR" -ge $((INT*3)) ]] || { t c.bad_d; echo; exit 1; }
case "$STORAGE" in auto|allflash|hybrid|vmfs|nvme|ssd|hdd) ;; *) t c.bad_s; echo; exit 1 ;; esac
case "$PLATFORM" in auto|vmware|baremetal|vm) ;; *) t c.bad_platform; echo; exit 1 ;; esac
ES_URL="${ES_URL%/}"        # strip trailing slash. Otherwise requests go to //_cluster/health

msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# ── Lower own priority to the minimum (avoid contending with ES) ─────────────────────────
renice -n 19 -p $$ >/dev/null 2>&1 || true
command -v ionice >/dev/null 2>&1 && ionice -c 3 -p $$ >/dev/null 2>&1 || true

IS_ROOT=0; [[ $EUID -eq 0 ]] && IS_ROOT=1
[[ $IS_ROOT -eq 0 ]] && msg "$(t c.notroot)"

# ── Find the ES process and data paths first ─────────────────────────────────
# Used to pick the output location and find the ES address, so the user need not know paths or addresses
# pgrep -f also matches other commands containing the pattern (tail, shells, etc.), so pick only java processes
ES_PID=""
for p in $(pgrep -f 'org\.elasticsearch\.bootstrap\.Elasticsearch' 2>/dev/null); do
  [[ "$p" == "$$" ]] && continue
  a0=$(tr '\0' '\n' < /proc/$p/cmdline 2>/dev/null | head -1)
  c=$(cat /proc/$p/comm 2>/dev/null)
  if [[ "$c" == "java" || "${a0##*/}" == java* ]]; then ES_PID=$p; break; fi
done
N_ES=$(for p in $(pgrep -x java 2>/dev/null); do grep -qa 'org.elasticsearch.bootstrap.Elasticsearch' /proc/$p/cmdline 2>/dev/null && echo $p; done | wc -l)
[[ "$N_ES" -gt 1 ]] && msg "$(t c.multi_es "$N_ES" "$ES_PID")"
ES_CMDLINE=""; [[ -n "$ES_PID" ]] && ES_CMDLINE=$(tr '\0' ' ' < /proc/$ES_PID/cmdline 2>/dev/null)
CONF_DIR=$(printf '%s' "$ES_CMDLINE" | grep -oE 'es\.path\.conf=[^ ]+' | head -1 | cut -d= -f2)
CONF_DIR=${CONF_DIR:-${ES_PATH_CONF:-/etc/elasticsearch}}
# If ES is in a container (ECK, Docker), paths are container-relative. From the host, read them via /proc/<pid>/root.
# A different mount namespace alone (systemd PrivateTmp etc., as with RHEL elasticsearch.service) is not a container.
# Treat it as a container only when the root directory itself (device, inode) differs
ES_ROOT=""
if [[ -n "$ES_PID" && -r /proc/$ES_PID/root/ ]] && \
   [[ "$(stat -L -c '%d:%i' /proc/$ES_PID/root/ 2>/dev/null)" != "$(stat -L -c '%d:%i' / 2>/dev/null)" ]]; then
  ES_ROOT="/proc/$ES_PID/root"
  msg "$(t c.container "$ES_PID")"
fi

# path.data in elasticsearch.yml. Reads single-line (path.data: /a, [/a, /b]), nested (path:\n  data: ...) and list (- /a) forms
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
# Default paths: /var/lib/elasticsearch for package installs, /usr/share/elasticsearch/data for official container images (incl. ECK)
if [[ ${#DATA_PATHS[@]} -eq 0 ]]; then
  for dflt in /var/lib/elasticsearch /usr/share/elasticsearch/data; do
    [[ -d "$ES_ROOT$dflt" ]] && { DATA_PATHS+=("$dflt"); break; }
  done
fi

# Output location: without -o, pick a filesystem other than ES data with at least 50MB free.
# Writing results to the disk under test pollutes the measurements by that much
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
    msg "$(t c.outdir_moved "$cand" "$OUT_BASE")"
    OUT_BASE="$cand"; break
  done
fi

HOST=$(hostname 2>/dev/null || echo unknown)
TS=$(date +%Y%m%d_%H%M%S)
OUT="$OUT_BASE/esdisk_${HOST}_${TS}"
S="$OUT/static"
# Free space at output location (abort under 50MB, to avoid filling a service disk). Checked before creating the directory
AVAIL_KB=$(df -Pk "$OUT_BASE" 2>/dev/null | awk 'NR==2{print $4}')
[[ "${AVAIL_KB:-0}" -lt 51200 ]] && { t c.nospace "$OUT_BASE"; echo; exit 1; }
mkdir -p "$S" || { t c.mkdir_fail "$OUT"; echo; exit 1; }

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
# Paths from -p may contain spaces, so store one per line separately (meta is for compatibility)
: > "$OUT/user_paths"
for p in ${USER_PATHS[@]+"${USER_PATHS[@]}"}; do printf '%s\n' "$p" >> "$OUT/user_paths"; done
# data path candidates found by the collector (so the analyzer can identify the device even if ES is down)
for p in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do printf '%s\n' "$p"; done > "$S/data_paths"
# Block device the data path actually lives on. Reads major:minor from the mountinfo of the ES process (or this shell if none)
# and resolves the device name via /sys/dev/block. The device is found correctly even when paths differ from the host, as in containers
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

msg "$(t c.start "$OUT" "$DUR" "$INT")"

# ── Helpers ──────────────────────────────────────────────────────────────────
save()  { local f="$1"; shift; "$@" > "$S/$f" 2>&1 || true; }
catf()  { [[ -r "$1" ]] && cat "$1" 2>/dev/null; }

# =============================================================================
# 1. Static snapshot (settings, configuration)
# =============================================================================
msg "$(t c.step1)"
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

# sysfs: all block devices (excluding loop/ram/sr)
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
    # SCSI address H:C:T:L (secondary evidence linking a RAID controller logical disk number to an OS device)
    hctl=${real##*/}; [[ "$hctl" =~ ^[0-9]+:[0-9]+:[0-9]+:[0-9]+$ ]] && printf 'HCTL|%s|%s\n' "$n" "$hctl"
    # SCSI disk cache mode (value the kernel got from the device) and FUA support
    for sd in "$d"/device/scsi_disk/*; do
      [[ -d "$sd" ]] || continue
      printf 'ATTR|%s|cache_type|%s\n' "$n" "$(catf "$sd/cache_type")"
      printf 'ATTR|%s|FUA|%s\n' "$n" "$(catf "$sd/FUA")"
    done
    # AER error counters of the PCIe device the disk hangs off (HBA, RAID controller, NVMe) (kernel 4.17+)
    pci=$(echo "$real" | grep -oE '[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]' | tail -1)
    if [[ -n "$pci" && -r "/sys/bus/pci/devices/$pci/aer_dev_fatal" ]]; then
      printf 'AER|%s|%s|cor=%s|nonfatal=%s|fatal=%s\n' "$n" "$pci" \
        "$(awk '/TOTAL_ERR_COR/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_correctable" 2>/dev/null)" \
        "$(awk '/TOTAL_ERR_NONFATAL/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_nonfatal" 2>/dev/null)" \
        "$(awk '/TOTAL_ERR_FATAL/{print $2}' "/sys/bus/pci/devices/$pci/aer_dev_fatal" 2>/dev/null)"
    fi
    # Partitions
    for p in "$d"/"$n"*; do
      [[ -r "$p/start" ]] && printf 'PART|%s|%s|%s\n' "${p##*/}" "$n" "$(cat "$p/start")"
    done
  done
  for h in /sys/class/scsi_host/host*; do
    [[ -r "$h/proc_name" ]] && printf 'HOSTDRV|%s|%s\n' "${h##*/}" "$(cat "$h/proc_name")"
    # Status exposed in sysfs by RAID controller drivers (megaraid: firmware crash, hpsa/smartpqi: firmware version)
    for f in fw_crash_state fw_version firmware_revision; do
      [[ -r "$h/$f" ]] && printf 'HOSTATTR|%s|%s|%s\n' "${h##*/}" "$f" "$(tr -d '\n' < "$h/$f" 2>/dev/null)"
    done
  done
  # Kernel raid_class (mpt2sas/mpt3sas IR volumes etc.): level, state, resync progress
  for r in /sys/class/raid_devices/*; do
    [[ -d "$r" ]] || continue
    printf 'RAIDDEV|%s|level=%s|state=%s|resync=%s|dev=%s\n' "${r##*/}" "$(catf "$r/level")" "$(catf "$r/state")" \
      "$(catf "$r/resync")" "$(ls "$r/device/block" 2>/dev/null | head -1)"
  done
} > "$S/sysfs" 2>/dev/null

# sysctl / kernel memory settings
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

# Platform detection raw data / VMware
# The analyzer makes the verdict. Here we only collect the values it is based on (so re-rendering the bundle on a PC gives the same result)
# systemd-detect-virt prints "none" and exits 1 when there is no virtualization. unknown only when the command is missing
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
  # CPUID hypervisor bit. 1 on a virtual machine. Secondary evidence on old distros without detect-virt
  echo "cpu_hypervisor_flag=$(grep -m1 -cE '^flags.*[[:space:]]hypervisor([[:space:]]|$)' /proc/cpuinfo 2>/dev/null)"
  # Whether running inside a container (secondary evidence when detect-virt is missing)
  [[ -f /.dockerenv ]] && echo "dockerenv=1"
  [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] && echo "kubernetes=1"
  # CPU frequency governor. powersave on bare-metal also delays I/O completion handling (usually absent on VMs)
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

# Storage attachment and device state (for bare-metal/SAN verdicts). All sysfs/proc reads
{
  # NVMe controller: model, firmware, transport (not pcie means NVMe-oF, i.e. remote), temperature, PCIe link
  # Reading the hwmon temperature makes the kernel request the SMART log from the device once. Read-only, once per run
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
  # FC HBA ports (SAN verdict)
  for h in /sys/class/fc_host/host*; do
    [[ -d "$h" ]] || continue
    printf 'FCHOST|%s|port_state=%s|speed=%s\n' "${h##*/}" "$(catf "$h/port_state")" "$(catf "$h/speed")"
  done
  # iSCSI session count
  n_is=$(ls -d /sys/class/iscsi_session/session* 2>/dev/null | wc -l)
  echo "ISCSI|sessions|$n_is"
} > "$S/storage" 2>/dev/null
# Software RAID state (resync, degraded). Values generated in memory
[[ -r /proc/mdstat ]] && cat /proc/mdstat > "$S/mdstat" 2>/dev/null

# ── Hardware status (automatic on bare-metal, off with --no-hw) ─────────────────────
# Virtual machine disks are virtual devices, so SMART/RAID info is meaningless and skipped.
# All commands below are query (show) only. No command changes settings or starts a self-test or rebuild.
# Same level as what smartd and monitoring agents (Prometheus storcli exporter etc.) run periodically.
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
# Some vendor tools leave log files in the working directory (storcli.log, UcliEvt.log).
# Run them inside the output directory (hw_tool_logs) so nothing is left elsewhere on the server, and include them in the bundle without deleting
hw_run() {  # $1=output file, rest=command
  local out="$1"; shift
  mkdir -p "$OUT/hw_tool_logs"
  { echo "#CMD $*"; (cd "$OUT/hw_tool_logs" && timeout 30 "$@" 2>&1 | head -c 4194304); echo; } >> "$out"
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
  # Broadcom MegaRAID 96xx and Dell PERC 12 and later (mpi3mr driver) are managed with storcli2/perccli2.
  # Command syntax is the same (/call, /vall, show ... J), but drives are queried per controller instead of /eall/sall
  if [[ "$DRVS" == *" mpi3mr "* ]]; then
    T=$(find_tool storcli2 perccli2 /opt/MegaRAID/storcli2/storcli2 /opt/MegaRAID/perccli2/perccli2)
    if [[ -n "$T" ]]; then
      for c in "/call show all J" "/call/vall show all J" "/call show patrolread J" "/call show cc J"; do
        # shellcheck disable=SC2086
        hw_run "$S/raid_storcli" "$T" $c
      done
      for n in 0 1 2 3; do
        [[ $n -gt 0 ]] && ! grep -q "Controller = $n" "$S/raid_storcli" && break
        hw_run "$S/raid_storcli" "$T" "/c$n/eall/sall" show all J
        hw_run "$S/raid_storcli" "$T" "/c$n/sall" show all J
      done
      echo "#TOOL storcli2" >> "$S/raid_storcli"
      RAID_TOOLS+="storcli2 "
    elif grep -qiE 'PERC|MR9[0-9]|MegaRAID|RAID' /sys/block/sd*/device/model 2>/dev/null; then
      # mpi3mr is also used for HBAs, so report a missing tool only when RAID logical disks are visible
      echo "#TOOL_ABSENT storcli2/perccli2" >> "$S/raid_storcli"
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
fi
echo "raid_tools=$RAID_TOOLS" >> "$OUT/meta"

# SMART: skip logical disks behind a RAID controller, since the controller tool reports member disk status.
# -n standby: do not wake HDDs in standby. 15s cap per device
SMART_N=0
if [[ $SMART -eq 1 ]]; then
  if command -v smartctl >/dev/null 2>&1; then
    for d in /sys/block/*; do
      n=${d##*/}
      case "$n" in loop*|ram*|sr*|zram*|dm-*|md*|nbd*|rbd*) continue ;; esac
      hdrv=$(cat "$(readlink -f "$d/device" 2>/dev/null | grep -oE '.*/host[0-9]+')/scsi_host/"*/proc_name 2>/dev/null | head -1)
      case "$hdrv" in megaraid_sas|mpi3mr|hpsa|smartpqi|aacraid|arcmsr) continue ;; esac
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
# Per-process cumulative disk I/O (start of measurement). Read again at the end to see from the delta "who besides ES uses the disk".
# /proc/<pid>/io is generated in memory, and grep silently skips processes that have exited
procio() { grep -HE '^(read_bytes|write_bytes):' /proc/[0-9]*/io 2>/dev/null; grep -H . /proc/[0-9]*/comm 2>/dev/null; }
procio > "$S/procio_start"
# AWS EBS (Nitro): exposes time (us) over volume/instance performance limits via an NVMe log page. Query only.
# Read only when nvme-cli (amzn plugin) or ebsnvme from amazon-ec2-utils is present. Skipped otherwise
EBSNV=""
if grep -qs 'Amazon Elastic Block Store' /sys/block/nvme*n*/device/model 2>/dev/null; then
  if command -v nvme >/dev/null 2>&1 && nvme amzn help >/dev/null 2>&1; then EBSNV="nvme"
  elif command -v ebsnvme >/dev/null 2>&1; then EBSNV="ebsnvme"; fi
fi
ebsstats() {
  local d
  [[ -z "$EBSNV" ]] && return 0
  for d in /sys/block/nvme*n*; do
    grep -qs 'Amazon Elastic Block Store' "$d/device/model" || continue
    echo "#DEV ${d##*/}"
    if [[ "$EBSNV" == nvme ]]; then timeout 10 nvme amzn stats -o json "/dev/${d##*/}" 2>/dev/null || timeout 10 nvme amzn stats "/dev/${d##*/}" 2>&1
    else timeout 10 ebsnvme stats -j "/dev/${d##*/}" 2>&1; fi
  done
}
ebsstats > "$S/ebs_stats_start"
save dmsetup_table dmsetup table
# thin pool usage, snapshot fill level (device-mapper status query, read-only)
save dmsetup_status dmsetup status
save udev_rules sh -c "grep -rhsE 'scheduler|read_ahead|queue/|timeout' /etc/udev/rules.d/ /usr/lib/udev/rules.d/ /lib/udev/rules.d/ 2>/dev/null | grep -v '^#' | head -100"

# Kernel log: I/O errors, SCSI resets, hung tasks (last 7 days, as far as available)
# Capped with -n: on a node with recurring faults, kernel messages can reach hundreds of thousands of lines,
# and reading that much journal is itself disk load. Newest first, so the cap is enough.
KLOG_MAX_LINES=${KLOG_MAX_LINES:-20000}
KPAT='I/O error|blk_update_request|Buffer I/O error|critical medium error|Medium Error|rejecting I/O|hung_task|blocked for more than [0-9]+ seconds|remount.*read-only|XFS \(.*\).*(error|shutdown|[Cc]orruption)|EXT4-fs (error|warning)|Sense Key|(scsi|sd [0-9]|pvscsi|mptscsih|mptbase|nvme|ata[0-9]|megaraid|mpt3sas|mpt2sas|hpsa|smartpqi|aacraid|qla2xxx|lpfc).*(\<abort|\<reset\>|timed out|timing out|timeout|failed|FATAL|fault)|controller is down|AER:.*(error|Error)|md/raid.*(Disk failure|not operational)|multipath.*(Failing path|remaining active paths: 0)|megaraid_sas.*/0x[0-9a-fA-F]+/(FATAL|CRIT|DEAD|WARN)|(megaraid|mpt3sas|hpsa|smartpqi|aacraid).*([Bb]attery|BBU|CacheVault|degraded|[Dd]egraded|offline|lockup|[Pp]redictive|[Rr]ebuild)|thin.*(out of data space|switching pool to|read-only mode)|device-mapper: snapshots: Invalidating'
if [[ $NO_KLOG -eq 0 ]]; then
  {
    command -v journalctl >/dev/null 2>&1 && journalctl -k --since "7 days ago" -n "$KLOG_MAX_LINES" -o short-iso --no-pager 2>/dev/null
  } | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io" 2>/dev/null
  # If there is no journal or the result is empty, use dmesg (memory ring buffer, no disk reads)
  [[ -s "$S/klog_io" ]] || { dmesg -T 2>/dev/null | grep -Ei "$KPAT" | grep -viE 'nmi|audit|usb|BogoMIPS|preset' | tail -300 > "$S/klog_io"; }
else
  : > "$S/klog_io"
fi

# Network (ES transport, for reference)
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

# sar history: reads past data sysstat has already recorded (no new collection)
# Read the sa file only once. Previously the same file was read twice with -d and -u,
# but sar_u is unused in the report, so that read was pure waste.
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

# ── ES process (record values found earlier) ───────────────────────────────────
echo "es_instances=$N_ES" >> "$OUT/meta"
echo "es_pid=$ES_PID" >> "$OUT/meta"
if [[ -n "$ES_PID" && -d /proc/$ES_PID ]]; then
  tr '\0' '\n' < /proc/$ES_PID/cmdline > "$S/es_cmdline" 2>/dev/null
  catf /proc/$ES_PID/limits  > "$S/es_limits"
  catf /proc/$ES_PID/status  > "$S/es_status"
  catf /proc/$ES_PID/cgroup  > "$S/es_cgroup"
  ls /proc/$ES_PID/fd 2>/dev/null | wc -l > "$S/es_fdcount"
  # cgroup I/O limits (v2: io.max, v1: blkio.throttle)
  {
    cg=$(awk -F: '$1=="0"{print $3}' /proc/$ES_PID/cgroup 2>/dev/null)
    [[ -n "$cg" && -r "/sys/fs/cgroup$cg/io.max" ]] && sed 's/^/io.max: /' "/sys/fs/cgroup$cg/io.max"
    for f in /sys/fs/cgroup/blkio/system.slice/*elastic*/blkio.throttle.*_device; do
      [[ -s "$f" ]] && sed "s|^|${f##*/}: |" "$f"
    done
  } > "$S/es_cgroup_io" 2>/dev/null
  # elasticsearch.yml: extract only the needed keys (no secrets)
  if [[ -r "$CONF_DIR/elasticsearch.yml" ]]; then
    grep -vE '^\s*#' "$CONF_DIR/elasticsearch.yml" | grep -viE 'password|secret|token|key' \
      | grep -E '^\s*(path|data|bootstrap|node\.roles|node\.attr|index\.store|cluster\.routing|- )' > "$S/es_yml" 2>/dev/null
  fi
fi

# ── ES logs: extract only messages directly tied to the disk ────────────────────────
ES_LOG_DIR=$(grep -oE 'es\.path\.logs=[^ ]+' "$S/es_cmdline" 2>/dev/null | head -1 | cut -d= -f2)
ES_LOG_DIR=${ES_LOG_DIR:-/var/log/elasticsearch}
ESLOGPAT='now throttling indexing|stopped throttling|disk watermark|flood stage|failed to flush|Too many open files|failed to write|translog.*(error|corrupt|recover)|overhead, spent|\[gc\]\[|shard failed|failed to recover|Data too large|timed out after'
# Log reading is the largest disk load this tool generates. What it reads pushes out page cache,
# and on an ES node the evicted space is segment cache, so ES then reads that much more from disk.
# So it is capped: most of the budget goes to the newest log (default 8MB), and the two previous logs get 2MB each.
# The final output is tail -100 anyway, so this range is enough in most cases.
# To look further back, raise ESLOG_TAIL_MB (load grows accordingly).
ESLOG_TAIL_MB=${ESLOG_TAIL_MB:-8}
ESLOG_OLD_MB=${ESLOG_OLD_MB:-2}
ESLOG_MAX_FILES=${ESLOG_MAX_FILES:-3}
ESLOG_BYTES=0
if [[ $NO_ESLOG -eq 0 && -d "$ES_LOG_DIR" ]]; then
  # Sort by newest mtime and give the larger budget to the front (newest). Excludes gc/deprecation/audit/slowlog
  i=0
  while read -r lf; do
    [[ -n "$lf" ]] || continue
    i=$((i+1))
    if [[ $i -eq 1 ]]; then mb=$ESLOG_TAIL_MB; else mb=$ESLOG_OLD_MB; fi
    sz=$(stat -c %s "$lf" 2>/dev/null || echo 0)
    want=$((mb*1048576)); [[ "$sz" -lt "$want" ]] && want=$sz
    ESLOG_BYTES=$((ESLOG_BYTES + want))
    echo "#FILE $lf (tail ${mb}MB)"
    tail -c "$((mb*1048576))" "$lf" 2>/dev/null | grep -Eia "$ESLOGPAT" | tail -100
  done < <(find -H "$ES_LOG_DIR" -maxdepth 1 -name '*.log' -mtime -7 -size -2G -printf '%T@\t%p\n' 2>/dev/null \
            | grep -vE '(gc|deprecation|audit|slowlog|index_search|index_indexing)[^/]*\.log$' \
            | sort -rn | head -"$ESLOG_MAX_FILES" | cut -f2-) > "$S/es_log" 2>/dev/null
fi
echo "read_eslog_bytes=$ESLOG_BYTES" >> "$OUT/meta"

# ── mmap usage of the ES process (check headroom against max_map_count) ───────────────
# Reads the ES process mapping list once. While reading it holds the target process mmap_lock in read mode,
# so it only briefly contends with ES mmap/munmap (segment open/close). Measured: about 17ms for 40k mappings.
# To avoid even this on nodes with very many mappings, skip it with --light or NO_MAPS.
if [[ $NO_MAPS -eq 0 && -n "$ES_PID" && -r /proc/$ES_PID/maps ]]; then
  MAPS_T0=$(date +%s%N)
  wc -l < /proc/$ES_PID/maps > "$S/es_mapcount" 2>/dev/null
  echo "maps_read_ms=$(( ($(date +%s%N) - MAPS_T0) / 1000000 ))" >> "$OUT/meta"
fi

# ── ES API (query only, local node only) ────────────────────────────────────────
cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # escape curl -K values (\ and ")
ES_CALLS="$OUT/es_calls"; : > "$ES_CALLS"
es_get() {  # $1=path $2=outfile  → prints http code
  local cfg="" code=""
  [[ -n "$ES_API_KEY" ]] && cfg="header = \"Authorization: ApiKey $(cfgesc "$ES_API_KEY")\""
  [[ -z "$ES_API_KEY" && -n "$ES_USER" ]] && cfg="user = \"$(cfgesc "${ES_USER}:${ES_PASSWORD}")\""
  code=$(printf '%s\n' "$cfg" | curl -s -k --max-time 10 --connect-timeout 3 -K - \
      -o "$2" -w '%{http_code}' "${ES_URL}/$1" 2>/dev/null || true)
  # Record call count and response size to report ES query cost (subshell, so accumulated in a file)
  printf '%s\t%s\t%s\n' "$code" "$(stat -c %s "$2" 2>/dev/null || echo 0)" "${1%%\?*}" >> "$ES_CALLS"
  printf '%s' "$code"
}
# Find the ports ES actually listens on from the ES process sockets.
# So the address need not be known even when network.host is bound to a specific IP (localhost fails) or http.port is changed.
# /proc/<pid>/net/tcp is in the ES process network namespace, so ES in a container is read correctly too
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
    [[ "$c" == "200" || "$c" == "401" ]] && msg "$(t c.es_found "$ES_URL")"
  fi
  [[ -n "$c" ]] || c=$(es_get "" "$S/es_root.json")
  # If auth is required and no account was given, ask directly when running on a terminal.
  # The password is not left in shell history or the process list
  if [[ "$c" == "401" && -z "$ES_USER" && -z "$ES_API_KEY" && -t 0 && -r /dev/tty ]]; then
    msg "$(t c.auth_ask)"
    read -r -p "  $(t c.user): " ES_USER < /dev/tty
    if [[ -n "$ES_USER" ]]; then
      read -rs -p "  $(t c.password): " ES_PASSWORD < /dev/tty; echo >&2
      c=$(es_get "" "$S/es_root.json")
      [[ "$c" == "401" ]] && msg "$(t c.auth_fail)"
    fi
  fi
  echo "es_url=$ES_URL" >> "$OUT/meta"; echo "es_http=$c" >> "$OUT/meta"
  if [[ "$c" == "200" ]]; then
    ES_OK=1
    es_get "_nodes/_local?filter_path=nodes.*.name,nodes.*.version,nodes.*.roles,nodes.*.process.mlockall,nodes.*.settings.path,nodes.*.jvm.mem,nodes.*.jvm.using_compressed_ordinary_object_pointers,nodes.*.os.allocated_processors" "$S/es_nodeinfo.json" >/dev/null
    es_get "_cluster/settings?include_defaults=true&filter_path=**.cluster.routing.allocation.disk*,**.cluster.routing.allocation.awareness*" "$S/es_cluster_settings.json" >/dev/null
    es_get "_cluster/health?filter_path=status,number_of_nodes,active_shards,relocating_shards,initializing_shards,unassigned_shards" "$S/es_health.json" >/dev/null
  elif [[ "$c" == "401" ]]; then
    msg "$(t c.auth_need)"
  else
    msg "$(t c.es_fail "$c")"
  fi
fi
NODE_STATS_PATH="_nodes/_local/stats/indices,fs,thread_pool,jvm,indexing_pressure?filter_path=nodes.*.timestamp,nodes.*.name,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.store,nodes.*.indices.segments,nodes.*.indices.translog,nodes.*.fs,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.merge,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.jvm.gc,nodes.*.indexing_pressure,nodes.*.indices.shard_stats"
# Index settings tied directly to the disk. Without include_defaults, only "explicitly changed indices" appear in the response
IDX_SETTINGS_PATH="_all/_settings?filter_path=**.index.translog.durability,**.index.translog.sync_interval,**.index.translog.flush_threshold_size,**.index.merge.scheduler.max_thread_count,**.index.store.type,**.index.store.preload,**.index.refresh_interval"
IDX_STATS_PATH="_nodes/_local/stats/indices?level=indices&filter_path=nodes.*.indices.indices.*.indexing.index_total,nodes.*.indices.indices.*.indexing.index_time_in_millis,nodes.*.indices.indices.*.merges.total_time_in_millis,nodes.*.indices.indices.*.merges.total_size_in_bytes,nodes.*.indices.indices.*.refresh.total,nodes.*.indices.indices.*.store.size_in_bytes,nodes.*.indices.indices.*.segments.count,nodes.*.indices.indices.*.search.query_total"
CLUSTER_STATS_PATH="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.thread_pool.merge,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

if [[ $ES_OK -eq 1 ]]; then
  es_get "$NODE_STATS_PATH" "$S/es_stats_start.json" >/dev/null
  # Queries that grow with index count (per-index stats, _all/_settings, _ilm/explain) are dropped automatically at large scale,
  # so the user need not judge cluster size to decide on --no-index-stats. Thresholds are practical ones
  if [[ $NO_IDXSTATS -eq 0 ]]; then
    NODE_SHARDS=$(grep -oE '"total_count": *[0-9]+' "$S/es_stats_start.json" 2>/dev/null | head -1 | tr -dc '0-9')
    CL_SHARDS=$(grep -oE '"active_shards": *[0-9]+' "$S/es_health.json" 2>/dev/null | head -1 | tr -dc '0-9')
    if [[ "${NODE_SHARDS:-0}" -ge 2000 || "${CL_SHARDS:-0}" -ge 20000 ]]; then
      NO_IDXSTATS=1; echo "index_stats_auto_skip=1" >> "$OUT/meta"
      msg "$(t c.idx_skip "${NODE_SHARDS:-?}" "${CL_SHARDS:-?}")"
    fi
  fi
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "$IDX_STATS_PATH" "$S/es_idx_start.json" >/dev/null
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "$IDX_SETTINGS_PATH" "$S/es_idx_settings.json" >/dev/null
  # ── Whole cluster: first snapshot in the same window as local measurement ────────────────
  if [[ $NO_CLUSTER -eq 0 ]]; then
    C="$S/cluster"; mkdir -p "$C"
    CC=$(es_get "$CLUSTER_STATS_PATH" "$C/node_stats_1.json")
    if [[ "$CC" == "200" ]]; then
      echo "cluster=ok" >> "$OUT/meta"
    else
      echo "cluster=failed_$CC" >> "$OUT/meta"
      msg "$(t c.cluster_fail "$CC")"
      msg "$(t c.cluster_perm)"
      NO_CLUSTER=1
    fi
  fi
fi

# =============================================================================
# 2. Sampling loop: one awk run per iteration (1 fork)
# =============================================================================
msg "$(t c.step2 "$DUR")"
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
  es_get "_cluster/settings?include_defaults=true&filter_path=**.disk.watermark*,**.disk.threshold*,**.indices.recovery*,**.node_concurrent*,**.cluster_concurrent_rebalance*,**.allocation.awareness*" "$C/cluster_settings.json" >/dev/null
  es_get "_nodes?filter_path=nodes.*.name,nodes.*.roles,nodes.*.attributes,nodes.*.host,nodes.*.ip,nodes.*.settings.path" "$C/nodes_info.json" >/dev/null
  [[ $NO_IDXSTATS -eq 0 ]] && es_get "_all/_ilm/explain?only_managed=true&filter_path=indices.*.phase,indices.*.policy,indices.*.action" "$C/ilm_explain.json" >/dev/null
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
# 3. Final snapshot
# =============================================================================
msg "$(t c.step3)"
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
ebsstats > "$S/ebs_stats_end"

# ── Collector own resource usage ────────────────────────────────────────────────
# bash builtin times: cumulative user/sys CPU of self / child processes
times > "$OUT/self_overhead" 2>/dev/null
# Max RSS (incl. children). The shell cannot see ru_maxrss, so recorded only if /usr/bin/time exists
du -sk "$OUT" | awk '{print "output_kb="$1}' >> "$OUT/meta"
# ES query cost
awk -F'\t' '{n++; b+=$2} END{printf "es_api_calls=%d\nes_api_bytes=%d\n", n+0, b+0}' "$ES_CALLS" >> "$OUT/meta" 2>/dev/null
{
  echo "light_mode=$(( NO_ESLOG & NO_KLOG & NO_SAR & NO_MAPS ))"
  echo "skipped=$( [[ $NO_ESLOG -eq 1 ]] && printf 'eslog '; [[ $NO_KLOG -eq 1 ]] && printf 'klog '; \
                   [[ $NO_SAR -eq 1 ]] && printf 'sar '; [[ $NO_MAPS -eq 1 ]] && printf 'maps '; \
                   [[ $NO_IDXSTATS -eq 1 ]] && printf 'index-stats%s ' "$(grep -q '^index_stats_auto_skip=1' "$OUT/meta" && echo '(auto)')" )"
} >> "$OUT/meta"

# Warn if results are being written to the same filesystem as ES data.
# That adds writes to the disk under test and pollutes the measurements accordingly
OUT_DEV=$(df -Pk "$OUT_BASE" 2>/dev/null | awk 'NR==2{print $1}')
for dp in ${DATA_PATHS[@]+"${DATA_PATHS[@]}"}; do
  [[ -d "$dp" ]] || continue
  if [[ "$(df -Pk "$dp" 2>/dev/null | awk 'NR==2{print $1}')" == "$OUT_DEV" ]]; then
    msg "$(t c.samefs "$OUT_BASE" "$dp")"
    msg "$(t c.samefs2)"
    echo "out_on_data_fs=1" >> "$OUT/meta"
    break
  fi
done

# =============================================================================
# 4. Bundle + HTML
# =============================================================================
msg "$(t c.step4)"
# Summary verdict built with shell (awk) only. Results are visible right away even on servers without Python.
# Shown on screen in the chosen language; the bundle keeps both languages (summary.ko.txt, summary.en.txt)
echo >&2
if [[ -f "$HERE/es_disk_summary.sh" ]]; then
  bash "$HERE/es_disk_summary.sh" "$OUT" --lang "$LNG" >&2 || true
  for l in ko en; do
    [[ "$l" == "$LNG" ]] || bash "$HERE/es_disk_summary.sh" "$OUT" --lang "$l" >/dev/null 2>&1 || true
  done
fi
echo >&2
# HTML is built in both languages before packing the bundle (es_disk_report.ko.html, es_disk_report.en.html)
PY=""
for c in python3 /usr/libexec/platform-python python; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [[ $NO_RENDER -eq 1 ]]; then
  :
elif [[ -n "$PY" && -f "$HERE/es_disk_render.py" ]]; then
  "$PY" "$HERE/es_disk_render.py" "$OUT" --lang both -o "$OUT/es_disk_report.html" >/dev/null && \
    msg "$(t c.html "$OUT/es_disk_report.$LNG.html")"
else
  msg "$(t c.nopy)"
  msg "$(t c.nopy2 "$(basename "$OUT").tar.gz")"
fi
tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null
msg "$(t c.summary "$OUT/summary.$LNG.txt")"
msg "$(t c.done "$OUT.tar.gz")"
