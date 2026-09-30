#!/usr/bin/env bash
# =============================================================================
# es_cluster_probe.sh  (v0.11.0)
# Queries an Elasticsearch cluster "from the disk point of view". (READ-ONLY)
#
#  - Calls only ES read APIs (GET). No settings changes, no index writes.
#  - No SSH to nodes. Runs from anywhere that can reach ES (laptops included).
#  - Takes two snapshots and computes the delta → disk usage can be compared across nodes.
#  - Required privilege: cluster monitor (monitoring_user level). No admin account needed.
#
# Usage:
#   ES_PASSWORD='***' ./es_cluster_probe.sh --es-url https://es:9200 --es-user elastic
#   ES_API_KEY='...'  ./es_cluster_probe.sh --es-url https://es:9200 -g 120
#
#   --es-url URL  ES address (default: tries localhost:9200 with http, then https)
#   --es-user U   ES user. Password in env var ES_PASSWORD, API Key in ES_API_KEY
#                 If neither is set and ES requires auth, prompts on the terminal
#   -g SEC        Interval between the two snapshots (default 60s, longer is more stable. Min 10)
#   -o DIR        Output location (default /tmp)
#   --deep        Also collect per-index size (response grows with many indices)
#   --insecure    Allow self-signed certificates (default)
#   --strict-tls  Verify certificates. Use with --cacert, or when the CA is in the OS trust store
#   --cacert F    CA certificate file (implies --strict-tls)
#   --lang L      Screen language: ko | en (default from the locale). HTML is built in both
#
# ⚠ Default is curl -k (no certificate verification). If internal security policy requires verification,
#   use --strict-tls or --cacert.
#
# Output: <output>/escluster_<ts>/ and es_cluster_report.ko.html / .en.html inside it (auto-built if python3 is present)
#       To view together with a node bundle:  es_disk_render.py <node bundle> --cluster <this directory>
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
_catload "$HERE/i18n/$LNG.txt" p.; _catload "$HERE/i18n/ko.txt" p.
export LC_ALL=C

ES_URL=""; ES_USER=""; GAP=60; OUT_BASE="/tmp"; DEEP=0
STRICT=0; CACERT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --es-url) ES_URL="$2"; shift 2 ;;
    --es-user) ES_USER="$2"; shift 2 ;;
    -g) GAP="$2"; shift 2 ;;
    -o) OUT_BASE="$2"; shift 2 ;;
    --deep) DEEP=1; shift ;;
    --insecure) STRICT=0; shift ;;
    --strict-tls) STRICT=1; shift ;;
    --cacert) CACERT="$2"; STRICT=1; shift 2 ;;
    --lang) shift 2 ;;
    --lang=*) shift ;;
    -h|--help) t p.help; echo; exit 0 ;;
    *) t p.badopt "$1"; echo; exit 1 ;;
  esac
done
ES_PASSWORD="${ES_PASSWORD:-}"; ES_API_KEY="${ES_API_KEY:-}"
ES_URL="${ES_URL%/}"
[[ "$GAP" =~ ^[0-9]+$ && "$GAP" -ge 10 ]] || { t p.bad_g; echo; exit 1; }
command -v curl >/dev/null 2>&1 || { t p.nocurl; echo; exit 1; }
[[ -z "$CACERT" || -r "$CACERT" ]] || { t p.cacert "$CACERT"; echo; exit 1; }
TLS=(-k); [[ $STRICT -eq 1 ]] && TLS=()
[[ -n "$CACERT" ]] && TLS+=(--cacert "$CACERT")

# Create the output directory only after ES connectivity is confirmed (on failure nothing is left behind and nothing needs deleting)
TS=$(date +%Y%m%d_%H%M%S); OUT="$OUT_BASE/escluster_$TS"
msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # escape curl -K values (\ and ")
es_get() {  # $1=path $2=outfile
  local cfg=""
  [[ -n "$ES_API_KEY" ]] && cfg="header = \"Authorization: ApiKey $(cfgesc "$ES_API_KEY")\""
  [[ -z "$ES_API_KEY" && -n "$ES_USER" ]] && cfg="user = \"$(cfgesc "${ES_USER}:${ES_PASSWORD}")\""
  printf '%s\n' "$cfg" | curl -s "${TLS[@]}" --max-time 30 --connect-timeout 5 -K - \
      -o "$2" -w '%{http_code}' "${ES_URL}/$1" 2>/dev/null || true
}

if [[ -z "$ES_URL" ]]; then
  for ES_URL in http://localhost:9200 https://localhost:9200; do
    CODE=$(es_get "" /dev/null)
    [[ "$CODE" == "200" || "$CODE" == "401" ]] && break
  done
else
  CODE=$(es_get "" /dev/null)
fi
if [[ "$CODE" == "401" && -z "$ES_USER" && -z "$ES_API_KEY" && -t 0 && -r /dev/tty ]]; then
  msg "$(t p.auth_ask "$ES_URL")"
  read -r -p "  $(t p.user): " ES_USER < /dev/tty
  read -rs -p "  $(t p.password): " ES_PASSWORD < /dev/tty; echo >&2
  CODE=$(es_get "" /dev/null)
fi
if [[ "$CODE" != "200" ]]; then
  if [[ "$CODE" == "401" ]]; then
    t p.auth_fail "$ES_URL"; echo
  else
    t p.es_fail "$CODE" "$ES_URL"; echo
  fi
  [[ $STRICT -eq 1 ]] && t p.strict; echo
  exit 2
fi
mkdir -p "$OUT" || exit 1
es_get "" "$OUT/root.json" >/dev/null
echo "es_url=$ES_URL"$'\n'"gap=$GAP"$'\n'"start_wall=$(date '+%Y-%m-%d %H:%M:%S %z')" > "$OUT/meta"

# Per-node disk and indexing metrics (for the delta between two points)
NODE_STATS="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.thread_pool.merge,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

msg "$(t p.snap1)"
es_get "$NODE_STATS" "$OUT/node_stats_1.json" >/dev/null

# Collect static information during the wait
msg "$(t p.config "$GAP")"
es_get "_cat/nodes?format=json&h=name,node.role,master,disk.used_percent,disk.avail,disk.total,heap.percent,ram.percent,cpu,load_1m,version" "$OUT/cat_nodes.json" >/dev/null
es_get "_cat/allocation?format=json&bytes=b&h=node,shards,disk.indices,disk.used,disk.avail,disk.total,disk.percent" "$OUT/cat_allocation.json" >/dev/null
es_get "_cluster/health" "$OUT/health.json" >/dev/null
es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$OUT/cat_recovery.json" >/dev/null
es_get "_cat/pending_tasks?format=json" "$OUT/pending_tasks.json" >/dev/null
es_get "_snapshot/_status" "$OUT/snapshot_status.json" >/dev/null
es_get "_cluster/settings?include_defaults=true&filter_path=**.disk.watermark*,**.disk.threshold*,**.indices.recovery*,**.node_concurrent*,**.cluster_concurrent_rebalance*,**.allocation.awareness*,**.max_shards_per_node*" "$OUT/cluster_settings.json" >/dev/null
es_get "_nodes?filter_path=nodes.*.name,nodes.*.roles,nodes.*.attributes,nodes.*.settings.path,nodes.*.process.mlockall,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.os.available_processors,nodes.*.os.name,nodes.*.host,nodes.*.ip" "$OUT/nodes_info.json" >/dev/null
# ILM phase: used to split hot/warm in the per-index distribution report. Skipped without read_ilm privilege
es_get "_all/_ilm/explain?only_managed=true&filter_path=indices.*.phase,indices.*.policy,indices.*.action" "$OUT/ilm_explain.json" >/dev/null
[[ $DEEP -eq 1 ]] && es_get "_cat/indices?format=json&bytes=b&h=index,health,pri,rep,docs.count,store.size,pri.store.size&s=store.size:desc" "$OUT/cat_indices.json" >/dev/null

sleep "$GAP"
msg "$(t p.snap2)"
es_get "$NODE_STATS" "$OUT/node_stats_2.json" >/dev/null
echo "end_wall=$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$OUT/meta"

PY=""
for c in python3 /usr/libexec/platform-python; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' 2>/dev/null && { PY="$c"; break; }
done
# HTML is built in both languages before packing the bundle (es_cluster_report.ko.html, es_cluster_report.en.html)
if [[ -n "$PY" && -f "$HERE/es_disk_render.py" ]]; then
  "$PY" "$HERE/es_disk_render.py" --cluster-only "$OUT" --lang both -o "$OUT/es_cluster_report.html" >/dev/null && \
    msg "$(t p.html "$OUT/es_cluster_report.$LNG.html")"
else
  msg "$(t p.nopy "$(basename "$OUT").tar.gz")"
fi
tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null
msg "$(t p.done "$OUT" "$OUT.tar.gz")"
msg "$(t p.merge "$OUT")"
