#!/usr/bin/env bash
# =============================================================================
# es_disk_bench.sh  (v0.10.0, 선택 사항)
# ES data 디스크의 "최대 능력"을 측정합니다. → 리포트의 여유율 계산용
# fio 가 있으면 fio 로 5가지(무작위 읽기, 순차 쓰기·읽기, 혼합, 동기 쓰기)를 재고,
# 없으면 OS 기본 도구 dd 로 3가지(순차 쓰기·읽기, 동기 쓰기 지연)만 잽니다. 무작위 I/O 는 dd 로 잴 수 없습니다.
#
# ⚠ 이 스크립트는 디스크에 실제 부하를 겁니다. 반드시 아래 조건에서만 실행하세요.
#   - 서비스 투입 전, 또는 해당 노드의 ES를 내린 점검 시간
#   - 공유 스토리지(vSAN, SAN 어레이, 클라우드 볼륨)는 벤치 부하가 같은 스토리지를 쓰는
#     다른 VM·서버에도 영향을 줄 수 있습니다. VMware·스토리지 관리자와 시간을 맞추세요.
#     bare-metal 로컬 디스크는 이 노드에만 영향이 갑니다.
#
# 안전장치:
#   - ES 프로세스가 떠 있으면 실행 거부 (--force-with-es 로만 우회)
#   - 테스트 파일 생성 후 디스크 사용률이 80%를 넘으면 실행 거부
#   - 테스트 전용 하위 디렉터리만 사용하고 종료 시(중단 포함) 삭제
#   - drop_caches, sync 강제, 원시 장치 쓰기는 하지 않음 (direct I/O 파일 테스트만)
#
# 사용: sudo ./es_disk_bench.sh [-t /var/lib/elasticsearch] [-s 4G] [-r 30] [-o /tmp]
#
#   -t PATH           측정할 ES data 경로. 안 주면 elasticsearch.yml 의 path.data 에서 찾음
#                     (ES_PATH_CONF 또는 /etc/elasticsearch). 경로가 여러 개면 -t 로 골라 주세요
#   -s SIZE           테스트 파일 크기 (기본 4G. 4G / 512M / 1048576 형식)
#   -r SEC            테스트당 실행 시간 (기본 30, 최소 5)
#   -o DIR            결과 저장 위치 (기본 /tmp)
#   --force-with-es   ES 가 떠 있어도 실행 (영향을 감수할 때만)
# 결과: <출력>/esbench_<host>_<ts>/*.json
#       같은 서버에서 es_disk_collect.sh 를 실행하면 최근 결과를 자동으로 찾아 리포트에 넣습니다
#
# 해석 주의: 테스트 파일이 vSAN 캐시 계층이나 RAID 컨트롤러·어레이 캐시에 들어가면
#            결과가 실제보다 좋게 나옵니다.
#            결과는 "상한" 으로 보고, 여유율은 낙관적인 값으로 해석하세요.
# =============================================================================
set -u
TARGET=""; SIZE="4G"; RT=30; OUT_BASE="/tmp"; FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t) TARGET="$2"; shift 2 ;;
    -s) SIZE="$2"; shift 2 ;;
    -r) RT="$2"; shift 2 ;;
    -o) OUT_BASE="$2"; shift 2 ;;
    --force-with-es) FORCE=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/{print;next} NR>1{exit}' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done
# -t 가 없으면 elasticsearch.yml 에서 path.data 를 찾는다 (es_disk_collect.sh 와 같은 해석)
if [[ -z "$TARGET" ]]; then
  YML="${ES_PATH_CONF:-/etc/elasticsearch}/elasticsearch.yml"
  mapfile -t FOUND < <(awk '
    /^[[:space:]]*#/ { next }
    function emit(v,   n, i, a) { gsub(/[\[\]"\047]/, "", v); n = split(v, a, ","); for (i = 1; i <= n; i++) { gsub(/^[ \t]+|[ \t]+$/, "", a[i]); if (a[i] != "") print a[i] } }
    /^path\.data[[:space:]]*:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); if (v != "") emit(v); else inlist = 1; next }
    /^path[[:space:]]*:[[:space:]]*$/ { inpath = 1; next }
    inpath && /^[[:space:]]+data[[:space:]]*:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); if (v != "") emit(v); else inlist = 1; next }
    inlist && /^[[:space:]]*-[[:space:]]*/ { v = $0; sub(/^[[:space:]]*-[[:space:]]*/, "", v); emit(v); next }
    /^[^[:space:]]/ { inpath = 0; inlist = 0 }
    /^[[:space:]]+[a-z]/ && !/^[[:space:]]+data/ { inlist = 0 }
  ' "$YML" 2>/dev/null)
  [[ ${#FOUND[@]} -eq 0 && -d /var/lib/elasticsearch ]] && FOUND=(/var/lib/elasticsearch)
  if [[ ${#FOUND[@]} -eq 1 ]]; then
    TARGET="${FOUND[0]}"; echo "ES data 경로 자동 탐지: $TARGET"
  elif [[ ${#FOUND[@]} -gt 1 ]]; then
    echo "ES data 경로가 여러 개입니다. -t 로 하나를 고르세요: ${FOUND[*]}"; exit 1
  fi
fi
[[ -n "$TARGET" && -d "$TARGET" ]] || { echo "ES data 경로를 찾지 못했습니다. -t 로 지정하세요 (존재하는 디렉터리)"; exit 1; }
ENGINE=fio
if ! command -v fio >/dev/null 2>&1; then
  ENGINE=dd
  echo "fio 가 없어 dd 로 측정합니다 (순차 쓰기·읽기, 동기 쓰기 지연). 무작위 I/O 까지 재려면 fio 를 설치하세요."
fi

if pgrep -f 'org\.elasticsearch\.bootstrap\.Elasticsearch' >/dev/null 2>&1 && [[ $FORCE -eq 0 ]]; then
  echo "ES가 실행 중입니다. 벤치 부하가 서비스 I/O와 경합하므로 실행하지 않습니다."
  echo "점검 시간에 ES를 내린 뒤 실행하거나, 영향을 감수할 때만 --force-with-es 를 쓰세요."
  exit 2
fi

[[ "$RT" =~ ^[0-9]+$ && "$RT" -ge 5 ]] || { echo "-r 은 5 이상 정수(초)"; exit 1; }
# 크기 표기 검증: 숫자 + 선택적 G/M/K (fio 가 받는 형식). GB·4g 같은 표기는 여기서 걸러낸다
[[ "$SIZE" =~ ^[0-9]+[gGmMkK]?$ ]] || { echo "-s 는 4G / 512M / 1048576 같은 형식으로 지정하세요 (현재: $SIZE)"; exit 1; }
to_kb() {
  local v=${1^^}
  case "$v" in
    *G) echo $(( ${v%G} * 1048576 )) ;;
    *M) echo $(( ${v%M} * 1024 )) ;;
    *K) echo $(( ${v%K} )) ;;
    *)  echo $(( v / 1024 )) ;;
  esac
}
NEED_KB=$(to_kb "$SIZE")
read -r TOT_KB USED_KB AVL_KB <<< "$(df -Pk "$TARGET" | awk 'NR==2{print $2,$3,$4}')"
[[ "${TOT_KB:-0}" =~ ^[0-9]+$ && "${TOT_KB:-0}" -gt 0 ]] || { echo "df 로 $TARGET 의 용량을 읽지 못했습니다"; exit 1; }
[[ "$NEED_KB" -gt 0 ]] || { echo "-s 값이 너무 작습니다 (최소 1MB)"; exit 1; }
AFTER_PCT=$(( (USED_KB + NEED_KB) * 100 / TOT_KB ))
if [[ $NEED_KB -ge $AVL_KB || $AFTER_PCT -gt 80 ]]; then
  echo "공간 부족: 테스트 후 사용률 ${AFTER_PCT}% 예상 (기준 80%). -s 로 크기를 줄이세요."; exit 3
fi

HOST=$(hostname); TS=$(date +%Y%m%d_%H%M%S)
OUT="$OUT_BASE/esbench_${HOST}_${TS}"; mkdir -p "$OUT"
WORK="$TARGET/.es_disk_bench_$$"; mkdir -p "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

echo "대상 $TARGET · 파일 $SIZE · 테스트당 ${RT}s · 예상 소요 $(( (RT + 5) * 5 / 60 + 1 ))분"
echo "결과 → $OUT"
COMMON=(--directory="$WORK" --filename=bench.dat --size="$SIZE" --direct=1 --time_based
        --runtime="$RT" --ramp_time=5 --group_reporting --output-format=json)

run() { local name="$1"; shift; echo "  ▸ $name"; fio --name="$name" "${COMMON[@]}" "$@" > "$OUT/$name.json" 2>"$OUT/$name.err" || echo "    실패. $OUT/$name.err 확인"; }

# ── fio 가 없을 때: dd (coreutils) 로 측정 ─────────────────────────────────
# 결과는 분석기가 읽는 fio JSON 과 같은 키로 남긴다 (jobs[0].read/write.bw 는 KiB/s)
if [[ $ENGINE == dd ]]; then
  MB=$(( NEED_KB / 1024 )); [[ $MB -lt 64 ]] && MB=64
  ddrun() {  # $1=이름 $2=측정 대상 방향(read|write) 나머지=dd 인자. 경과 시간은 date +%s%N 로 직접 잰다
    local name="$1" dir="$2"; shift 2
    echo "  ▸ $name (dd)"
    local t0 t1 bytes
    t0=$(date +%s%N)
    timeout $(( RT * 20 + 60 )) dd "$@" 2>"$OUT/$name.err"
    t1=$(date +%s%N)
    bytes=$(awk '/bytes/ {print $1; exit}' "$OUT/$name.err")
    awk -v b="${bytes:-0}" -v ns="$(( t1 - t0 ))" -v dir="$dir" -v name="$name" 'BEGIN {
      s = ns / 1e9; kibs = (s > 0) ? b / 1024 / s : 0
      printf "{\"engine\": \"dd\", \"jobs\": [{\"jobname\": \"%s\", \"%s\": {\"bw\": %.1f, \"iops\": %.1f}}], \"elapsed_s\": %.3f}\n", name, dir, kibs, 0, s
    }' > "$OUT/$name.json"
  }
  ddrun seqwrite_1m write if=/dev/zero of="$WORK/bench.dat" bs=1M count="$MB" oflag=direct conv=fsync
  ddrun seqread_1m  read  if="$WORK/bench.dat" of=/dev/null bs=1M iflag=direct
  # translog 처럼 4KiB 를 쓸 때마다 디스크 기록 완료를 기다린다 (oflag=dsync). 한 건당 평균 지연을 기록
  N_SYNC=2000
  echo "  ▸ fsync_4k (dd oflag=dsync, 4KiB x $N_SYNC)"
  t0=$(date +%s%N)
  timeout $(( RT * 4 + 30 )) dd if=/dev/zero of="$WORK/sync.dat" bs=4k count=$N_SYNC oflag=dsync 2>"$OUT/fsync_4k.err"
  t1=$(date +%s%N)
  done_n=$(awk '/records out/ {split($1, a, "+"); print a[1]; exit}' "$OUT/fsync_4k.err")
  awk -v n="${done_n:-0}" -v ns="$(( t1 - t0 ))" 'BEGIN {
    avg = (n > 0) ? ns / n / 1e6 : 0
    printf "{\"engine\": \"dd\", \"jobs\": [{\"jobname\": \"fsync_4k\", \"write\": {\"iops\": %.1f, \"bw\": %.1f}}], \"sync_avg_ms\": %.3f}\n", (ns > 0 ? n / (ns / 1e9) : 0), (ns > 0 ? n * 4 / (ns / 1e9) : 0), avg
  }' > "$OUT/fsync_4k.json"
  echo "완료: $OUT"
  echo "ES 를 다시 올린 뒤 같은 서버에서 es_disk_collect.sh 를 실행하면 이 결과가 리포트에 자동으로 들어갑니다."
  exit 0
fi

# 검색: 작은 무작위 읽기, 동시 요청 많음
run randread_4k  --rw=randread  --bs=4k  --ioengine=libaio --iodepth=32 --numjobs=4
# segment flush·merge 쓰기: 큰 순차 쓰기
run seqwrite_1m  --rw=write     --bs=1m  --ioengine=libaio --iodepth=8  --numjobs=1
# merge 읽기·샤드 복구: 큰 순차 읽기
run seqread_1m   --rw=read      --bs=1m  --ioengine=libaio --iodepth=8  --numjobs=1
# 인덱싱+검색 혼합
run randrw_16k   --rw=randrw --rwmixread=70 --bs=16k --ioengine=libaio --iodepth=32 --numjobs=4
# translog fsync 비용: 쓰기마다 fdatasync, 동시성 1
run fsync_4k     --rw=write     --bs=4k  --ioengine=psync  --iodepth=1  --numjobs=1 --fdatasync=1

echo "완료: $OUT"
echo "ES 를 다시 올린 뒤 같은 서버에서 es_disk_collect.sh 를 실행하면 이 결과가 리포트에 자동으로 들어갑니다."
echo "이미 만든 번들에 넣으려면: python3 es_disk_render.py <수집 번들> --bench $OUT"
