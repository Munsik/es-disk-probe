#!/usr/bin/env bash
# =============================================================================
# es_disk_bench.sh  (v0.9.0, 선택 사항)
# ES data 디스크의 "최대 능력"을 fio로 측정합니다. → 리포트의 여유율 계산용
#
# ⚠ 이 스크립트는 디스크에 실제 부하를 겁니다. 반드시 아래 조건에서만 실행하세요.
#   - 서비스 투입 전, 또는 해당 노드의 ES를 내린 점검 시간
#   - vSAN은 호스트 여러 대가 공유하는 스토리지입니다. 벤치 부하가 같은 클러스터의
#     다른 VM에도 영향을 줄 수 있으니 VMware 관리자와 시간을 맞추세요.
#
# 안전장치:
#   - ES 프로세스가 떠 있으면 실행 거부 (--force-with-es 로만 우회)
#   - 테스트 파일 생성 후 디스크 사용률이 80%를 넘으면 실행 거부
#   - 테스트 전용 하위 디렉터리만 사용하고 종료 시(중단 포함) 삭제
#   - drop_caches, sync 강제, 원시 장치 쓰기는 하지 않음 (direct I/O 파일 테스트만)
#
# 사용: sudo ./es_disk_bench.sh -t /var/lib/elasticsearch [-s 4G] [-r 30] [-o /tmp]
# 결과: <출력>/esbench_<host>_<ts>/*.json  →  es_disk_render.py --bench <이 디렉터리>
#
# 해석 주의: 테스트 파일이 vSAN 캐시 계층에 들어가면 결과가 실제보다 좋게 나옵니다.
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
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 1 ;;
  esac
done
[[ -n "$TARGET" && -d "$TARGET" ]] || { echo "-t 로 ES data 경로(존재하는 디렉터리)를 지정하세요"; exit 1; }
command -v fio >/dev/null 2>&1 || { echo "fio가 없습니다. 폐쇄망이면 OS 설치 미디어의 fio rpm/deb를 설치하세요."; exit 1; }

if pgrep -f 'org\.elasticsearch\.bootstrap\.Elasticsearch' >/dev/null 2>&1 && [[ $FORCE -eq 0 ]]; then
  echo "ES가 실행 중입니다. 벤치 부하가 서비스 I/O와 경합하므로 실행하지 않습니다."
  echo "점검 시간에 ES를 내린 뒤 실행하거나, 영향을 감수할 때만 --force-with-es 를 쓰세요."
  exit 2
fi

to_kb() { local v=${1^^}; case "$v" in *G) echo $(( ${v%G} * 1048576 ));; *M) echo $(( ${v%M} * 1024 ));; *) echo $(( v / 1024 ));; esac; }
NEED_KB=$(to_kb "$SIZE")
read -r TOT_KB USED_KB AVL_KB <<< "$(df -Pk "$TARGET" | awk 'NR==2{print $2,$3,$4}')"
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

run() { local name="$1"; shift; echo "  ▸ $name"; fio --name="$name" "${COMMON[@]}" "$@" > "$OUT/$name.json" 2>"$OUT/$name.err" || echo "    실패 — $OUT/$name.err 확인"; }

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

echo "완료. 리포트에 반영: python3 es_disk_render.py <수집 번들> --bench $OUT"
