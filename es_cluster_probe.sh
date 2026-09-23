#!/usr/bin/env bash
# =============================================================================
# es_cluster_probe.sh  (v0.9.0)
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
#   -g SEC      두 스냅샷 사이 간격 (기본 60초, 길수록 안정적)
#   -o DIR      결과 위치 (기본 /tmp)
#   --deep      인덱스별 용량까지 수집 (인덱스가 많으면 응답이 커짐)
#   --insecure  자체 서명 인증서 허용 (기본 활성)
#
# 결과: <출력>/escluster_<ts>/  →  es_disk_render.py <노드번들> --cluster <이 디렉터리>
#       노드 번들 없이 클러스터만 볼 때:  es_disk_render.py --cluster-only <이 디렉터리>
# =============================================================================
set -u
umask 077
export LC_ALL=C

ES_URL="http://localhost:9200"; ES_USER=""; GAP=60; OUT_BASE="/tmp"; DEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --es-url) ES_URL="$2"; shift 2 ;;
    --es-user) ES_USER="$2"; shift 2 ;;
    -g) GAP="$2"; shift 2 ;;
    -o) OUT_BASE="$2"; shift 2 ;;
    --deep) DEEP=1; shift ;;
    --insecure) shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done
ES_PASSWORD="${ES_PASSWORD:-}"; ES_API_KEY="${ES_API_KEY:-}"
command -v curl >/dev/null 2>&1 || { echo "curl이 필요합니다"; exit 1; }

TS=$(date +%Y%m%d_%H%M%S); OUT="$OUT_BASE/escluster_$TS"; mkdir -p "$OUT" || exit 1
msg() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

cfgesc() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }   # curl -K 값 이스케이프 (\ 와 ")
es_get() {  # $1=path $2=outfile
  local cfg=""
  [[ -n "$ES_API_KEY" ]] && cfg="header = \"Authorization: ApiKey $(cfgesc "$ES_API_KEY")\""
  [[ -z "$ES_API_KEY" && -n "$ES_USER" ]] && cfg="user = \"$(cfgesc "${ES_USER}:${ES_PASSWORD}")\""
  printf '%s\n' "$cfg" | curl -s -k --max-time 30 --connect-timeout 5 -K - \
      -o "$2" -w '%{http_code}' "${ES_URL}/$1" 2>/dev/null || true
}

CODE=$(es_get "" "$OUT/root.json")
if [[ "$CODE" != "200" ]]; then
  echo "ES 접속 실패 (http=$CODE). --es-url / 인증 정보를 확인하세요."; rm -rf "$OUT"; exit 2
fi
echo "es_url=$ES_URL"$'\n'"gap=$GAP"$'\n'"start_wall=$(date '+%Y-%m-%d %H:%M:%S %z')" > "$OUT/meta"

# 노드별 디스크·인덱싱 지표 (두 시점의 차분용)
NODE_STATS="_nodes/stats/fs,indices,thread_pool,jvm,os?filter_path=nodes.*.name,nodes.*.roles,nodes.*.host,nodes.*.timestamp,nodes.*.fs.total,nodes.*.fs.io_stats,nodes.*.indices.store,nodes.*.indices.indexing,nodes.*.indices.search,nodes.*.indices.merges,nodes.*.indices.refresh,nodes.*.indices.flush,nodes.*.indices.segments.count,nodes.*.indices.translog,nodes.*.thread_pool.write,nodes.*.thread_pool.search,nodes.*.thread_pool.flush,nodes.*.jvm.mem.heap_used_percent,nodes.*.os.cpu.percent"

msg "1차 스냅샷"
es_get "$NODE_STATS" "$OUT/node_stats_1.json" >/dev/null

# 변하지 않는 정보는 대기 시간 동안 수집
msg "구성 정보 수집 (대기 ${GAP}초 동안)"
es_get "_cat/nodes?format=json&h=name,node.role,master,disk.used_percent,disk.avail,disk.total,heap.percent,ram.percent,cpu,load_1m,version" "$OUT/cat_nodes.json" >/dev/null
es_get "_cat/allocation?format=json&bytes=b&h=node,shards,disk.indices,disk.used,disk.avail,disk.total,disk.percent" "$OUT/cat_allocation.json" >/dev/null
es_get "_cluster/health" "$OUT/health.json" >/dev/null
es_get "_cat/recovery?format=json&active_only=true&h=index,shard,type,stage,source_node,target_node,bytes_total,bytes_percent,time" "$OUT/cat_recovery.json" >/dev/null
es_get "_cat/pending_tasks?format=json" "$OUT/pending_tasks.json" >/dev/null
es_get "_snapshot/_status" "$OUT/snapshot_status.json" >/dev/null
es_get "_cluster/settings?include_defaults=true&flat_settings=true&filter_path=**.disk.watermark*,**.disk.threshold*,**.indices.recovery*,**.node_concurrent*,**.cluster_concurrent_rebalance*,**.allocation.awareness*,**.max_shards_per_node*" "$OUT/cluster_settings.json" >/dev/null
es_get "_nodes?filter_path=nodes.*.name,nodes.*.roles,nodes.*.attributes,nodes.*.settings.path,nodes.*.process.mlockall,nodes.*.jvm.mem.heap_max_in_bytes,nodes.*.os.available_processors,nodes.*.os.name,nodes.*.host,nodes.*.ip" "$OUT/nodes_info.json" >/dev/null
[[ $DEEP -eq 1 ]] && es_get "_cat/indices?format=json&bytes=b&h=index,health,pri,rep,docs.count,store.size,pri.store.size&s=store.size:desc" "$OUT/cat_indices.json" >/dev/null

sleep "$GAP"
msg "2차 스냅샷"
es_get "$NODE_STATS" "$OUT/node_stats_2.json" >/dev/null
echo "end_wall=$(date '+%Y-%m-%d %H:%M:%S %z')" >> "$OUT/meta"

tar -C "$OUT_BASE" -czf "$OUT.tar.gz" "$(basename "$OUT")" 2>/dev/null
msg "완료: $OUT  (번들 $OUT.tar.gz)"
msg "리포트에 반영: python3 es_disk_render.py <노드 번들> --cluster $OUT"
