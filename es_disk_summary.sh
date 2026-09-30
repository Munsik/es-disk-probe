#!/usr/bin/env bash
# =============================================================================
# es_disk_summary.sh  (v0.11.1)
# Summarizes a collected bundle into a verdict with bash + awk only. Shows results right away even on servers without Python (RHEL 7 etc.).
# Runs automatically when es_disk_collect.sh finishes. To run it separately:
#
#   ./es_disk_summary.sh <bundle directory> [--lang ko|en]
#
# Gives only key numbers and verdicts. The full verdict with bottleneck location, cluster comparison and sources is in the HTML report (es_disk_render.py).
# Verdict thresholds are the same as the HTML report (README "Threshold sources").
# =============================================================================
set -u
# Language: --lang ko|en; if absent, ko when the locale (LC_ALL, LC_MESSAGES, LANG) starts with ko, else en
B="" LNG=""
while [[ $# -gt 0 ]]; do
  case "$1" in --lang) LNG="${2:-}"; shift; [[ $# -gt 0 ]] && shift ;; --lang=*) LNG="${1#--lang=}"; shift ;; *) B="$1"; shift ;; esac
done
if [[ "$LNG" != ko && "$LNG" != en ]]; then
  _loc="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"; [[ "$_loc" == ko* ]] && LNG=ko || LNG=en
fi
export LC_ALL=C
HERE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
CATF="$HERE/i18n/$LNG.txt"; CATFB="$HERE/i18n/ko.txt"
if [[ -z "$B" || ! -d "$B/static" ]]; then
  [[ "$LNG" == ko ]] && echo "사용: $0 <번들 디렉터리> [--lang ko|en]" || echo "usage: $0 <bundle dir> [--lang ko|en]"
  exit 1
fi
S="$B/static"

awk -v B="$B" -v CATF="$CATF" -v CATFB="$CATFB" '
function kv(line,   i) { i = index(line, "="); return i ? substr(line, i + 1) : "" }
function sortn(a, n,   i, j, t) { for (i = 2; i <= n; i++) { t = a[i]; j = i - 1; while (j >= 1 && a[j] > t) { a[j+1] = a[j]; j-- } a[j+1] = t } }
function pct(a, n, p,   k) { if (n < 1) return -1; sortn(a, n); k = int(p * (n - 1) + 0.5) + 1; return a[k] }
function f1(x) { return (x < 0) ? "-" : sprintf("%.1f", x) }
function f2(x) { return (x < 0) ? "-" : sprintf("%.2f", x) }
# kind: run = measurement results (latency, saturation, errors), cfg = settings/configuration risk.
# Verdict sentences are chosen by the same rules as the HTML report
function add(sev, title, act, kind) { nf++; FS_[nf] = sev; FT[nf] = title; FA[nf] = act
  if (kind == "run") { if (rank[sev] > wrun) wrun = rank[sev] }
  else if (rank[sev] > wcfg) wcfg = rank[sev] }
# Message catalog (i18n/<lang>.txt, key = "text"). Missing keys fall back to ko, then to the key itself
function catline(line, A,   p, k, v) {
  if (line ~ /^[ \t]*#/) return
  p = index(line, " = \""); if (!p) return
  k = substr(line, 1, p - 1); gsub(/[ \t]+$/, "", k)
  v = substr(line, p + 4); sub(/"[ \t]*$/, "", v)
  gsub(/\\"/, "\"", v); gsub(/\\n/, "\n", v); gsub(/\\\\/, "\\", v)
  A[k] = v
}
function msg(k) { return (k in M) ? M[k] : ((k in MF) ? MF[k] : k) }
# "&" in a gsub replacement means the matched text, so escape it in values
function amp(x) { gsub(/&/, "\\&", x); return x }
function tr(k, a1, a2, a3, a4, a5,   s) {
  s = msg(k)
  gsub(/\{1\}/, amp(a1), s); gsub(/\{2\}/, amp(a2), s); gsub(/\{3\}/, amp(a3), s); gsub(/\{4\}/, amp(a4), s); gsub(/\{5\}/, amp(a5), s)
  return s
}
function phys(k, depth,   s, out, i, n, arr) {
  if (depth > 8 || k == "") return ""
  if (k in part_parent) return phys(part_parent[k], depth + 1)
  if (k in slaves) { n = split(slaves[k], arr, " "); out = ""; for (i = 1; i <= n; i++) if (arr[i] != "") out = out " " phys(arr[i], depth + 1); return out }
  return " " k
}
BEGIN { rank["info"] = 1; rank["caution"] = 2; rank["warn"] = 3; rank["crit"] = 4; wrun = 0; wcfg = 0
  while ((getline cl < CATF) > 0) catline(cl, M); close(CATF)
  while ((getline cl < CATFB) > 0) catline(cl, MF); close(CATFB)
  n = split("kvm KVM qemu QEMU microsoft Hyper-V xen Xen amazon AWS_Nitro google Google_Compute_Engine oracle VirtualBox powervm IBM_PowerVM zvm IBM_z/VM parallels Parallels bhyve bhyve", hv_, " ")
  for (i = 1; i < n; i += 2) { HVL[hv_[i]] = hv_[i + 1]; gsub(/_/, " ", HVL[hv_[i]]) }
  HVL["vm"] = HVL["vm-other"] = HVL["unknown-vm"] = msg("r.0018")
  SEVK["info"] = "r.0013"; SEVK["caution"] = "r.0014"; SEVK["warn"] = "r.0015"; SEVK["crit"] = "r.0016" }
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
  if (l ~ /i\/o error|blk_update_request|medium error|rejecting i\/o/) kl["io_err"]++
  if (l ~ /xfs .*(error|shutdown|corruption)|ext4-fs error|read-only/) kl["fs_err"]++
  if (l ~ /hung_task|blocked for more than/) kl["hung_task"]++
  if (l ~ /abort|reset/) kl["abort_reset"]++
  if (l ~ /timed out|timing out|timeout/) kl["timeout"]++
  if (l ~ /megaraid_sas.*\/0x[0-9a-f]+\/(fatal|crit|dead|warn)|(megaraid|hpsa|smartpqi|aacraid|mpt3sas).*(battery|bbu|cachevault|degraded|offline|predictive|rebuild)/) kl["raid_evt"]++
  if (l ~ /thin.*(out of data space|out-of-data-space)|snapshots: invalidating/) kl["thin"]++
  next }
FILENAME ~ /\/static\/dmsetup_status$/ {
  for (i = 1; i < NF; i++) if ($i == "thin-pool") {
    split($(i + 3), dd, "/"); if (dd[2] > 0) { tp = 100.0 * dd[1] / dd[2]; if (tp > thinmax) { thinmax = tp; thinname = $1; sub(/:$/, "", thinname) } }
    break }
  next }
FILENAME ~ /\/static\/mdstat$/ { if ($0 ~ /\[[U_]*_[U_]*\]/) mddeg++; if ($0 ~ /(resync|recovery|reshape|check) *=/) mdop++; next }
FILENAME ~ /\/static\/raid_(storcli|ssacli|arcconf)$/ {
  # Vendor RAID tool results (only if present). Detailed verdict is in the HTML report; here we only catch abnormal states
  l = $0
  # storcli/perccli JSON has one key per line. State values are interpreted by which list they are in (rctx).
  # storcli2/perccli2 may differ in key names and value notation, so common variants are checked too
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
  # Member disk media: if the controller reports it, use it instead of guessing from rotational
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
  # AWS EBS: cumulative time over limit (us). Reads both JSON (single or multi-line) and text (title + IOPS/Throughput lines, or name: value)
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
FILENAME ~ /samples\.raw$/ {
  if ($1 == "#T") { ns++; t[ns] = $2; sec = ""; next }
  if ($1 == "==>") { sec = $2; next }
  if (sec == "/proc/diskstats" && NF >= 14) { for (i = 4; i <= NF; i++) ds[ns, $3, i - 3] = $i; devseen[$3] = 1 }
  else if (sec == "/proc/pressure/io" && $1 == "full") { split($NF, q, "="); psi[ns] = q[2] }
  else if (sec == "DSTATE") dst[ns] = $1
  else if (sec == "/proc/vmstat" && ($1 == "pswpin" || $1 == "pswpout")) vmsw[ns] += $2
  next }
END {
  # ── ES data physical disk ─────────────────────────────────────────────
  if (ndp == 0) dpath[++ndp] = "/var/lib/elasticsearch"      # package default path when the collector could not find one
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

  # ── Platform and verdict thresholds (same as the HTML report) ─────────────────────
  vv = virt["detect_virt_vm"]; if (vv == "") vv = virt["detect_virt"]
  if (meta["platform"] == "baremetal") vv = "none"; else if (meta["platform"] == "vmware") vv = "vmware"; else if (meta["platform"] == "vm" && (vv == "none" || vv == "")) vv = "vm"
  if (vv ~ /^(docker|podman|lxc|lxc-libvirt|systemd-nspawn|openvz|rkt|wsl|proot|pouch|container-other)$/) vv = ""
  if (vv == "vmware" || tolower(virt["sys_vendor"]) ~ /vmware/) { plat = "VMware Guest"; media = "vmware" }
  else if (vv != "" && vv != "none" && vv != "unknown") {
    plat = ((vv in HVL) ? HVL[vv] : vv) " Guest"; media = "vm"
    for (k in nv) { split(k, kk, SUBSEP); if (kk[2] == "model" && nv[k] ~ /Elastic Block Store|MSFT NVMe Accelerator|nvme_card-pd|PersistentDisk/) media = "cloud" }
  }
  else if (vv == "none" || virt["cpu_hypervisor_flag"] == "0") { plat = "bare-metal"; media = "" }
  else { plat = msg("s.plat_unknown"); media = "vm" }
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

  # ── Aggregate metrics per interval ────────────────────────────────────────────────
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
    I[++ni] = (rio + wio) / dt; MBS[++nb] = (rsec + wsec) * 512 / 1048576 / dt; Q[++nq] = wtd / (dt * 1000); U[++nu] = (util > 100 ? 100 : util)
    tf += fl; tft += ftk; tdt += dt
  }
  rp = pct(R, nr, 0.95); wp = pct(W, nw, 0.95); ip = pct(I, ni, 0.95); mp_ = pct(MBS, nb, 0.95); qp = pct(Q, nq, 0.95); up = pct(U, nu, 0.95)
  np_ = 0; for (s = 2; s <= ns; s++) if (s in psi) { P[++np_] = (psi[s] - psi[s-1]) / ((t[s] - t[s-1]) * 1e6) * 100 }
  pp = pct(P, np_, 0.95)
  nds = 0; for (s = 1; s <= ns; s++) if (s in dst) DS[++nds] = dst[s]
  dp = pct(DS, nds, 0.95)
  qd = 0; for (i = 1; i <= ndev; i++) qd += attr[dev[i], "device/queue_depth"]
  # Is only one device slow within a group (stripe, md, multipath): compare each device p95 to the median of the rest
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
      add("warn", tr("s.outlier", wd, f2(wv), f2(med)), msg("s.outlier.act"), "run")
      if (wv >= c3) outl = 4; else if (wv >= c2) outl = 3; else outl = 2
      if (outl > wrun) wrun = outl
    }
  }

  # ── Verdict ────────────────────────────────────────────────────────────
  lat = (rp > wp) ? rp : wp
  lowload = (ip < 50 && mp_ < 5)
  if (nr + nw == 0) latj = msg("s.lat.na")
  else if (lat >= c3) { latj = msg("r.0016"); add("crit", tr("s.lat.crit", f2(lat)), msg("s.lat.act_bad"), "run") }
  else if (lat >= c2) { latj = msg("r.0015"); add("warn", tr("s.lat.warn", f2(lat)), msg("s.lat.act_bad"), "run") }
  else if (lat >= c1) { latj = msg("r.0014"); add("caution", tr("s.lat.caution", f2(lat)), msg("s.lat.act_caution"), "run") }
  else latj = msg("r.0011")
  if (qd > 0 && qp >= 0 && qp / qd >= 0.8 && lat >= c1) add("warn", tr("s.queue", int(100 * qp / qd), f1(qp), qd), msg("s.queue.act"), "run")
  if (pp >= 20) add("warn", tr("s.psi", f1(pp)), msg("s.psi.act"), "run")
  else if (pp >= 5) add("caution", tr("s.psi", f1(pp)), msg("s.psi.act"), "run")
  if (dp >= 8) add("warn", tr("s.dstate", dp), msg("s.dstate.act"), "run")
  if (hasf && tf / tdt >= 1 && tft / tf >= c1) add((tft / tf >= c2) ? "warn" : "caution", tr("s.flush", f2(tft / tf), f1(tf / tdt)), msg("s.flush.act"))
  for (k in kl) if (kl[k] > 0) {
    sv = (k ~ /^(io_err|fs_err)$/) ? "crit" : (k == "timeout" ? "caution" : "warn")
    add(sv, tr("s.klog", msg("s.kl." k), kl[k]), msg("s.klog.act"), "run")
  }
  win = (ns >= 2) ? t[ns] - t[1] : 0
  for (d in ebsdev) if (win > 0) {
    # Larger of IOPS and throughput (same as the Python verdict)
    ev = ebsv[1, d, "vol", "i"] - ebsv[0, d, "vol", "i"]; x = ebsv[1, d, "vol", "t"] - ebsv[0, d, "vol", "t"]; if (x > ev) ev = x
    ei = ebsv[1, d, "inst", "i"] - ebsv[0, d, "inst", "i"]; x = ebsv[1, d, "inst", "t"] - ebsv[0, d, "inst", "t"]; if (x > ei) ei = x
    ev /= 1e6; ei /= 1e6
    if (ev >= 0.01 * win) add((ev >= 0.1 * win) ? "warn" : "caution", tr("s.ebs.vol", f1(ev), d, int(win)), msg("s.ebs.vol.act"), "run")
    if (ei >= 0.01 * win) add((ei >= 0.1 * win) ? "warn" : "caution", tr("s.ebs.inst", f1(ei), d, int(win)), msg("s.ebs.inst.act"), "run")
  }
  mmc = sysctl["vm.max_map_count"] + 0
  if (mmc > 0 && mmc < 262144) add("crit", tr("s.mmc", mmc), msg("s.mmc.act"))
  for (i = 1; i <= ndev; i++) {
    d = dev[i]; ra = attr[d, "queue/read_ahead_kb"] + 0
    if (ra > 128) add((ra >= 1024) ? "warn" : "caution", tr("s.ra", d, ra), tr("s.ra.act", d))
    sc = attr[d, "queue/scheduler"]; if (sc ~ /\[(cfq|bfq)\]/ && !(media == "hdd")) add("caution", tr("s.sched", d, sc), msg("s.sched.act"))
    stt = attr[d, "device/state"]; if (stt != "" && stt != "running") add("crit", tr("s.devstate", d, stt), msg("s.devstate.act"), "run")
    tmo = attr[d, "device/iotmo_cnt"]; if (tmo ~ /^0x/ && tmo != "0x0") add("caution", tr("s.iotmo", d, tmo), msg("s.iotmo.act"), "run")
    if (plat == "VMware Guest") { to = attr[d, "device/timeout"] + 0; if (to > 0 && to < 60) add("warn", tr("s.scsito", d, to), msg("s.scsito.act")) }
    c = d; sub(/n[0-9]+$/, "", c)
    if (d ~ /^nvme/ && nv[c, "temp"] != "" && nv[c, "temp_max"] != "" && nv[c, "temp"] + 0 >= nv[c, "temp_max"] + 0)
      add("warn", tr("s.nvmetemp", c, int(nv[c, "temp"] / 1000)), msg("s.nvmetemp.act"))
  }
  # swap: same rules as HTML. swap I/O during measurement, swap on + memory_lock off, swap on the data disk
  swmax = 0
  for (s = 2; s <= ns; s++) if (((s - 1) in vmsw) && (s in vmsw) && t[s] > t[s - 1]) { r_ = (vmsw[s] - vmsw[s - 1]) / (t[s] - t[s - 1]); if (r_ > swmax) swmax = r_ }
  if (swmax > 0) add("warn", tr("s.swapio", f1(swmax)), msg("s.swapio.act"))
  if (swaps > 0 && mlock != "true") {
    swp_ = sysctl["vm.swappiness"]
    add((swp_ != "" && swp_ + 0 <= 1) ? "caution" : "warn", tr("s.swapml", msg(mlock == "false" ? "s.swapml.off" : "s.swapml.unknown"), (swp_ == "" ? "-" : swp_)), msg("s.swapml.act"))
  }
  if (!guess) for (i = 1; i <= swaps; i++) {
    k = swdev[i]; if (k !~ /^\/dev\//) continue
    sub(/^\/dev\//, "", k); if (k ~ /^mapper\//) { sub(/^mapper\//, "", k); k = dmname[k] }
    sp = phys(k, 0); n_ = split(sp, spa, " ")
    hit_ = 0
    for (j = 1; j <= n_; j++) for (q_ = 1; q_ <= ndev; q_++) if (spa[j] != "" && spa[j] == dev[q_]) hit_ = 1
    if (hit_) add("caution", tr("s.swapdata", swdev[i]), msg("s.swapdata.act"))
  }
  for (i = 1; i <= ndp; i++) if (dmount[dpath[i]]) {
    o = mnt_opt[dmount[dpath[i]]]; fs_ = mnt_fs[dmount[dpath[i]]]
    if (o ~ /(^|,)(nobarrier|barrier=0)(,|$)/) add("warn", tr("s.barrier", dpath[i], o), msg("s.barrier.act"))
    if (o ~ /(^|,)(sync|dirsync)(,|$)/) add("warn", tr("s.syncmnt", dpath[i]), msg("s.syncmnt.act"))
    if (fs_ ~ /^(nfs|nfs4|cifs|tmpfs)$/) add("crit", tr("s.netfs", dpath[i], fs_), msg("s.netfs.act"))
  }
  if (thinmax >= 85) add((thinmax >= 95) ? "crit" : "warn", tr("s.thin", thinname, sprintf("%.0f", thinmax)), msg("s.thin.act"))
  else if (thinmax >= 70) add("caution", tr("s.thin", thinname, sprintf("%.0f", thinmax)), msg("s.thin.act_watch"))
  if (mddeg) add("warn", msg("s.md"), msg("s.md.act"))
  if (raid_vd) add("warn", msg("s.raidvd"), msg("s.raidvd.act"))
  if (raid_pd) add("warn", msg("s.raidpd"), msg("s.raidpd.act"))
  if (raid_bat) add("warn", msg("s.raidbat"), msg("s.raidbat.act"))
  if (raid_wt) add("caution", msg("s.raidwt"), msg("s.raidwt.act"))
  if (mdop) add("info", msg("s.mdop"), msg("s.mdop.act"))
  if (virt["cpu_governor"] ~ /^(powersave|conservative|ondemand)$/ && plat == "bare-metal") add("caution", tr("s.gov", virt["cpu_governor"]), msg("s.gov.act"))
  if (unsure && st == "auto") add("info", msg("s.unsure"), msg("s.unsure.act"))

  # Noisy neighbor processes
  esp = meta["es_pid"]; tot = 0; top1 = ""; topv = 0
  for (pid in seen) { v = pio[1, pid] - pio[0, pid]; if (v <= 0 || !((0, pid) in pio)) continue; tot += v; if (pid == esp) esv = v; else if (v > topv) { topv = v; top1 = comm[pid] "(pid " pid ")" } }
  if (tot >= 52428800 && esp != "" && (tot - esv) / tot >= 0.3 && !lowload) add("caution", tr("s.neighbor", int(100 * (tot - esv) / tot), top1, int(topv / 1048576)), msg("s.neighbor.act"))

  # ── Output ────────────────────────────────────────────────────────────
  if (wrun >= 3) verdict = msg("r.0946")
  else if (lowload && wrun <= 2) { verdict = msg("r.0950"); vnote = msg("s.hold_note") }
  else if (wcfg >= 3 || wrun == 2) verdict = msg("r.0952")
  else verdict = msg("r.0954")
  mlab["nvme"] = "NVMe"; mlab["ssd"] = "SSD"; mlab["hdd"] = "HDD"; mlab["vmware"] = msg("r.0002"); mlab["allflash"] = "vSAN All-Flash"
  mlab["hybrid"] = "vSAN Hybrid"; mlab["vmfs"] = msg("r.0003"); mlab["vm"] = msg("r.0001"); mlab["network"] = msg("s.label.network"); mlab["cloud"] = msg("s.label.cloud")
  printf "%s\n", msg("s.head")
  printf "%s\n", tr("s.host", meta["host"], plat, devs, (guess ? msg("s.host.guess") : ""))
  cont = virt["detect_virt_container"]; if (cont == "" && virt["detect_virt"] ~ /^(docker|podman|lxc|lxc-libvirt|systemd-nspawn|openvz|rkt|wsl|proot|pouch|container-other)$/) cont = virt["detect_virt"]
  if (cont != "" && cont != "none") printf "%s\n", tr("s.container", cont)
  printf "%s\n", tr("s.basis", (media in mlab) ? mlab[media] : media, (unsure ? msg("s.basis.guess") : ""), c1, c2, c3)
  printf "\n%s\n", tr("s.verdict", verdict)
  if (vnote != "") printf "      %s\n", vnote
  printf "\n"
  printf "%s\n", tr("s.l.lat", f2(rp), f2(wp), latj)
  printf "%s\n", tr("s.l.load", f1(ip), f1(mp_), f1(qp), (qd > 0 ? " / queue_depth " qd : ""), f1(up))
  if (hasf && tf > 0) printf "%s\n", tr("s.l.flush", f1(tf / tdt), f2(tft / tf))
  printf "%s\n", tr("s.l.sat", (np_ ? f1(pp) : "-"), (nds ? dp : "-"))
  if (nf) {
    printf "\n%s\n", msg("s.items")
    for (r = 4; r >= 1; r--) for (i = 1; i <= nf; i++) if (rank[FS_[i]] == r) printf "  [%s] %s\n         → %s\n", msg(SEVK[FS_[i]]), FT[i], FA[i]
  } else printf "\n%s\n", msg("s.noitems")
  printf "\n%s\n", msg("s.more")
}' "$B/meta" "$S/virt" "$S/sysctl" "$S/sysfs" \
   $( [[ -r "$S/datadev" ]] && echo "$S/datadev" ) $( [[ -r "$S/data_paths" ]] && echo "$S/data_paths" ) \
   "$S/mounts" "$S/swaps" "$S/klog_io" \
   $( [[ -r "$S/dmsetup_status" ]] && echo "$S/dmsetup_status" ) $( [[ -r "$S/mdstat" ]] && echo "$S/mdstat" ) \
   $( [[ -r "$S/storage" ]] && echo "$S/storage" ) \
   $( for r in raid_storcli raid_ssacli raid_arcconf; do [[ -r "$S/$r" ]] && echo "$S/$r"; done ) \
   $( [[ -r "$S/procio_start" ]] && echo "$S/procio_start" "$S/procio_end" ) \
   $( [[ -s "$S/ebs_stats_start" && -s "$S/ebs_stats_end" ]] && echo "$S/ebs_stats_start" "$S/ebs_stats_end" ) \
   $( [[ -r "$S/es_nodeinfo.json" ]] && echo "$S/es_nodeinfo.json" ) \
   "$B/samples.raw" 2>/dev/null | tee "$B/summary.$LNG.txt"
