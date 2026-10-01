# es-disk-probe

English | [한국어](README.ko.md)

Read-only disk I/O diagnostic tool for Elasticsearch nodes (VMware vSAN guests, bare-metal, SAN, other hypervisors, OS level)

It tells you, from the OS, whether the disks on an Elasticsearch node are healthy right now and, if not, where the cause is.
On a VMware guest it separates inside the VM from outside the VM. On bare-metal it separates load saturation from disk or controller faults. On SAN it separates the server from the storage array.
The platform is detected automatically on the server where it runs. Measurements are checked against Elastic, VMware and Red Hat official recommendations, and the result is an HTML report with actions for each owner.

v0.11.1 · Unofficial tool · Read-only · Korean and English reports · Point-in-time diagnostic (not a continuous monitoring tool)

---

## Read this first

### What this tool is

This is not an official Elastic or VMware product. It is a set of diagnostic scripts written for real field work.

The thresholds the report cites come from Elastic, VMware and Red Hat official documentation, and each item lists its source.
The verdicts and actions built on top of those thresholds are this tool's interpretation.
Every item in the report carries one of `[Elastic official]`, `[VMware official]`, `[Red Hat official]`, `[Field practice]` or `[Reference]`.
When you pass results to a customer, present the items tagged official as recommendations and treat the rest as review comments.

### It looks at one point in time

It does not replace continuous monitoring. The two do different jobs.

| | This tool | Continuous monitoring (Elastic System/Linux integration) |
|---|---|---|
| Nature | Run once, get one report | Collects continuously, time series and alerts |
| How it runs | Run by hand when needed (5 minutes by default) | Resident agent |
| Strength | Goes deep on that moment (p95, every setting, bottleneck location) | Long-term trends, comparison across nodes, automatic alerts |
| What it cannot see | Anything outside the measured window | Short latency spikes, setting values, kernel log correlation |

Do not schedule it with cron or a systemd timer. The measurement window is short, so it cannot show trends,
and you only pile up disconnected reports. For always-on monitoring, use Elastic Agent.
Alert thresholds that match this tool's criteria are in section 4 of `GUARDLINE.md`.

The report is also a snapshot of that moment. Measure again after you change settings or when the load changes.

### When to use it

Good fits:

- A disk-related incident or alert fired on an ES node, and you need to tell whether the cause is inside the VM or on the vSAN or host side
- On a bare-metal node, you need to tell whether the load exceeded what the disks can do, or whether a disk or RAID controller is faulty
- On a SAN-attached node, you need to tell whether the problem is on the server side (HBA queue, paths) or in the storage array
- You need to confirm whether indexing or search latency comes from the disk
- You need to bring measured evidence, not just "it's slow", to the VMware admin, the storage admin or hardware maintenance
- Right before a new build or expansion, you want one pass over OS settings and server or VM configuration against recommendations (the performance verdict needs real load)
- The network is air-gapped and bringing in outside tools is hard

Do not use it for:

- Tuning after you have already confirmed the disk is the cause. This tool stops at isolating the cause
- Always-on monitoring. See the table above
- Capacity sizing evidence. It does not load the disk to measure peak performance. It only measures the real production load
- Performance problems outside the disk. CPU, heap, GC and query tuning are out of scope. It does tell you when the disk is not the cause
- Running inside a container (ECK, Docker). Run it on that node (the host). On the host it finds ES inside containers automatically
  and follows the ES process mount info to the device holding data (local PV, Ceph RBD, cloud volumes and so on)

### What it does and does not do on the server

It does not:

- Change system settings. It touches no sysctl, /sys, mount options or ES settings
- Send write requests to ES. It uses read APIs (`GET`) only. No index creation, no setting changes, no restarts
- Run `drop_caches`, force `sync`, or access raw devices
- Talk to external networks. It connects to nothing except the ES address you give it
- Run disk load tests. A production server already has real load, so it measures that as is
- Delete files. It deletes no file on the server, including its own output files

It does:

- Read `/proc` and `/sys`, call ES read APIs, and write files in one place only: the output location (default `/tmp`, moved to another disk automatically if it is on the same disk as ES data). It creates two things there: the result directory `esdisk_<host>_<time>/` and the bundle `esdisk_<host>_<time>.tar.gz`
- Read the tail of the ES server logs and the sar records (see the load table below)
- Lower its own priority with `renice 19` and `ionice idle`. This ends with the process
- Read NVMe temperatures. Reading `/sys/class/nvme/*/hwmon` makes the kernel request the SMART log from the device once. It is a read-only command, once per run

On bare-metal it queries hardware status automatically. On a VM it skips this, since the devices are virtual and the result means nothing.
All of these are query (show) commands, at the same level as what smartd or monitoring agents (such as the Prometheus storcli exporter) run periodically.
Turn them off with `--no-hw`.

| Query | Command | Condition |
|---|---|---|
| SMART | `smartctl -H -A -i -n standby` (does not wake sleeping HDDs, 15 s cap per device) | smartmontools installed, disk not behind a RAID controller |
| Broadcom and Dell RAID | `storcli64` or `perccli64` with `/call show all J`, `/call/vall show all J`, `/call/eall/sall show all J`, `/call show patrolread J`, `/call show cc J` | megaraid_sas or mpt3sas driver, tool installed |
| Broadcom MegaRAID 96xx and Dell PERC 12 and later | `storcli2` or `perccli2` with `/call show all J`, `/call/vall show all J`, `/cN/eall/sall show all J`, `/cN/sall show all J`, patrolread and cc | mpi3mr driver, tool installed. The JSON keys are not confirmed by public documentation, so when parsing fails it keeps the raw output in the bundle and reports it |
| HPE RAID | `ssacli ctrl all show config detail` (SR controllers). HPE MR controllers use storcli | hpsa or smartpqi driver, tool installed |
| Microchip and Adaptec RAID | `arcconf getconfig <n> AL` | aacraid or smartpqi driver, tool installed |
| AWS EBS limit exceeded | `nvme amzn stats` (nvme-cli amzn plugin) or `ebsnvme stats -j`, twice: at collection start and end | EBS NVMe volumes, tool present (included by default on Amazon Linux) |

Some vendor tools leave log files (storcli.log, UcliEvt.log) in the directory they run from, so the tool runs them in `hw_tool_logs/` inside the output directory and keeps those logs in the bundle instead of deleting them.
Each command has a 30 s cap. If a tool is missing, it is skipped and the report notes that installing it lets the tool check cache and battery automatically.

### What the bundle contains

Check this before the bundle leaves the customer's network. Everything is plain text or JSON, so you can open and review any file before sending it.

| Contains | Examples |
|---|---|
| Server identity | Hostname, kernel and OS version, CPU and memory size, block device and mount layout, network interface names and error counters |
| Elasticsearch metadata | Cluster and node names, node IPs and roles, index names with their size and write counts, ILM phases, disk-related cluster settings |
| Log excerpts | Kernel log lines about disks and controllers (last 7 days), ES server log lines about throttling, watermarks and flush failures |
| Process information | Process names and their I/O byte counters. The ES process command line (JVM options) |
| Hardware status | SMART attributes, RAID controller configuration and event state, serial numbers as the vendor tools print them |

It does not contain:

- Passwords or API keys. Credentials are passed to curl through standard input and never written to a file.
- `elasticsearch.yml` lines containing password, secret, token or key. Only path, role, routing and store settings are kept.
- Secret values in the ES command line. Any JVM option whose name looks like a secret (password, secret, token, api key, credential, private) is saved as `name=***`.
- Document contents or query text. It reads statistics APIs only.

If hostnames, IPs or index names must not leave the site, run the analyzer on a PC inside the network and pass on only the HTML report.
The report shows the same names, so review it before sending.

### After collection

The tool deletes nothing, so the result directory and the `.tar.gz` stay in the output location.
Copy the bundle off the server, then remove both yourself:

```bash
ls -d /tmp/esdisk_*            # or the location shown at the end of the run
rm -rf /tmp/esdisk_<host>_<time> /tmp/esdisk_<host>_<time>.tar.gz
```

On a PC, `es_disk_render.py` unpacks a `.tar.gz` bundle into the system temp directory and leaves it there. Delete it when you are done if needed.

---

## Load this tool puts on the server

This tool runs on production nodes, so its load was measured directly.
Values are for the defaults (300 s measurement, 5 s interval), against a directory holding 530MB of ES logs,
with the page cache emptied first.
Measurements use `getrusage(RUSAGE_CHILDREN)`: CPU and max RSS including child processes,
plus `ru_inblock` and `ru_oublock` (actual block layer I/O, excluding page cache hits).

| Item | Default mode | `--light` | Notes |
|---|---|---|---|
| CPU | 0.97 s | 0.62 s | 0.32% of one CPU over the 300 s measurement |
| Disk reads | 12.0 MB | 0 MB | All from ES server logs. Zero during sampling |
| Disk writes | 0.39 MB | 0.18 MB | Output files (`-o` location) |
| Memory (max RSS) | Collector 4.2 MB | 4.2 MB | The python process that builds the report uses 18.5 MB separately |
| ES queries | Up to 19 GETs | Up to 15 GETs | 0 write requests. `--light` skips the 4 per-index queries |

Sampling does not read the disk. Every 5 s it reads `/proc/diskstats`, `/proc/stat`, `/proc/pressure/io`,
ES threads' `/proc/<pid>/task/*/stat` and similar files. The kernel builds these values in memory,
so they cause no block layer I/O. Each sample starts only one `awk` and one `sleep` process
and costs about 5ms of CPU. Raising the measurement time from 300 s to 600 s adds only about 0.3 s of CPU, and disk reads stay the same.

The 12MB of disk reads is all ES server logs. It reads the last 8MB of the newest log and the last 2MB of each of the two logs before it,
up to 12MB in total, looking for throttle, watermark and flush failure records.
Whatever it reads pushes the same amount out of the page cache. On an ES node, what gets pushed out is segment cache,
so ES later reads that much more from disk. That is why there is a cap, and you can lower it further.

| Option | Effect | What you lose |
|---|---|---|
| `--no-eslog` | Disk reads from 12MB to 0 MB | Findings based on ES logs (throttle, watermark records) |
| `--light` | The above + skips reading the kernel log, sar and maps, and the per-index ES queries | Traces of past incidents, 7-day history, mmap headroom finding, per-index write distribution |
| `--no-index-stats` | Skips the 4 ES queries that grow with the index count (per-index stats at start and end, index settings, ILM) | Per-index write distribution, ILM phase |
| `ESLOG_TAIL_MB=2` | Log reads from 12MB to 6MB | Older log events |

The load on ES is 19 GETs in total, all monitoring queries. There are no write requests.
Three of them get more expensive as the cluster grows.

| Query | Calls | Cost grows with |
|---|---|---|
| `_nodes/_local/stats?level=indices` | 2 (start and end) | Number of indices on this node |
| `_ilm/explain?only_managed=true` | 1 | Number of ILM-managed indices |
| `_nodes/stats` (whole cluster) | 2 | Number of nodes |
| The other 14 (`_cat/*`, `_cluster/health`, `_snapshot/_status` and so on) | 1-2 each | Negligible |

With many shards (2,000 or more on this node, or 20,000 or more in the cluster), the queries that grow with the index count are dropped automatically.
These limits are field practice. To drop them on smaller clusters too, use `--no-index-stats`. To skip all cluster queries, use `--no-cluster`.
`_snapshot/_status` is called without arguments, so it is the light form that only shows running snapshots,
not the heavy form that reads the repository.

Only one action touches the ES process directly: it reads `/proc/<pid>/maps` once per run.
On a process with 40,000 mappings this took about 17ms, and during that time it holds that process's `mmap_lock` in read mode.
The only effect is brief contention when ES opens or closes segments (`mmap`, `munmap`). To avoid even this, use `--light`.
The other reads, `/proc/<pid>/io` and `/proc/<pid>/task/*/stat`, take no lock.

The output location must be on a different disk from ES data. On the same filesystem, it adds writes to the disk being measured
and skews the measurement by that much. Without `-o`, the tool picks a disk other than the ES data disk.
If the location you give with `-o` is on the same disk, it warns you and marks it in the report.

Every run records its own measured load in the report section "Load this diagnostic put on the server".
It is there so whoever receives the report can check it.

For your first run, try it on an internal or development node and check the output.


| Principle | Implementation |
|---|---|
| Automatic platform detection | Tells VMware, bare-metal (local or SAN), and other hypervisors or clouds apart, and switches thresholds and owners accordingly. [Reading the verdict](#reading-the-verdict) |
| Does not change the system | Only reads `/proc` and `/sys` and calls ES read APIs (GET). Writes only the result directory and its `.tar.gz` in the output location |
| No service impact | Measured CPU 0.97 s (0.32% of one CPU over a 300 s measurement), 4.2MB memory, 12MB disk reads, `nice 19` + `ionice idle`. [Measured load details](#load-this-tool-puts-on-the-server) |
| Adds no load | No load-test tool. Measures the real production load. When load is low, it holds the performance verdict itself |
| Deletes nothing | No script contains a delete operation (`rm` and similar). Logs left by vendor tools stay in the output directory and go into the bundle |
| Actions are advice only | No automatic tuning. It gives evidence, why it matters, how to fix it and the source. The owner decides whether to apply it |
| Works air-gapped | No external packages or CDN. bash + awk + coreutils, and the analyzer uses the Python 3.6 standard library |

The one exception is that the collector lowers its own priority with `renice` and `ionice`.
This goes away when the process ends.

---

## Quick start

```bash
git clone https://github.com/Munsik/es-disk-probe.git
cd es-disk-probe
chmod +x *.sh

# Run on a data node during peak hours (300 s by default, 5 s interval). No options needed
sudo ./es_disk_collect.sh
```

If ES requires authentication, it prompts for a user and password. Read-only privileges (`monitor`) are enough.
When it finishes, the summary verdict built by the shell (awk) prints right away. This also works on servers without Python 3 (RHEL 7 and so on).

```
=== es-disk-probe summary verdict (shell) ===
Host es-warm-01 · bare-metal · Target disks sdb
Thresholds SSD (estimated) · Latency Caution 3 / Warning 6 / Critical 15 ms

Verdict: Signs of disk performance degradation

Latency p95   read 1.20 ms · write 2.00 ms  → OK
Load p95      IOPS 2700.0 · 49.2 MB/s · aqu-sz 3.0 / queue_depth 256 · %util 70.0
Flush         40.0 per second · avg 9.00 ms
Saturation    PSI io full p95 0.4% · ES D state threads p95 0

Items to check (most severe first)
  [Warning] flush (device cache flush) avg 9.00 ms, 40.0 per second
         → Check for SSDs with power-loss protection and a battery-backed RAID cache
  [Warning] Kernel log: RAID controller events (1 in the last 7 days)
         → Send the timestamps from static/klog_io in the bundle to the owner
  [Warning] LVM thin pool vg-pool-tpool data 92% used
         → Extend the pool (lvextend) or free up space. Writes stop when it is full
  [Caution] Non-ES processes account for 62% of disk I/O (top: backup-agent(pid 777) 501MB)
         → Check whether that process uses the ES data disk
```

The same content is saved in the bundle in two languages: `summary.ko.txt` and `summary.en.txt`.
If the server has Python 3.6+, it also builds the HTML report in both languages (`es_disk_report.ko.html`, `es_disk_report.en.html`) and adds it to the bundle.

### Language

Screen output is Korean if the server locale (`LC_ALL`, `LC_MESSAGES`, then `LANG`) starts with `ko`, and English otherwise. Change it with `--lang ko|en`.
The bundle always contains the summary and HTML in both languages, so a bundle collected in Korea can go straight to a team abroad.
To rebuild with the analyzer, use `python3 es_disk_render.py <bundle> --lang ko|en|auto|both` (default both).
All screen text lives in `i18n/ko.txt` and `i18n/en.txt`, and both languages use the same code for the verdict rules.
The HTML report adds the bottleneck location finding, the cluster comparison, the per-index distribution, and evidence and sources for every item.

### What it decides for you

The tool decides the following automatically, so you do not have to. Use the options only when the automatic choice is wrong.

| Item | What it does automatically | Option to override |
|---|---|---|
| Platform | Detects VMware, bare-metal (local or SAN), and other VMs or clouds | `--platform` |
| Threshold media | Detects NVMe, SSD, HDD, media behind RAID (via the controller tool), SAN, Ceph RBD and cloud volumes. The VMware datastore type cannot be seen from the guest, so the common threshold is the default there | `-s` |
| Hardware status | On bare-metal, queries SMART and RAID controllers (storcli or perccli, ssacli, arcconf). Only the tools that are installed | `--no-hw` |
| ES in a container | When run on the host, finds ES inside the container and follows its mount info to the data device | `-p` |
| ES address | Finds the ports the ES process actually listens on and tries http, then https. Also works when `network.host` is bound to an IP | `--es-url` |
| ES authentication | On 401, prompts for user and password in the terminal. The password does not end up in shell history | `--es-user` + `ES_PASSWORD`, `ES_API_KEY` |
| ES data path | Found from the ES process and `path.data` in `elasticsearch.yml` (even when ES is down) | `-p` |
| Output location | If `/tmp` is on the same disk as ES data, switches to another disk such as `/var/tmp` or `/root` | `-o` |
| Per-index queries | Skipped at 2,000 or more shards on this node, or 20,000 or more in the cluster | `--no-index-stats` |
| Cluster queries | Without the privilege (403), builds the report from this node's results only | `--no-cluster` |
| Report generation | Builds the HTML right away if the server has Python 3.6 or later (including RHEL 8 platform-python) | `--no-render` |

If the server has no `python3`, copy the bundle to a PC and build it there.

```bash
python3 es_disk_render.py esdisk_es-hot-01_20260923_142031.tar.gz
```

---

## Requirements

| Area | Details |
|---|---|
| OS | RHEL / CentOS / Rocky 7, 8, 9, Ubuntu 20.04+ |
| Collector and summary verdict | bash 4.2+, awk (gawk or mawk), coreutils, util-linux, procps. `curl` only for ES queries. All installed by default on the OS |
| HTML analyzer | Python 3.6+ standard library only (detects RHEL 8 `/usr/libexec/platform-python` automatically). If the server has none, copy the bundle to a PC and run it there |
| Privileges | root recommended (ES process I/O, kernel log, VMware details) |
| ES privileges | `cluster monitor` (`monitoring_user` level). No admin account needed |
| Elasticsearch | Run against 8.19 on a real node. Thresholds checked against the documentation up to 9.5. It uses only the stats, `_cat` and health APIs, which 7.x also has, but 7.x and 8.x before 8.19 are not tested. Elastic Cloud Hosted and Serverless are out of scope (no OS access) |
| Optional (more detail when present) | `sysstat` (history), `ethtool` (NIC ring), `open-vm-tools` (VMware resources), `smartmontools` (SMART), RAID controller tools (`storcli` or `perccli`, `ssacli`, `arcconf`) |

### Commands it uses on the server

Every verdict works at its basic level with none of the optional tools. The optional tools only add detail on the same items.

| Command | Package | Installed by default | Used for |
|---|---|---|---|
| bash, awk, grep, sed, sort, find, stat, df, tar, timeout, date | bash, gawk/mawk, coreutils, findutils, tar | Yes | Collection, summary verdict |
| lsblk, lscpu, ionice, dmesg | util-linux | Yes | Device layout, lowering priority, kernel log |
| pgrep, sysctl, renice | procps | Yes | ES process, kernel settings |
| journalctl, systemd-detect-virt | systemd | Yes | Kernel log, platform detection |
| dmsetup | device-mapper (lvm2) | Yes (RHEL, Ubuntu server) | LVM, multipath and thin pool layout and state |
| curl | curl | Yes | ES read APIs |
| python3 | python3 / platform-python | Yes on RHEL 8 and 9 and Ubuntu. No on RHEL 7 | HTML report (shell summary only without it) |

`es_disk_collect.sh` and `es_cluster_probe.sh` run with `curl -k` by default.
This default allows for self-signed certificates. The collector only talks to localhost, so leaving it as is causes no problem.
If you need certificate verification in `es_cluster_probe.sh`, which queries remotely, use `--strict-tls` or `--cacert <file>`.

---

## Options

```
-d SEC        Measurement duration (default 300)
-i SEC        Sample interval (default 5)
-p PATH       ES data path (repeatable. Detected automatically if omitted)
-o DIR        Output location (default /tmp. Moved to another disk automatically if it shares a disk with ES data)
-s TYPE       Storage type (default auto)
              VMware: allflash | hybrid (vSAN), vmfs (SAN or NFS datastore)
              bare-metal and SAN: nvme | ssd | hdd  (set this when no RAID controller tool can read the media)
--platform P  Force the platform: auto | vmware | baremetal | vm (default auto)
--no-hw       Turn off hardware status queries (SMART and RAID controller queries done automatically on bare-metal)
--smart       Read SMART on VMs as well (usually not needed)
--es-url URL  ES address (default: detected automatically)
--es-user U   ES user (password in the ES_PASSWORD environment variable, API key in ES_API_KEY)
--no-es       Skip ES API queries
--no-cluster  Skip cluster-wide queries (this node only)
--no-render   Skip HTML generation (bundle only)
--lang L      Screen output language ko | en (default: locale). Summary and HTML in the bundle are always in both languages

Options that reduce load (see "Load this tool puts on the server" above)
--light           Skip all extra collection that reads the disk, and the per-index ES queries. Zero disk reads
--no-eslog        Skip reading the ES server logs only. Most of the read volume
--no-index-stats  Skip ES queries that grow with the index count (skipped automatically with many shards)
```

Environment variables allow finer control:
`ESLOG_TAIL_MB` (read cap for the newest log, default 8), `ESLOG_OLD_MB` (previous logs, default 2),
`ESLOG_MAX_FILES` (number of files, default 3), `KLOG_MAX_LINES` (kernel log line cap, default 20000).

If cluster queries fail, it only logs a warning and builds the report from local results.

`-s` and `--platform` also exist in the analyzer (`es_disk_render.py --storage ... --platform ...`).
You can rebuild the report with different thresholds without collecting the bundle again.

---

## Components

| File | Role | Server impact |
|---|---|---|
| `es_disk_collect.sh` | All collection. Local + cluster + per-index distribution | Read-only. No setting changes |
| `es_disk_summary.sh` | Core verdict summary using the shell (awk) only. Runs automatically when the collector finishes, `summary.ko.txt` and `summary.en.txt` | Reads the bundle only |
| `es_disk_render.py` | Full analysis, verdict and HTML generation (server or PC) | Does not need to run on the server |
| `es_cluster_probe.sh` | (Optional) Remote cluster-only query, without logging in to nodes | Read API GETs only |
| `i18n/ko.txt`, `i18n/en.txt` | Screen text catalogs (`key = "text"`). Shared by the three scripts and the analyzer | Text only |
| `GUARDLINE.ko.md` / `GUARDLINE.md` | Design, layout, continuous monitoring thresholds and change principles (Korean / English) | Documentation |
| `docs/STYLE.md` | Writing rules and the Korean and English glossary | Documentation |
| `tests/` | Synthetic bundle generator and verdict tests (32 scenarios × two languages), catalog checks | Not run on the server |

---

## Everything it checks automatically

It checks the items below so that one run can answer "in any environment, how heavy is the disk load, what is wrong, and who should fix it and how".
Sources not in bold are all standard OS tools and `/proc` or `/sys`.

| Area | Checks | Source |
|---|---|---|
| Load level | Latency p95 (read and write), IOPS, throughput, request size, merge ratio, queued I/O (aqu-sz), inflight, %util | /proc/diskstats |
| Saturation | PSI io some/full, ES threads in D state, iowait, queue utilization (aqu-sz ÷ queue_depth), limit-hit pattern (flat IOPS and throughput + rising queue) | /proc/pressure, /proc/&lt;pid&gt;/task, /sys/block |
| fsync cost | Flush request count and average time (kernel 5.5+), ES flush average time | /proc/diskstats, ES node stats |
| Cause location | Bottleneck location (per platform), only writes slow, only one device in a group slow, several nodes under heavy load at once | Combination of the metrics above, _nodes/stats |
| Noisy neighbors | Top non-ES processes by disk I/O | /proc/&lt;pid&gt;/io |
| Errors | Kernel log (I/O error, abort/reset, timeout, hung task, FS errors, controller and PCIe, RAID controller events, multipath, md, thin pool), device state, timeout and error counters, PCIe AER, NVMe controller state | journalctl/dmesg, /sys |
| Hardware | RAID level (hpsa, smartpqi), raid_class volume state, controller firmware crashes, md degraded, resync and mismatch, NVMe temperature and PCIe link, CPU governor, SMART, controller cache, battery and member drives | /sys, /proc/mdstat, **smartctl**, **storcli, perccli, ssacli, arcconf** |
| Block device settings | readahead, scheduler, iostats, wbt, SCSI timeout, queue_depth, partition alignment, reported write cache | /sys/block |
| Storage layout | LVM linear and stripe, thin pool usage, LVM snapshots, dm-crypt, dm-cache, multipath, whether it shares a disk with the OS, swap, the snapshot repository or logs | dmsetup table/status, /proc/swaps, ES settings |
| Filesystem | Type (NFS and others), atime, discard, barrier, sync, data=journal, usage and watermark, inodes | /proc/mounts, df |
| Memory | Swap usage and settings, heap share, page cache headroom, major faults, dirty pages, VMware balloon and host swap | /proc/meminfo, /proc/vmstat, **vmware-toolbox-cmd** |
| ES | Indexing throttle, write and search rejections, indexing pressure, flush, refresh and merge time, translog durability, merge threads, store type, file handles, mmap headroom, throttle, watermark and flush failures in the ES logs | ES read APIs, /proc/&lt;pid&gt;, ES logs |
| Cluster | Skew between nodes (same tier), watermark proximity, recovery, relocation and snapshot progress, awareness, shard skew, write concentration by index, ILM phase | ES read APIs |
| Platform | VMware (controller, NIC, reservation and limit), other VMs (steal), cloud volumes, Ceph RBD, ES in a container | systemd-detect-virt, DMI, mountinfo |
| History | Latency history for the last 7 days | **sar** (what sysstat has already recorded) |

## What one run gives you

| View | Contents |
|---|---|
| Local (kernel) | Latency p95, aqu-sz, PSI, D state, every setting, kernel log, VMware resources or hardware status |
| Cluster | Disk usage compared across nodes, shard and capacity skew, cluster-driven load such as recovery and snapshots |
| Index | Write, merge and search distribution by index for this node's shards, ILM phase |

The cluster snapshot is taken over the same window as the local measurement.

Cross-checks catch these cases:

- The disk is not slow, but shards are concentrated on this node
- Several nodes slowed down at the same time, not just one (suspect shared storage)
- Writes are concentrated on one index

---

## Report layout

The top shows a one-sentence verdict and a 9-item summary (Latency, Saturation, Errors, Elasticsearch impact, Memory and cache, Configuration, Platform resources,
Network, Cluster), followed by up to 3 "do this first" items, each with its owner.
Then, in this order:

1. Key numbers and timeline. Latency, IOPS, aqu-sz and PSI charts (no external CDN)
2. Evidence and actions. Grouped by owner, with evidence, why it matters, action and source
3. Best practice checklist. Every item, including the ones that pass (about 30)
4. Elasticsearch impact. How the metrics changed over the measurement window
5. Estimated ceiling. Theoretical queue-based ceiling and current utilization, calculated without a load test
6. Cluster view, and the indices using this node's disks
7. Per-device detail, 7-day history (sar)
8. Load this diagnostic put on the server. Measured values for that run
9. What was measured, how, and what this cannot see. How it differs from continuously collected metrics, and what a guest cannot see
10. Appendix. Raw kernel and memory settings, kernel log excerpts

Sections without data (no cluster query, no sar records) are left out.

Sample reports (English `.en.html`, Korean `.ko.html`): `docs/sample_node_report` (VMware guest), `docs/sample_baremetal_report` (bare-metal, HDD RAID5 where a CacheVault fault dropped the write cache to write-through),
`docs/sample_cluster_report` (remote cluster query). All use synthetic data.

The record of checking thresholds against the latest documentation, and the remaining update items, are in `docs/UPDATE_NOTES.md`.

---

## Reading the verdict

| Verdict | Meaning |
|---|---|
| Disk is healthy | Latency, saturation and errors are all within thresholds, and there are no unfavorable settings |
| Holding up for now, but risk factors exist | Measurements are fine, but some settings will cause trouble if load grows or the host comes under contention |
| Elasticsearch shows processing delays, but disk latency is normal | The cause is likely outside the disk (CPU, heap, bulk, shards) |
| Signs of disk performance degradation | Disk latency, saturation or errors were observed during the measurement |
| Configuration checked, performance verdict on hold | Load was too low during the measured window to judge. Measure again during indexing or search peaks |

The bottleneck location is judged only when latency exceeds its threshold.
Queue utilization (aqu-sz ÷ sum of queue_depth) splits it into three ranges. The ranges are the same everywhere. What each range means and who owns it depends on the platform.

| Queue utilization | VMware guest | bare-metal local | bare-metal SAN |
|---|---|---|---|
| 80% or more | Guest-side queue is full. System admin: split VMDKs + separate PVSCSI controllers, raise queue depth | The disk layout is at its concurrency limit. System admin: add disks and stripe, faster media | Server LUN queue is full. System admin: split LUNs and stripe, check paths |
| 40-80% | Not conclusive. Check esxtop DAVG and KAVG separately | Not conclusive. Check kernel log, SMART and controller events | Not conclusive. Compare with latency on the array side |
| Below 40% | Outside the VM. VMware admin: vSAN, host contention, resync | The disk or controller itself is slow. Hardware team: bad disk, RAID rebuild, cache and battery | Array or SAN path. Storage admin: array load, port errors |
| No queue_depth | Measure again as root | For NVMe, guidance uses temperature, link and SMART instead of the queue | Measure again as root |

Other VMs (KVM, Hyper-V, cloud) are judged the same way as VMware, without naming the backend.
For virtual disks with no queue_depth, such as virtio-blk, the guidance states that this is not a privilege problem.

`aqu-sz` is a time average that includes requests waiting in the block layer, so it can exceed queue_depth.
The report therefore also shows the number of I/Os actually handed to the device (`inflight`), and treats the queue as saturated if either one exceeds 80%.
The 40% and 80% range boundaries are field practice, not official figures.

---

## Threshold sources

Every item in the report shows its source level.

| Item | Threshold | Source |
|---|---|---|
| readahead | 128KiB (can grow to several MiB with LVM or RAID) | [Elastic official] Tune for search speed |
| vm.max_map_count | Minimum 262144, recommended 1048576 (from 8.16) | [Elastic official] Bootstrap checks |
| swap | Disabled > memory_lock > swappiness=1 | [Elastic official] Disable swapping |
| JVM heap | Automatic sizing recommended. If set by hand, 50% of RAM or less and under the compressed oops limit (26GB in most cases, 30GB on some). Judged from whether the node reports compressed oops in use | [Elastic official] JVM settings |
| File handles | 65535 or more | [Elastic official] File descriptors |
| disk watermark | Default 85/90/95%. If the ratios are not set explicitly, max_headroom (200/150/100GB) also applies when calculating the actual boundary (8.5+) | [Elastic official] Cluster-level shard allocation and routing settings |
| Storage type | Directly attached local storage is generally faster [Elastic official] Tune for indexing speed. A data path on a network filesystem (NFS, SMB) is rated Critical | [Field practice] |
| translog durability | Default request fsyncs on every request, async fsyncs every sync_interval (default 5s) | [Elastic official] Translog settings |
| Merge thread count | Default is half the processor count (max 4 on 9.3 and earlier and on 8.x), lowered to 1 on spinning disks | [Elastic official] Merge settings |
| index.store.type | Default hybridfs | [Elastic official] Store |
| Storage for indexing | SSD recommended, RAID 0 stripe, avoid remote storage | [Elastic official] Tune for indexing speed |
| vSAN latency | The vSAN performance view treats under 5ms (flash) / 20ms (hybrid) as normal (vSAN 7 and 8. No separate figure for ESA) | [VMware official] Broadcom KB 389082 |
| vSAN latency (device view) | NVMe under 0.5ms, SSD 1ms or less, HDD 10-20ms | [VMware official] Broadcom KB 424485 |
| bare-metal and SAN latency (Caution line) | NVMe above 1ms, enterprise SSD above 3ms, HDD above 25ms | [VMware official] Per-device alert thresholds from Broadcom KB 424485. The KB gives HDD above 30ms as critical |
| bare-metal and SAN latency (Warning and Critical) | NVMe 3/10ms, SSD 6/15ms, HDD 30/50ms | [Field practice] |
| Other VM latency | 5 / 10 / 20ms | [Field practice] Borrows the KB 389082 flash figure as the common threshold |
| I/O scheduler (bare-metal) | High-performance SSD and NVMe none/kyber, traditional HDD mq-deadline/bfq | [Red Hat official] Disk schedulers for different use cases |
| tuned profile (bare-metal) | throughput-performance. Selected automatically for compute nodes at install, turns off power saving | [Red Hat official] TuneD profiles |
| Merge threads (bare-metal HDD) | `max_thread_count` 1 on spinning disks | [Elastic official] Merge settings |
| Local vs remote storage | Directly attached local storage is generally faster, and some remote storage is very slow under ES load | [Elastic official] Tune for indexing/search speed |
| NVMe temperature | Warning when hwmon temp1_max (current overheat threshold, WCTEMP by default) is reached | Linux nvme hwmon, NVMe specification |
| Guest vs VMDK latency gap | Possible queue exhaustion on a low queue depth controller | [VMware official] Troubleshooting vSAN Performance |
| PVSCSI queue | Default 64 (device) / 254 (adapter), ring_pages from 8 to 32 | [VMware official] Broadcom KB 343323 (formerly 2053145) |
| Virtual SCSI controller | Legacy adapters have queue depth 32, PVSCSI 64 | [VMware official] Troubleshooting vSAN Performance |
| NIC | VMXNET3 | [VMware official] Broadcom KB 321259 (formerly 1001805) |
| vSAN network | 2% packet loss cuts storage performance by 32% | [VMware official] Troubleshooting vSAN Performance (VCF 9.1 edition) |
| esxtop DAVG, KAVG, GAVG | A problem if they stay above 10ms | [VMware official] Broadcom KB 344099 |
| AWS EBS limit exceeded | Accumulated time over the volume or instance limit, as reported by Nitro. Caution at 1% or more of the measurement time, Warning at 10% or more | [AWS official] EBS detailed performance statistics. Ranges are [Field practice] |
| I/O scheduler | mq-deadline or none | [Red Hat official] Setting the disk scheduler |
| tuned profile | virtual-guest on VMs. Based on throughput-performance (swappiness 30 and dirty_ratio 30, versus 10 and 40 in throughput-performance) | [Red Hat official] TuneD profiles (RHEL 10) |
| Three latency levels | Caution / Warning / Critical | [Field practice] Levels built from KB 389082 (vSAN) and KB 424485 (device). No Elastic official figure |
| Queue utilization ranges | 40% / 80% | [Field practice] No official figure |
| PSI levels | 5% / 20% | [Field practice] The kernel documentation gives no thresholds |
| THP | madvise or never | [Reference] Database vendor operating practice. Not an Elastic requirement |

There is a reason for two sets of latency thresholds. This tool measures latency as seen by the OS.
On a VMware guest, that includes time spent in the vSAN backend, the hypervisor and virtual SCSI, so the VM-view figures (KB 389082) apply.
Bare-metal has none of those layers, so the device-view figures (KB 424485) become the expectation.
However, await as seen by the OS also includes time spent waiting in the block layer, so under heavy load it comes out higher than the device's own latency.
That is why the Caution line uses the KB's "alert threshold", not the device's "normal range".

---

## Relationship to continuous monitoring

This tool does not replace Elastic's System/Linux integration.
Long-term trends, time series across many nodes and alert automation belong on the Elastic side.
This tool is for a single deep dive when an alert fires, and it fills in the following, which continuous collection cannot see:

- Latency p95 (continuous metrics are mostly averages, which erase short spikes)
- Bottleneck location finding (aqu-sz ÷ queue_depth)
- Every block device setting (readahead, scheduler, timeout, iostats, wbt)
- Tracing back the LVM and partition layout
- SCSI abort/reset and hung tasks in the kernel log
- VMware balloon, host swap, reservation, limit
- Storage IRQ imbalance, mmap headroom
- Full check table against official documentation

Continuous alert thresholds are in section 4 of `GUARDLINE.md`.

---

## What the OS cannot see

The report lists these per platform in its "What the OS cannot see by design" table.

| Platform | Items |
|---|---|
| VMware (vSAN) | vSAN storage policy (RAID, FTT, stripe, IOPS limit), write amplification from ES replicas stacked on vSAN replication, VM snapshots, ES node placement on hosts (anti-affinity), vSAN network and resync, cache usage, physical disk state, vNUMA |
| VMware (SAN or NFS datastore) | Datastore type and the array behind it, array-side latency, Storage I/O Control and disk IOPS limits, VM snapshots, host and datastore placement, ESXi path policy |
| bare-metal local | BIOS power policy. Without a RAID controller tool, this also includes cache policy and battery, RAID level and rebuild schedule, and member drive state |
| bare-metal SAN | Array-side latency and controller load, other servers on the same array, volume QoS limits, SAN switch port errors, array replication and snapshot schedules |
| Other VMs and clouds | Host storage backend and cache, volume IOPS and throughput limits (for AWS EBS, it reads time over the limit directly when nvme-cli is present), other VMs on the same host, ES node placement |

The RAID controller cache is not judged from the kernel's `queue/write_cache` value.
Some controllers report a battery-backed cache as "write through", so that value alone does not tell you whether the cache is on.
The verdict uses the controller tool's current cache policy (both the configured and the effective value).

---

## Tests

You can check verdicts for each platform, media and load combination without a real server. Only the Python standard library is used.

```bash
python3 tests/run_tests.py                 # 32 scenarios × Korean and English: expected verdict, verdicts match across languages, shell and HTML verdicts match, catalog checks
python3 tests/i18n_check.py                # Catalog checks only (keys, placeholders, HTML tags, forbidden characters and phrases)
./es_disk_summary.sh <bundle directory> --lang en   # Shell summary verdict only
python3 tests/run_tests.py --dump /tmp/t   # Save per-scenario HTML reports (both languages) and finding lists (JSON)
python3 tests/make_bundle.py --list        # List scenarios
```

---

## Validation status

| Item | Status |
|---|---|
| `/proc/diskstats` calculation accuracy | Cross-checked against iostat 12.6. r_await, w_await, aqu-sz and %util match |
| Kernel log patterns | True-positive and false-positive unit tests pass |
| LVM, NVMe and partition layout | Unit tests on synthetic data pass |
| Parsing 5 `path.data` notations | Pass |
| HTML and charts | Tag validation + execution check on a fake DOM pass |
| Compatibility | Python 3.6 syntax, bash 4.2 and mawk checks pass |
| Shell summary verdict | All 32 synthetic bundles reach the same conclusion as the HTML verdict, including whether the media type is estimated. `tests/run_tests.py` compares them on every run (checked with both mawk and gawk) |
| Per-platform verdicts | 32 synthetic bundles pass: VMware 6 (vSAN All-Flash, Hybrid, default, VMFS), bare-metal NVMe 2, HDD RAID, md SSD stripe, FC SAN, Ceph RBD, RAID tools 8, KVM, AWS EBS 4, ECK, container, RHEL service (PrivateTmp), basic OS checks, low-load hold, ES merge and vector direct IO, index settings |
| RAID tool output parsing | storcli JSON keys: the keys the Prometheus storcli exporter uses. ssacli and arcconf: synthetic output built from published output labels. storcli2 and perccli2: 3 variants with changed key names and value notation (spaces, snake_case, with count fields) and 1 unparseable case. Not yet confirmed on real hardware output |
| AWS EBS statistics parsing | Validated with 4 formats: JSON (single-line and multi-line), ebsnvme text, "name : value" table, and text with units (us). Not yet confirmed on real nvme-cli output |
| 0.9.x → 0.10 regression | Finding lists for 3 VMware synthetic bundles are identical to 0.9.5 |
| Real Linux VM + real ES | UTM (QEMU, Apple Silicon) Rocky Linux 9.8 aarch64 + Elasticsearch 8.19.21. Measured twice, idle and under bulk indexing load (about 120,000 docs/s), and confirmed the shell summary and HTML verdicts match. Fixed 6 collection issues found there (CHANGELOG) |
| Real vSphere guest | Not validated |
| Real bare-metal (NVMe, RAID, SAN), cloud, Kubernetes | Not validated |
| `fs.io_stats` on production clusters | Confirmed on a single-node real ES 8.19. Multi-node production clusters not validated |

---

## Troubleshooting

**ES API connection failed (http=401)**
Set `--es-user` with `ES_PASSWORD`, or `ES_API_KEY`. Without them, only OS-level data is collected.

**Cluster query failed (http=403)**
The `cluster monitor` privilege is required. Without it, run with `--no-cluster` and the warning goes away.

**The verdict is "performance verdict on hold"**
Load was low during the measured window. Run again during indexing or search peak hours with `-d 600` or more.

**PSI shown as unsupported (RHEL 8)**
The RHEL 8, 9 and 10 kernels include PSI, but it is disabled by default. Add `psi=1` to the boot parameters and reboot for a more accurate saturation verdict.
Without it, the verdict uses D state, queue and latency.

**Platform detected wrong, or "Platform not determined"**
Check "platform detection evidence" in the report appendix. This happens on old distributions without `systemd-detect-virt` or where DMI cannot be read.
Set it with `--platform baremetal|vmware|vm`. If you only have the bundle, rebuild with `python3 es_disk_render.py <bundle> --platform baremetal`.

**SSD RAID judged with HDD thresholds**
The RAID controller reports `rotational` as 1 for the logical drive. The report marks this as "estimated".
Collect again with `-s ssd`, or reanalyze the bundle with `es_disk_render.py --storage ssd`.

**No report was generated**
The server has no Python 3.6 or later. Copy the bundle (`.tar.gz`) to a PC and run `es_disk_render.py`.

---

## Roadmap

- [ ] v1.0.0 after validation on real vSphere and bare-metal (NVMe, hardware RAID, FC SAN)
- [x] AWS EBS time-over-limit verdict (nvme amzn stats). Azure and GCP expose no metric inside the VM, so only the pattern-based verdict applies
- [x] Cache, battery and member drive verdicts from RAID controller vendor tools (storcli, perccli, storcli2, perccli2, ssacli, arcconf)
- [ ] Guide for comparing against esxtop output
- [ ] Baseline comparison mode. Show differences from a previous bundle
- [x] Korean and English reports and screen output (`i18n/`)

---

## Notes

- This is not an official Elastic or VMware product.
- It provides diagnostic results and action guidance only, and changes no settings. The owner reviews and applies each action.
- It adds no load and deletes no files. The result directory and the `.tar.gz` in the output location are the only trace it leaves on the server. Remove them yourself after copying the bundle (see "After collection").

## License

MIT License. See [LICENSE](LICENSE).
