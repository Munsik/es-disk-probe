#!/usr/bin/env bash
# =============================================================================
# es_cluster_probe.sh  (v0.10.0)
# Elasticsearch 클러스터를 "디스크 관점"에서 조회합니다. (READ-ONLY)
#
#  - ES 조회 API(GET)만 호출합니다. 설정 변경, 인덱스 쓰기 없음.
#  - 노드에 SSH 하지 않습니다. ES에 접속 가능한 어디서든(노트북 포함) 실행 가능.
#  - 두 시점을 찍어 차분을 냅니다 → 노드별 디스크 사용량을 비교할 수 있습니다.
#  - 필요한 권한: cluster monitor (monitoring_user 수준). 관리자 계정 불필요.
#
# 사용:
#   ES_PASSWORD='***' ./es_cluster_probe.sh --es-url https://es:9200 --es-user elastic
#   ES_API_KEY='...'  ./es_cluster_probe.sh --es-url https://es:9200 -g 120
#
#   --es-url URL  ES 주소 (기본: localhost:9200 을 http, https 순서로 시도)
#   --es-user U   ES 사용자. 비밀번호는 환경변수 ES_PASSWORD, API Key 는 ES_API_KEY
#                 둘 다 없고 ES 가 인증을 요구하면 터미널에서 물어봅니다
#   -g SEC        두 스냅샷 사이 간격 (기본 60초, 길수록 안정적. 최소 10)
#   -o DIR        결과 위치 (기본 /tmp)
#   --deep        인덱스별 용량까지 수집 (인덱스가 많으면 응답이 커짐)
#   --insecure    자체 서명 인증서 허용 (기본값)
#   --strict-tls  인증서를 검증합니다. --cacert 와 함께 쓰거나 CA가 OS 신뢰 저장소에 있을 때
#   --cacert F    CA 인증서 파일 지정 (지정하면 --strict-tls 자동 적용)
#
# ⚠ 기본 동작은 curl -k (인증서 검증 생략)입니다. 사내 보안 정책상 검증이 필요하면
#   --strict-tls 또는 --cacert 를 쓰세요.
#
# 결과: <출력>/escluster_<ts>/ 와 그 안의 es_cluster_report.html (python3 가 있으면 자동 생성)
#       노드 번들과 합쳐 보려면:  es_disk_render.py <노드번들> --cluster <이 디렉터리>
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
t() {  # t key [values...]  → {1}, {2} ... 자리에 값
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

# 결과 디렉터리는 ES 접속이 확인된 뒤에 만든다 (실패하면 아무것도 남기지 않고, 지울 것도 없게)
TS=$(date +%Y%m%d_%H%M%S); OUT="$OUT_BASE/escluster_$TS"
msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # curl -K 값 이스케이프 (\ 와 ")
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

# 노드별 디스크·인덱싱 지표 (두 시점의 차분용)
NODE_STATS="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.thread_pool.merge,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

msg "$(t p.snap1)"
es_get "$NODE_STATS" "$OUT/node_stats_1.json" >/dev/null

# 변하지 않는 정보는 대기 시간 동안 수집
msg "$(t p.config "$GAP")"
es_get "_cat/nodes?format=json&h=name,node.role,master,disk.used_percent,disk.avail,disk.total,heap.percent,ram.percent,cpu,load_1m,version" "$OUT/cat_nodes.json" >/dev/null
es_get "_cat/allocation?format=json&bytes=b&h=node,shards,disk.indices,disk.used,disk.avail,disk.total,disk.percent" "$OUT/cat_allocation.json" >/dev/null
es_get "_cluster/health" "$OUT/health.json" >/dev/null
es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$OUT/cat_recovery.json" >/dev/null
es_get "_cat/pending_tasks?format=json" "$OUT/pending_tasks.json" >/dev/null
es_get "_snapshot/_status" "$OUT/snapshot_status.json" >/dev/null
es_get "_cluster/settings?include_defaults=true&filter_path=**.disk.watermark*,**.disk.threshold*,**.indices.recovery*,**.node_concurrent*,**.cluster_concurrent_rebalance*,**.allocation.awareness*,**.max_shards_per_node*" "$OUT/cluster_settings.json" >/dev/null
es_get "_nodes?filter_path=nodes.*.name,nodes.*.roles,nodes.*.attributes,nodes.*.settings.path,nodes.*.process.mlockall,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.os.available_processors,nodes.*.os.name,nodes.*.host,nodes.*.ip" "$OUT/nodes_info.json" >/dev/null
# ILM phase: 인덱스별 분포 리포트에서 hot/warm 구분에 사용. read_ilm 권한이 없으면 건너뜀
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
# HTML 은 번들을 묶기 전에 두 언어로 만든다 (es_cluster_report.ko.html, es_cluster_report.en.html)
if [[ -n "$PY" && -f "$HERE/es_disk_render.py" ]]; then
  "$PY" "$HERE/es_disk_render.py" --cluster-only "$OUT" --lang both -o "$OUT/es_cluster_report.html" >/dev/null && \
    msg "$(t p.html "$OUT/es_cluster_report.$LNG.html")"
else
  msg "$(t p.nopy "$(basename "$OUT").tar.gz")"
fi
tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null
msg "$(t p.done "$OUT" "$OUT.tar.gz")"
msg "$(t p.merge "$OUT")"
