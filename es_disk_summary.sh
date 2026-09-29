#!/usr/bin/env bash
# =============================================================================
# es_disk_summary.sh  (v0.10.0)
# 수집 번들을 bash + awk 만으로 요약 판정합니다. Python 이 없는 서버(RHEL 7 등)에서도 바로 결과를 봅니다.
# es_disk_collect.sh 가 끝날 때 자동으로 실행합니다. 따로 돌릴 때:
#
#   ./es_disk_summary.sh <번들 디렉터리>
#
# 핵심 수치와 판정만 냅니다. 병목 위치·클러스터 비교·출처가 달린 전체 판정은 HTML 리포트(es_disk_render.py)에 있습니다.
# 판정 기준은 HTML 리포트와 같습니다 (README "기준값 출처").
# =============================================================================
set -u
export LC_ALL=C
B="${1:-}"
[[ -n "$B" && -d "$B/static" ]] || { echo "사용: $0 <번들 디렉터리>"; exit 1; }
S="$B/static"

awk -v B="$B" '
function kv(line,   i) { i = index(line, "="); return i ? substr(line, i + 1) : "" }
function sortn(a, n,   i, j, t) { for (i = 2; i <= n; i++) { t = a[i]; j = i - 1; while (j >= 1 && a[j] > t) { a[j+1] = a[j]; j-- } a[j+1] = t } }
function pct(a, n, p,   k) { if (n < 1) return -1; sortn(a, n); k = int(p * (n - 1) + 0.5) + 1; return a[k] }
function f1(x) { return (x < 0) ? "-" : sprintf("%.1f", x) }
function f2(x) { return (x < 0) ? "-" : sprintf("%.2f", x) }
# kind: run = 측정 결과(지연·포화·오류), cfg = 설정·구성 위험, bench = es_disk_bench.sh 로 잰 스토리지 자체 능력.
# 판정 문장은 HTML 리포트와 같은 규칙으로 고른다
function add(sev, title, act, kind) { nf++; FS_[nf] = sev; FT[nf] = title; FA[nf] = act
  if (kind == "run") { if (rank[sev] > wrun) wrun = rank[sev] }
  else if (kind == "bench") { if (rank[sev] > wbench) wbench = rank[sev] }
  else if (rank[sev] > wcfg) wcfg = rank[sev] }
function phys(k, depth,   s, out, i, n, arr) {
  if (depth > 8 || k == "") return ""
  if (k in part_parent) return phys(part_parent[k], depth + 1)
  if (k in slaves) { n = split(slaves[k], arr, " "); out = ""; for (i = 1; i <= n; i++) if (arr[i] != "") out = out " " phys(arr[i], depth + 1); return out }
  return " " k
}
BEGIN { rank["참고"] = 1; rank["주의"] = 2; rank["경고"] = 3; rank["위험"] = 4; wrun = 0; wcfg = 0; wbench = 0; nbj = 0 }
FILENAME ~ /\/meta$/ { split($0, m, "="); meta[m[1]] = kv($0); next }
FILENAME ~ /\/static\/virt$/ { split($0, m, "="); virt[m[1]] = kv($0); next }
FILENAME ~ /\/static\/sysctl$/ { split($0, m, "="); sysctl[m[1]] = kv($0); next }
FILENAME ~ /\/static\/sysfs$/ {
  n = split($0, p, "|")
  if (p[1] == "ATTR") attr[p[2], p[3]] = p[4]
  else if (p[1] == "SLAVE") slaves[p[2]] = slaves[p[2]] " " p[3]
  else if (p[1] == "PART") part_parent[p[2]] = p[3]
  else if (p[1] == "SCSIHOST") host[p[2]] = p[3]
  else if (p[1] == "HOSTDRV") drv[p[2]] = p[3]
  if (p[1] == "ATTR" && p[3] == "dm/name") dmname[p[4]] = p[2]
  next }
FILENAME ~ /\/static\/datadev$/ { split($0, p, "|"); if (p[4] != "" && p[4] != "?") { datak[p[4]] = 1; datap[p[4]] = p[2] } next }
FILENAME ~ /\/static\/data_paths$/ { if ($0 != "") dpath[++ndp] = $0; next }
FILENAME ~ /\/static\/mounts$/ { mnt_src[++nm] = $1; mnt_pt[nm] = $2; mnt_fs[nm] = $3; mnt_opt[nm] = $4; next }
FILENAME ~ /\/static\/swaps$/ { if (FNR > 1 && NF) { swaps++; swdev[swaps] = $1 } next }
FILENAME ~ /\/static\/es_nodeinfo\.json$/ { if ($0 ~ /"mlockall" *: *true/) mlock = "true"; else if ($0 ~ /"mlockall" *: *false/) mlock = "false"; next }
FILENAME ~ /\/static\/klog_io$/ {
  l = tolower($0)
  if (l ~ /i\/o error|blk_update_request|medium error|rejecting i\/o/) kl["I/O 오류"]++
  if (l ~ /xfs .*(error|shutdown|corruption)|ext4-fs error|read-only/) kl["파일시스템 오류"]++
  if (l ~ /hung_task|blocked for more than/) kl["hung task"]++
  if (l ~ /abort|reset/) kl["abort/reset"]++
  if (l ~ /timed out|timing out|timeout/) kl["타임아웃"]++
  if (l ~ /megaraid_sas.*\/0x[0-9a-f]+\/(fatal|crit|dead|warn)|(megaraid|hpsa|smartpqi|aacraid|mpt3sas).*(battery|bbu|cachevault|degraded|offline|predictive|rebuild)/) kl["RAID 컨트롤러 이벤트"]++
  if (l ~ /thin.*(out of data space|out-of-data-space)|snapshots: invalidating/) kl["LVM thin·snapshot 이상"]++
  next }
FILENAME ~ /\/static\/dmsetup_status$/ {
  for (i = 1; i < NF; i++) if ($i == "thin-pool") {
    split($(i + 3), dd, "/"); if (dd[2] > 0) { tp = 100.0 * dd[1] / dd[2]; if (tp > thinmax) { thinmax = tp; thinname = $1; sub(/:$/, "", thinname) } }
    break }
  next }
FILENAME ~ /\/static\/mdstat$/ { if ($0 ~ /\[[U_]*_[U_]*\]/) mddeg++; if ($0 ~ /(resync|recovery|reshape|check) *=/) mdop++; next }
FILENAME ~ /\/static\/raid_(storcli|ssacli|arcconf)$/ {
  # 벤더 RAID 도구 결과 (있을 때만). 상세 판정은 HTML 리포트, 여기서는 상태 이상만 잡는다
  l = $0
  # storcli·perccli JSON 은 한 줄에 키 하나. 어느 목록 안인지(rctx)로 상태 값을 해석한다.
  # storcli2·perccli2 는 키 이름·값 표기가 다를 수 있어 흔한 변형을 함께 본다
  if (FILENAME ~ /storcli/) {
    ll = tolower(l); gsub(/_/, " ", ll)
    if (ll ~ /"(vd list|virtual drives?( list)?|logical drives?( list)?|ld list)" *: *\[/ || ll ~ /"\/c[0-9]+\/v[0-9]+" *: *\[/) rctx = "vd"
    else if (ll ~ /"(pd list|physical drives?( list)?|drives?( list)?|pds for vd [0-9]+|drives for vd [0-9]+)" *: *\[/) rctx = "pd"
    else if (ll ~ /(cachevault|bbu|energy ?pack|supercap)/ && ll ~ /[\[{] *$/) rctx = "bb"
    if (ll ~ /"(state|vd state|drive state)" *: *"/) {
      v = ll; sub(/.*: *"/, "", v); sub(/".*/, "", v)
      if (rctx == "vd" && v ~ /^(dgrd|pdgd|ofln|degraded|partially degraded|offline)$/) raid_vd++
      else if (rctx == "pd" && v ~ /^(offln|ubad|failed|rbld|offline|unconfigured bad|rebuild|rebuilding)$/) raid_pd++
      else if (rctx == "bb" && v ~ /^(degraded|failed|learning|missing)$/) raid_bat++
    }
    if (rctx == "vd" && ll ~ /"(cache|write cache|cache policy|write policy)" *: *"[^"]*(wt|write ?-?through)[^"]*"/) raid_wt++
  }
  if (l ~ /Status of [Ll]ogical [Dd]evice *: *(Degraded|Failed|Offline|Impacted)/ || l ~ /^ +Status: (Failed|Interim Recovery Mode|Recovering)/) raid_vd++
  if (l ~ /Battery\/Capacitor Status: *(Failed|Recharging|Not Fully Charged)/ || l ~ /Overall Backup Unit Status *: *(Failed|Degraded)/) raid_bat++
  if (l ~ /Cache Status: *(Temporarily|Permanently) Disabled/) raid_wt++
  if (l ~ /^ +State +: *(Failed|Offline)/) raid_pd++
  if (l !~ /^#TOOL_ABSENT/ && l != "") raid_tool = 1
  # 구성 디스크 매체: 컨트롤러가 알려 주면 rotational 추정 대신 이것을 쓴다
  ln = tolower(l); gsub(/_/, " ", ln)
  if (ln ~ /"(med|media|media type)" *: *"(ssd|nvme|solid state)/ || l ~ /Interface Type: Solid State/ || l ~ /(, SSD,|Device Type +: SSD)/) raid_ssd++
  if (ln ~ /"(med|media|media type)" *: *"(hdd|hard disk)/ || l ~ /Rotational Speed:/ || l ~ /(, HDD,|Device Type +: HDD)/) raid_hdd++
  next }
FILENAME ~ /\/static\/storage$/ {
  split($0, p, "|"); if (p[1] == "NVME") nv[p[2], p[3]] = p[4]; next }
FILENAME ~ /\/static\/procio_(start|end)$/ {
  st = (FILENAME ~ /start$/) ? 0 : 1
  if (match($0, /^\/proc\/[0-9]+\/io:(read|write)_bytes: *[0-9]+/)) {
    split($0, a, "/"); pid = a[3]; v = $NF + 0; pio[st, pid] += v; seen[pid] = 1
  } else if ($0 ~ /^\/proc\/[0-9]+\/comm:/) { split($0, a, "/"); pid = a[3]; c = $0; sub(/^[^:]*:/, "", c); comm[pid] = c }
  next }
FILENAME ~ /\/static\/ebs_stats_(start|end)$/ {
  # AWS EBS: 한도 초과 누적 시간(us). JSON(한 줄·여러 줄)과 텍스트(제목 + IOPS/Throughput 줄, 또는 이름: 값) 모두 읽는다
  es_ = (FILENAME ~ /start$/) ? 0 : 1
  if ($1 == "#DEV") { edev = $2; esec = ""; next }
  n_ = split(tolower($0), part_, ",")
  for (i = 1; i <= n_; i++) {
    x = part_[i]; hasn = (x ~ /[:=] *[0-9]+ *(us)? *[}]* *$/)
    v = x; sub(/.*[:=] */, "", v); gsub(/[^0-9]/, "", v)
    if (x ~ /exceeded/) {
      sc = (x ~ /instance/) ? "inst" : ((x ~ /volume/) ? "vol" : esec)
      mt = (x ~ /iops/) ? 1 : ((x ~ /throughput|(^|[^a-z])tp([^a-z]|$)/) ? 1 : 0)
      if (hasn && mt && sc != "") { ebsv[es_, edev, sc, (x ~ /iops/) ? "i" : "t"] = v + 0; ebsdev[edev] = 1 }
      else if (!hasn) esec = (sc != "") ? sc : "vol"
    } else if (esec != "" && x ~ /^ *(iops|throughput|tp) *[:=] *[0-9]+/) {
      ebsv[es_, edev, esec, (x ~ /iops/) ? "i" : "t"] = v + 0; ebsdev[edev] = 1
    } else if (x !~ /:/ && x ~ /[a-z]/) esec = ""
  }
  next }
FILENAME ~ /\/bench\/[^\/]+\.json$/ {
  # es_disk_bench.sh 결과. fio JSON 은 read·write·sync 절마다 percentile 이 있고, dd 결과는 sync_avg_ms 한 줄
  bn = FILENAME; sub(/.*\//, "", bn); sub(/\.json$/, "", bn); if (!(bn in benchseen)) { benchseen[bn] = 1; nbf++ }
  if ($0 ~ /^ *"(read|write|sync|trim)" *: *\{/) { bsec = $0; sub(/^ *"/, "", bsec); sub(/".*/, "", bsec) }
  if ($0 ~ /"99\.000000" *: *[0-9]/ && !((bn, bsec) in bp99)) { v = $0; sub(/.*: */, "", v); gsub(/[^0-9.]/, "", v); bp99[bn, bsec] = v / 1e6 }
  if ($0 ~ /"sync_avg_ms" *: *[0-9]/) { v = $0; sub(/.*"sync_avg_ms" *: */, "", v); sub(/[^0-9.].*/, "", v); bavg[bn] = v + 0 }
  if ($0 ~ /^ *"iops" *: *[0-9]/ && bsec != "" && !((bn, bsec, "iops") in biops)) { v = $0; sub(/.*: */, "", v); gsub(/[^0-9.]/, "", v); biops[bn, bsec, "iops"] = v + 0 }
  if ($0 ~ /^ *"bw" *: *[0-9]/ && bsec != "" && !((bn, bsec, "bw") in biops)) { v = $0; sub(/.*: */, "", v); gsub(/[^0-9.]/, "", v); biops[bn, bsec, "bw"] = v + 0 }
  next }
FILENAME ~ /samples\.raw$/ {
  if ($1 == "#T") { ns++; t[ns] = $2; sec = ""; next }
  if ($1 == "==>") { sec = $2; next }
  if (sec == "/proc/diskstats" && NF >= 14) { for (i = 4; i <= NF; i++) ds[ns, $3, i - 3] = $i; devseen[$3] = 1 }
  else if (sec == "/proc/pressure/io" && $1 == "full") { split($NF, q, "="); psi[ns] = q[2] }
  else if (sec == "DSTATE") dst[ns] = $1
  else if (sec == "/proc/vmstat" && ($1 == "pswpin" || $1 == "pswpout")) vmsw[ns] += $2
  next }
END {
  # ── ES data 물리 디스크 ─────────────────────────────────────────────
  if (ndp == 0) dpath[++ndp] = "/var/lib/elasticsearch"      # 수집기가 경로를 못 찾았을 때의 패키지 기본 경로
  dlist = ""
  for (k in datak) dlist = dlist phys(k, 0)
  if (dlist == "") {
    for (i = 1; i <= ndp; i++) {
      best = 0; bi = 0
      for (j = 1; j <= nm; j++) { mp = mnt_pt[j]; if ((index(dpath[i], mp "/") == 1 || dpath[i] == mp || mp == "/") && length(mp) > best) { best = length(mp); bi = j } }
      if (bi) { src = mnt_src[bi]; sub(/^\/dev\//, "", src); if (src ~ /^mapper\//) { sub(/^mapper\//, "", src); src = dmname[src] } dlist = dlist phys(src, 0); dmount[dpath[i]] = bi }
    }
  }
  nd = split(dlist, D, " "); ndev = 0
  for (i = 1; i <= nd; i++) if (D[i] != "" && !(D[i] in used)) { used[D[i]] = 1; dev[++ndev] = D[i] }
  if (ndev == 0) { for (d in devseen) if (d !~ /^(dm-|md|loop|sr|zram|ram)/) dev[++ndev] = d; guess = 1 }
  devs = ""; for (i = 1; i <= ndev; i++) devs = devs (i > 1 ? ", " : "") dev[i]

  # ── 플랫폼·판정 기준 (HTML 리포트와 같은 기준) ─────────────────────
  vv = virt["detect_virt_vm"]; if (vv == "") vv = virt["detect_virt"]
  if (meta["platform"] == "baremetal") vv = "none"; else if (meta["platform"] == "vmware") vv = "vmware"; else if (meta["platform"] == "vm" && (vv == "none" || vv == "")) vv = "vm"
  if (vv ~ /^(docker|podman|lxc|lxc-libvirt|systemd-nspawn|openvz|rkt|wsl|proot|pouch|container-other)$/) vv = ""
  if (vv == "vmware" || tolower(virt["sys_vendor"]) ~ /vmware/) { plat = "VMware Guest"; media = "vmware" }
  else if (vv != "" && vv != "none" && vv != "unknown") {
    plat = vv " Guest"; media = "vm"
    for (k in nv) { split(k, kk, SUBSEP); if (kk[2] == "model" && nv[k] ~ /Elastic Block Store|MSFT NVMe Accelerator|nvme_card-pd|PersistentDisk/) media = "cloud" }
  }
  else if (vv == "none" || virt["cpu_hypervisor_flag"] == "0") { plat = "bare-metal"; media = "" }
  else { plat = "플랫폼 미확정"; media = "vm" }
  st = meta["storage"]; if (st != "" && st != "auto" && !(meta["tool_version"] ~ /^0\.9/)) media = st
  unsure = 0
  if (media == "") {
    media = "nvme"
    for (i = 1; i <= ndev; i++) {
      d = dev[i]; hd = drv[host[d]]
      c = d; sub(/n[0-9]+$/, "", c)
      if (d ~ /^nvme/ && nv[c, "model"] ~ /Elastic Block Store|MSFT NVMe Accelerator|nvme_card-pd|PersistentDisk/) mm = "cloud"
      else if (d ~ /^nvme/) mm = "nvme"; else if (d ~ /^(rbd|nbd)/) mm = "network"; else mm = (attr[d, "queue/rotational"] == "1") ? "hdd" : "ssd"
      if (hd ~ /^(megaraid_sas|mpi3mr|hpsa|smartpqi|aacraid|arcmsr)$/) {
        if (raid_tool && raid_hdd) mm = "hdd"; else if (raid_tool && raid_ssd) mm = "ssd"; else unsure = 1
      }
      if (mm == "hdd" || ((mm == "network" || mm == "cloud") && media != "hdd") || (mm == "ssd" && media == "nvme")) media = mm
    }
  }
  if (media == "allflash" || media == "vmware" || media == "vmfs" || media == "vm" || media == "cloud" || media == "network") { c1 = 5; c2 = 10; c3 = 20 }
  else if (media == "hybrid") { c1 = 10; c2 = 20; c3 = 30 }
  else if (media == "nvme") { c1 = 1; c2 = 3; c3 = 10 }
  else if (media == "ssd") { c1 = 3; c2 = 6; c3 = 15 }
  else if (media == "hdd") { c1 = 25; c2 = 30; c3 = 50 }
  else { c1 = 5; c2 = 10; c3 = 20 }

  # ── 구간별 합산 지표 ────────────────────────────────────────────────
  nr = nw = ni = nb = nq = nu = 0; tf = tft = tdt = 0
  for (s = 2; s <= ns; s++) {
    dt = t[s] - t[s-1]; if (dt <= 0) continue
    rio = wio = rtk = wtk = rsec = wsec = wtd = util = fl = ftk = 0
    for (i = 1; i <= ndev; i++) {
      d = dev[i]
      if (ds[s, d, 1] == "" || ds[s-1, d, 1] == "") continue
      rio += ds[s, d, 1] - ds[s-1, d, 1];  rsec += ds[s, d, 3] - ds[s-1, d, 3];  rtk += ds[s, d, 4] - ds[s-1, d, 4]
      wio += ds[s, d, 5] - ds[s-1, d, 5];  wsec += ds[s, d, 7] - ds[s-1, d, 7];  wtk += ds[s, d, 8] - ds[s-1, d, 8]
      u = (ds[s, d, 10] - ds[s-1, d, 10]) / (dt * 10); if (u > util) util = u
      wtd += ds[s, d, 11] - ds[s-1, d, 11]
      if (ds[s, d, 17] != "") { fl += ds[s, d, 16] - ds[s-1, d, 16]; ftk += ds[s, d, 17] - ds[s-1, d, 17]; hasf = 1 }
    }
    if (rio >= 20) R[++nr] = rtk / rio
    if (wio >= 20) W[++nw] = wtk / wio
    I[++ni] = (rio + wio) / dt; M[++nb] = (rsec + wsec) * 512 / 1048576 / dt; Q[++nq] = wtd / (dt * 1000); U[++nu] = (util > 100 ? 100 : util)
    tf += fl; tft += ftk; tdt += dt
  }
  rp = pct(R, nr, 0.95); wp = pct(W, nw, 0.95); ip = pct(I, ni, 0.95); mp_ = pct(M, nb, 0.95); qp = pct(Q, nq, 0.95); up = pct(U, nu, 0.95)
  np_ = 0; for (s = 2; s <= ns; s++) if (s in psi) { P[++np_] = (psi[s] - psi[s-1]) / ((t[s] - t[s-1]) * 1e6) * 100 }
  pp = pct(P, np_, 0.95)
  nds = 0; for (s = 1; s <= ns; s++) if (s in dst) DS[++nds] = dst[s]
  dp = pct(DS, nds, 0.95)
  qd = 0; for (i = 1; i <= ndev; i++) qd += attr[dev[i], "device/queue_depth"]
  # 묶음(stripe·md·multipath) 안에서 한 장치만 느린가: 장치별 p95 를 나머지 중앙값과 비교
  nbusy = 0
  for (i = 1; i <= ndev; i++) {
    d = dev[i]; nx = 0; split("", X)
    for (s = 2; s <= ns; s++) {
      if (ds[s, d, 1] == "" || ds[s-1, d, 1] == "") continue
      a1 = ds[s, d, 1] - ds[s-1, d, 1]; a5 = ds[s, d, 5] - ds[s-1, d, 5]
      if (a1 >= 20) X[++nx] = (ds[s, d, 4] - ds[s-1, d, 4]) / a1
      if (a5 >= 20) X[++nx] = (ds[s, d, 8] - ds[s-1, d, 8]) / a5
    }
    if (nx >= 3) { devp[d] = pct(X, nx, 0.95); busyd[++nbusy] = d }
  }
  if (nbusy >= 2) {
    wd = ""; wv = -1; for (i = 1; i <= nbusy; i++) if (devp[busyd[i]] > wv) { wv = devp[busyd[i]]; wd = busyd[i] }
    no = 0; split("", Y); for (i = 1; i <= nbusy; i++) if (busyd[i] != wd) Y[++no] = devp[busyd[i]]
    med = pct(Y, no, 0.5)
    if (med > 0 && wv >= c1 && wv >= 2 * med) {
      add("경고", "묶음 디스크 중 " wd " 만 느림 (p95 " f2(wv) " ms, 나머지 중앙값 " f2(med) " ms)", "그 디스크의 SMART·커널 로그·경로를 확인하고 교체 검토", "run")
      if (wv >= c3) outl = 4; else if (wv >= c2) outl = 3; else outl = 2
      if (outl > wrun) wrun = outl
    }
  }

  # ── 판정 ────────────────────────────────────────────────────────────
  lat = (rp > wp) ? rp : wp
  lowload = (ip < 50 && mp_ < 5)
  if (nr + nw == 0) latj = "평가 불가 (I/O 가 거의 없음)"
  else if (lat >= c3) { latj = "위험"; add("위험", "디스크 응답시간이 기준의 위험 수준 (p95 " f2(lat) " ms)", "HTML 리포트의 병목 위치 판정을 먼저 보세요", "run") }
  else if (lat >= c2) { latj = "경고"; add("경고", "디스크 응답시간이 기준의 경고 수준 (p95 " f2(lat) " ms)", "HTML 리포트의 병목 위치 판정을 먼저 보세요", "run") }
  else if (lat >= c1) { latj = "주의"; add("주의", "디스크 응답시간이 기준의 주의 수준 (p95 " f2(lat) " ms)", "피크 시간대 추세를 확인하세요", "run") }
  else latj = "정상"
  if (qd > 0 && qp >= 0 && qp / qd >= 0.8 && lat >= c1) add("경고", "큐 사용률 " int(100 * qp / qd) "% (aqu-sz p95 " f1(qp) " / queue_depth " qd ")", "디스크(가상 디스크)를 늘려 stripe 로 묶거나 부하를 나누세요", "run")
  if (pp >= 20) add("경고", "I/O 압박(PSI io full) p95 " f1(pp) "%", "응답시간·D 상태와 함께 원인을 가르세요", "run")
  else if (pp >= 5) add("주의", "I/O 압박(PSI io full) p95 " f1(pp) "%", "응답시간·D 상태와 함께 원인을 가르세요", "run")
  if (dp >= 8) add("경고", "ES 스레드가 디스크 대기(D 상태)로 자주 멈춤 (p95 " dp "개)", "쓰기면 translog·merge, 읽기면 page cache 부족을 의심", "run")
  if (hasf && tf / tdt >= 1 && tft / tf >= c1) add((tft / tf >= c2) ? "경고" : "주의", "flush(장치 캐시 비우기) 평균 " f2(tft / tf) " ms, 초당 " f1(tf / tdt) "회", "전원 차단 보호가 있는 SSD, 배터리 보호 RAID 캐시인지 확인")
  for (k in kl) if (kl[k] > 0) {
    sv = (k ~ /I\/O 오류|파일시스템/) ? "위험" : (k ~ /타임아웃/ ? "주의" : "경고")
    add(sv, "커널 로그: " k " " kl[k] "건 (최근 7일)", "번들의 static/klog_io 원문 시각을 담당자에게 전달", "run")
  }
  win = (ns >= 2) ? t[ns] - t[1] : 0
  for (d in ebsdev) if (win > 0) {
    # IOPS·처리량 중 큰 쪽 (Python 판정과 같다)
    ev = ebsv[1, d, "vol", "i"] - ebsv[0, d, "vol", "i"]; x = ebsv[1, d, "vol", "t"] - ebsv[0, d, "vol", "t"]; if (x > ev) ev = x
    ei = ebsv[1, d, "inst", "i"] - ebsv[0, d, "inst", "i"]; x = ebsv[1, d, "inst", "t"] - ebsv[0, d, "inst", "t"]; if (x > ei) ei = x
    ev /= 1e6; ei /= 1e6
    if (ev >= 0.01 * win) add((ev >= 0.1 * win) ? "경고" : "주의", "AWS EBS 볼륨 성능 한도 초과 " f1(ev) "초 (" d ", 측정 " int(win) "초 중)", "볼륨 IOPS·처리량 설정 상향 또는 볼륨 분산 (가상화·클라우드 관리자)", "run")
    if (ei >= 0.01 * win) add((ei >= 0.1 * win) ? "경고" : "주의", "EC2 인스턴스 EBS 한도 초과 " f1(ei) "초 (" d ", 측정 " int(win) "초 중)", "EBS 대역폭이 더 큰 인스턴스 유형으로 변경 (가상화·클라우드 관리자)", "run")
  }
  mmc = sysctl["vm.max_map_count"] + 0
  if (mmc > 0 && mmc < 262144) add("위험", "vm.max_map_count " mmc " (ES 최소 262144)", "sysctl -w vm.max_map_count=1048576 + /etc/sysctl.d 영구화")
  for (i = 1; i <= ndev; i++) {
    d = dev[i]; ra = attr[d, "queue/read_ahead_kb"] + 0
    if (ra > 128) add((ra >= 1024) ? "경고" : "주의", d " readahead " ra "KB (Elastic 권고 128KiB)", "blockdev --setra 256 /dev/" d " + udev 규칙")
    sc = attr[d, "queue/scheduler"]; if (sc ~ /\[(cfq|bfq)\]/ && !(media == "hdd")) add("주의", d " I/O 스케줄러 " sc, "mq-deadline 또는 none")
    stt = attr[d, "device/state"]; if (stt != "" && stt != "running") add("위험", d " 장치 상태 " stt, "커널 로그와 하드웨어 상태 즉시 확인", "run")
    tmo = attr[d, "device/iotmo_cnt"]; if (tmo ~ /^0x/ && tmo != "0x0") add("주의", d " 명령 타임아웃 기록 " tmo " (부팅 후 누적)", "커널 로그의 timeout·reset 시각 확인", "run")
    if (plat == "VMware Guest") { to = attr[d, "device/timeout"] + 0; if (to > 0 && to < 60) add("경고", d " SCSI timeout " to "초 (VMware 권고 180초)", "open-vm-tools 설치로 udev 규칙 적용") }
    c = d; sub(/n[0-9]+$/, "", c)
    if (d ~ /^nvme/ && nv[c, "temp"] != "" && nv[c, "temp_max"] != "" && nv[c, "temp"] + 0 >= nv[c, "temp_max"] + 0)
      add("경고", c " 온도 " int(nv[c, "temp"] / 1000) "C, 경고 온도 도달", "냉각·공기 흐름 점검")
  }
  # swap: HTML 과 같은 규칙. 측정 중 swap 입출력, swap 켜짐 + memory_lock 꺼짐, swap 이 data 디스크에 있음
  swmax = 0
  for (s = 2; s <= ns; s++) if (((s - 1) in vmsw) && (s in vmsw) && t[s] > t[s - 1]) { r_ = (vmsw[s] - vmsw[s - 1]) / (t[s] - t[s - 1]); if (r_ > swmax) swmax = r_ }
  if (swmax > 0) add("경고", "측정 중 swap 입출력 발생 (최대 " f1(swmax) " pages/s)", "swap 끄기(swapoff -a, /etc/fstab 정리) 또는 bootstrap.memory_lock: true")
  if (swaps > 0 && mlock != "true") {
    swp_ = sysctl["vm.swappiness"]
    add((swp_ != "" && swp_ + 0 <= 1) ? "주의" : "경고", "swap 이 켜져 있고 memory_lock 도 " (mlock == "false" ? "꺼져 있음" : "확인 안 됨") " (swappiness " (swp_ == "" ? "-" : swp_) ")", "Elastic 권고 순서: swap 끄기 > bootstrap.memory_lock: true > vm.swappiness=1")
  }
  if (!guess) for (i = 1; i <= swaps; i++) {
    k = swdev[i]; if (k !~ /^\/dev\//) continue
    sub(/^\/dev\//, "", k); if (k ~ /^mapper\//) { sub(/^mapper\//, "", k); k = dmname[k] }
    sp = phys(k, 0); n_ = split(sp, spa, " ")
    hit_ = 0
    for (j = 1; j <= n_; j++) for (q_ = 1; q_ <= ndev; q_++) if (spa[j] != "" && spa[j] == dev[q_]) hit_ = 1
    if (hit_) add("주의", "swap 이 ES data 디스크에 있음 (" swdev[i] ")", "swap 을 다른 디스크로 옮기거나 끄기")
  }
  for (i = 1; i <= ndp; i++) if (dmount[dpath[i]]) {
    o = mnt_opt[dmount[dpath[i]]]; fs_ = mnt_fs[dmount[dpath[i]]]
    if (o ~ /(^|,)(nobarrier|barrier=0)(,|$)/) add("경고", dpath[i] " barrier 꺼짐 (" o ")", "전원 차단 시 데이터 손실 위험. 옵션 제거")
    if (o ~ /(^|,)(sync|dirsync)(,|$)/) add("경고", dpath[i] " sync 마운트", "sync 옵션 제거")
    if (fs_ ~ /^(nfs|nfs4|cifs|tmpfs)$/) add("위험", dpath[i] " 파일시스템 " fs_, "로컬 블록 장치의 xfs/ext4 로 이전")
  }
  if (thinmax >= 85) add((thinmax >= 95) ? "위험" : "경고", "LVM thin pool " thinname " 데이터 " sprintf("%.0f", thinmax) "% 사용", "pool 확장(lvextend) 또는 정리. 가득 차면 쓰기 중단")
  else if (thinmax >= 70) add("주의", "LVM thin pool " thinname " 데이터 " sprintf("%.0f", thinmax) "% 사용", "사용률 상시 감시")
  if (mddeg) add("경고", "소프트웨어 RAID degraded", "빠진 디스크 교체·재구성")
  if (raid_vd) add("경고", "RAID 논리 디스크가 정상 상태가 아님 (컨트롤러 도구 조회)", "빠진 디스크 교체·재구성 확인")
  if (raid_pd) add("경고", "RAID 구성 디스크 고장·재구성 (컨트롤러 도구 조회)", "해당 디스크 교체 검토")
  if (raid_bat) add("경고", "RAID 배터리·캐시 보호 모듈이 정상이 아님 (컨트롤러 도구 조회)", "배터리·캐시 모듈 상태 확인. 캐시가 write-through 로 떨어졌을 수 있음")
  if (raid_wt) add("주의", "RAID 쓰기 캐시가 write-through 로 동작 (컨트롤러 도구 조회)", "배터리 상태와 캐시 정책 확인. HDD 에서는 쓰기 지연이 크게 늘어남")
  if (mdop) add("참고", "소프트웨어 RAID resync·check 진행 중 (측정값이 평소보다 나쁠 수 있음)", "작업 종료 후 재측정")
  if (virt["cpu_governor"] ~ /^(powersave|conservative|ondemand)$/ && plat == "bare-metal") add("주의", "CPU governor " virt["cpu_governor"], "tuned-adm profile throughput-performance")
  if (unsure && st == "auto") add("참고", "RAID 논리 디스크라 매체를 rotational 값으로 추정", "실제 매체가 다르면 -s ssd|hdd 로 다시 실행")

  # 옆집 프로세스
  esp = meta["es_pid"]; tot = 0; top1 = ""; topv = 0
  for (pid in seen) { v = pio[1, pid] - pio[0, pid]; if (v <= 0 || !((0, pid) in pio)) continue; tot += v; if (pid == esp) esv = v; else if (v > topv) { topv = v; top1 = comm[pid] "(pid " pid ")" } }
  if (tot >= 52428800 && esp != "" && (tot - esv) / tot >= 0.3 && !lowload) add("주의", "ES 가 아닌 프로세스의 디스크 I/O 비중 " int(100 * (tot - esv) / tot) "% (상위: " top1 " " int(topv / 1048576) "MB)", "그 프로세스가 ES data 디스크를 쓰는지 확인")

  # ── 구축 전 적합성 (벤치): translog fsync 한 건, page cache 에 없는 데이터 한 건 읽기. 판정 기준은 응답시간 기준과 같다
  sv_ = -1; svl = ""
  if (("fsync_4k", "sync") in bp99) { sv_ = bp99["fsync_4k", "sync"]; svl = "p99" }
  else if ("fsync_4k" in bavg) { sv_ = bavg["fsync_4k"]; svl = "평균(dd)" }
  rr1 = (("randread_4k_qd1", "read") in bp99) ? bp99["randread_4k_qd1", "read"] : -1
  if (sv_ >= 0) { nbj++
    if (sv_ >= c1) add((sv_ >= c2) ? "경고" : "주의", "벤치: 동기 쓰기(fsync) 한 건 " svl " " f2(sv_) " ms (기준 주의 " c1 " / 경고 " c2 " ms)", "쓰기 캐시 보호(전원 차단 보호 SSD, 배터리 보호 RAID 캐시)와 매체를 확인. bulk 요청마다 이 시간을 기다림", "bench")
  }
  if (rr1 >= 0) { nbj++
    if (rr1 >= c1) add((rr1 >= c2) ? "경고" : "주의", "벤치: 무작위 읽기 한 건 p99 " f2(rr1) " ms (기준 주의 " c1 " / 경고 " c2 " ms)", "매체(HDD 여부), RAID·스토리지 경로, 공유 스토리지의 다른 부하를 확인", "bench")
  }

  # ── 출력 ────────────────────────────────────────────────────────────
  if (wrun >= 3) verdict = "디스크 성능 저하 징후가 있습니다"
  else if (lowload && wrun <= 2 && nbj > 0) {
    if (wbench >= 3) verdict = "부하 전 점검: 스토리지가 ES 기준보다 느립니다"
    else if (wbench == 2 || wcfg >= 3) verdict = "부하 전 점검: 대체로 충족하지만 확인할 항목이 있습니다"
    else verdict = "부하 전 점검: 스토리지가 ES 기준을 충족합니다"
  }
  else if (lowload && wrun <= 2) { verdict = "설정 점검은 완료, 성능 판정은 보류합니다"; vnote = "측정 시간대 부하가 낮습니다. 피크 때 다시 실행하거나, 구축 전이라면 es_disk_bench.sh 를 먼저 돌린 뒤 다시 수집하세요" }
  else if (wcfg >= 3 || wrun == 2) verdict = "지금은 버티고 있지만 위험 요인이 있습니다"
  else verdict = "디스크는 정상입니다"
  mlab["nvme"] = "NVMe"; mlab["ssd"] = "SSD"; mlab["hdd"] = "HDD"; mlab["vmware"] = "VMware 공유 스토리지"; mlab["allflash"] = "vSAN All-Flash"
  mlab["hybrid"] = "vSAN Hybrid"; mlab["vmfs"] = "VMware SAN·NFS 데이터스토어"; mlab["vm"] = "가상 디스크 공통"; mlab["network"] = "네트워크 블록"; mlab["cloud"] = "클라우드 볼륨"
  printf "=== es-disk-probe 요약 판정 (셸) ===\n"
  printf "호스트 %s · %s · 대상 디스크 %s%s\n", meta["host"], plat, devs, (guess ? " (data 경로 미확인, 전체 디스크)" : "")
  cont = virt["detect_virt_container"]; if (cont == "" && virt["detect_virt"] ~ /^(docker|podman|lxc|lxc-libvirt|systemd-nspawn|openvz|rkt|wsl|proot|pouch|container-other)$/) cont = virt["detect_virt"]
  if (cont != "" && cont != "none") printf "주의: 컨테이너(%s) 안에서 실행한 결과입니다. 가능하면 호스트에서 다시 실행하세요\n", cont
  printf "판정 기준 %s%s · 응답시간 주의 %s / 경고 %s / 위험 %s ms\n", (media in mlab) ? mlab[media] : media, (unsure ? " (추정)" : ""), c1, c2, c3
  printf "\n판정: %s\n", verdict
  if (vnote != "") printf "      %s\n", vnote
  printf "\n"
  printf "응답시간 p95  읽기 %s ms · 쓰기 %s ms  → %s\n", f2(rp), f2(wp), latj
  printf "부하 p95      IOPS %s · %s MB/s · aqu-sz %s%s · %%util %s\n", f1(ip), f1(mp_), f1(qp), (qd > 0 ? " / queue_depth " qd : ""), f1(up)
  if (hasf && tf > 0) printf "flush         초당 %s회 · 평균 %s ms\n", f1(tf / tdt), f2(tft / tf)
  printf "포화          PSI io full p95 %s%% · ES D 상태 스레드 p95 %s\n", (np_ ? f1(pp) : "-"), (nds ? dp : "-")
  if (nbf > 0) {
    printf "벤치          동기 쓰기 한 건 %s · 무작위 읽기 한 건 %s", (sv_ >= 0 ? svl " " f2(sv_) " ms" : "-"), (rr1 >= 0 ? "p99 " f2(rr1) " ms" : "-")
    if (("randread_4k", "read", "iops") in biops) printf " · 무작위 읽기 최대 %s IOPS", int(biops["randread_4k", "read", "iops"])
    if (("seqwrite_1m", "write", "bw") in biops) printf " · 순차 쓰기 %s MB/s", int(biops["seqwrite_1m", "write", "bw"] / 1024)
    printf "\n"
  }
  if (nf) {
    printf "\n확인할 항목 (심각한 순)\n"
    for (r = 4; r >= 1; r--) for (i = 1; i <= nf; i++) if (rank[FS_[i]] == r) printf "  [%s] %s\n         → %s\n", FS_[i], FT[i], FA[i]
  } else printf "\n확인할 항목 없음\n"
  printf "\n전체 판정(병목 위치, 클러스터 비교, 기준 출처)은 HTML 리포트에 있습니다.\n"
}' "$B/meta" "$S/virt" "$S/sysctl" "$S/sysfs" \
   $( [[ -r "$S/datadev" ]] && echo "$S/datadev" ) $( [[ -r "$S/data_paths" ]] && echo "$S/data_paths" ) \
   "$S/mounts" "$S/swaps" "$S/klog_io" \
   $( [[ -r "$S/dmsetup_status" ]] && echo "$S/dmsetup_status" ) $( [[ -r "$S/mdstat" ]] && echo "$S/mdstat" ) \
   $( [[ -r "$S/storage" ]] && echo "$S/storage" ) \
   $( for r in raid_storcli raid_ssacli raid_arcconf; do [[ -r "$S/$r" ]] && echo "$S/$r"; done ) \
   $( [[ -r "$S/procio_start" ]] && echo "$S/procio_start" "$S/procio_end" ) \
   $( [[ -s "$S/ebs_stats_start" && -s "$S/ebs_stats_end" ]] && echo "$S/ebs_stats_start" "$S/ebs_stats_end" ) \
   $( ls "$B"/bench/*.json 2>/dev/null ) \
   $( [[ -r "$S/es_nodeinfo.json" ]] && echo "$S/es_nodeinfo.json" ) \
   "$B/samples.raw" 2>/dev/null | tee "$B/summary.txt"
