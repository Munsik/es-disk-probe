# Elasticsearch Node Disk Guardline (VMware · bare-metal · SAN)

English | [한국어](GUARDLINE.ko.md)

This document sets the **baseline for preventing** disk problems from making Elasticsearch unstable.
It uses the same checks and thresholds as the diagnostic tool (`es_disk_collect.sh`). Read it side by side with the "Best practice checklist" in the report.

Each item is tagged with its source type.

- **[Elastic]** Recommendation from official Elastic documentation
- **[VMware]** Recommendation from VMware/Broadcom documentation
- **[OS]** Kernel or distribution documentation
- **[Field practice]** No official number exists, so the value comes from operating experience. Adjust it to your environment

Section 1 covers VMware design and section 1-B covers bare-metal design. From section 2 on, the content is shared. Items that differ by platform are noted inside the tables.

---

## 1. Design stage: VM and vSAN (VMware admin)

These items cannot be changed from the Guest OS. Changing them later requires stopping the VM or moving data. Agreeing on them before the build is the cheapest option.

| Item | Baseline | Why it matters | Source |
|---|---|---|---|
| Memory reservation | Reserve 100% of VM memory | When the host runs short, any unreserved memory is reclaimed through balloon and swap. The page cache shrinks first, so searches read more from disk | [VMware] |
| Memory and CPU limit | Do not set | Memory above the limit is always subject to balloon and swap | [VMware] |
| Virtual disk controller | PVSCSI. On vSAN ESA, virtual NVMe (vNVMe) is also recommended | Handles the same I/O with less CPU and uses a deeper queue. With several disks, spread them across up to 4 controllers | [VMware] KB 392848, Troubleshooting vSAN Performance |
| Disk separation | Put the OS disk and the ES data disk on separate VMDKs. Attach the data VMDK to its own PVSCSI controller | Keeps OS logs and ES from sharing one controller queue | [VMware] |
| NIC | VMXNET3 | Replica writes and shard recovery travel over the network between ES nodes | [VMware] |
| Storage policy | For write-heavy hot nodes, consider RAID-1 (mirror) first. VMware states that RAID-5/6 on vSAN ESA performs at RAID-1 level | RAID-5/6 on OSA adds a read-modify-write to every write, which raises write latency | [VMware] |
| IOPS limit in the policy | Do not set | If set, the Guest only sees IOPS "flattening out for no reason" | [VMware] |
| Snapshots | Do not keep long term | While a snapshot exists, writes go to a delta file and slow down | [VMware] |
| Host distribution | DRS anti-affinity between ES node VMs | Prevents one host failure from losing a primary and its replica at the same time | [VMware][Elastic] |
| vSAN free space | Keep the free space recommended for your vSAN version (operations / host rebuild reserve) | Without it, resync and reconfiguration slow down and write latency rises in the meantime | [VMware] |
| CPU hot-add | Off | Turning it on disables vNUMA, which can slow memory access | [VMware] |

**Include write amplification in the design.** With ES replica 1 and vSAN FTT=1 (RAID-1), each document is physically written 4 times. ES and vSAN each provide their own availability, so always include this multiplier when sizing capacity and write bandwidth. Decide whether to reduce replicas from the availability requirements, not from disk alone.

**If the VMware datastore is SAN (VMFS) or NFS**, use array-side criteria instead of the vSAN items above: volume RAID level, array write cache and replication method,
Storage I/O Control and disk IOPS limits, and the ESXi path policy (Round Robin and so on). Pass `-s vmfs` to the diagnostic tool so its guidance matches.

---

## 1-B. Design stage: bare-metal servers and storage (System admin, Hardware team)

Changing these items later requires moving data or replacing hardware.

| Item | Baseline | Why it matters | Source |
|---|---|---|---|
| Media | SSD, NVMe if possible. HDD only for warm and cold tiers | SSD is generally faster than spinning disks. ES reads and writes many files at once, both sequentially and randomly | [Elastic] |
| Attachment | Prefer locally attached storage. If you use a SAN, benchmark it with real load | Directly attached storage has lower latency. Some remote storage is very slow under ES load | [Elastic] |
| Combining disks | RAID 0 or LVM stripe | ES replicas provide redundancy. Adding RAID 1/10 on top writes the same document several times | [Elastic] |
| RAID 5/6 | Avoid on hot nodes | Every write adds a read-modify-write | [Field practice] |
| Controller cache | Write-back protected by battery (or flash). Check battery health regularly | If the cache switches to write-through, every fsync goes to disk and write latency rises sharply | [Field practice] |
| RAID management tools | Install the vendor tool on the OS (storcli, perccli, storcli2 and perccli2 for MegaRAID 96xx and PERC 12 or later, ssacli, arcconf) | The diagnostic tool then checks cache, battery and member drive status automatically. It also shortens troubleshooting during an incident | [Field practice] |
| NVMe cooling | Keep airflow through the drive bays and fit blanks in empty slots | Above the thermal threshold (default warning temperature WCTEMP), the device lowers its own performance | [OS] NVMe specification |
| NVMe slot | A slot that connects at the PCIe generation and lane count the device supports | If the link trains at a lower speed, maximum throughput drops by the same amount | [OS] |
| BIOS power policy | Consider a performance profile (check the vendor guide) | Wake-up time from power-saving states is added to I/O completion handling | [Field practice] |
| Rack and power distribution | Spread ES nodes across racks and power feeds, and configure awareness | Prevents one rack failure from losing a primary and its replica at the same time | [Elastic] |

---

## 2. OS configuration (System admin)

| Item | Baseline | Check command | Source |
|---|---|---|---|
| readahead | 128KiB or less on ES data devices (dm devices too, if LVM) | `lsblk -o NAME,RA` | [Elastic] |
| I/O scheduler | VM: mq-deadline or none (avoid cfq/bfq). bare-metal: none or kyber for NVMe and SSD, mq-deadline or bfq for HDD | `cat /sys/block/sdX/queue/scheduler` | [OS] Red Hat |
| tuned profile | VM: virtual-guest. bare-metal: throughput-performance | `tuned-adm active` | [OS] Red Hat |
| CPU governor | bare-metal: performance (set by throughput-performance) | `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` | [OS] Red Hat |
| SCSI timeout | VMware: installing open-vm-tools sets 180 seconds automatically (below 60 seconds is a warning, [Field practice]). bare-metal local disks use the kernel default of 30 seconds. For SAN, follow the multipath vendor recommendation | `cat /sys/block/sdX/device/timeout` | [VMware] |
| I/O statistics | `queue/iostats` = 1 | `cat /sys/block/sdX/queue/iostats` | [OS] |
| File system | xfs or ext4 on a local block device. No NFS | `findmnt -T <path.data>` | [Elastic] |
| Mount options | noatime or relatime. Use `fstrim.timer` instead of `discard` | `findmnt -o OPTIONS` | [OS] |
| Partition alignment | 1MiB (2048 sectors) boundaries | `parted /dev/sdX unit s print` | [OS] |
| Combining VMDKs | LVM stripe (`lvcreate -i <number of disks> -I 256k`). No linear | `lvs -o +stripes` | [OS] |
| vm.max_map_count | Minimum 262144, recommended 1048576 | `sysctl vm.max_map_count` | [Elastic] |
| swap | Disabling is recommended. If that is not possible, set `bootstrap.memory_lock: true`, and at minimum `vm.swappiness=1` | `swapon --show` | [Elastic] |
| File handles | 65535 or more | `cat /proc/<pid>/limits` | [Elastic] |
| open-vm-tools | VMware only. Install it | `vmware-toolbox-cmd -v` | [VMware] |
| Software RAID check schedule | bare-metal md: schedule the periodic check outside service peaks | `cat /proc/mdstat`, `/etc/cron.d/raid-check` | [OS] |
| PSI | Disabled by default on RHEL 8, 9 and 10. Turn it on with the boot parameter `psi=1` for more accurate saturation diagnosis | `cat /proc/pressure/io` | [OS] |
| write barrier | No `nobarrier` or `barrier=0`. A power loss can corrupt the file system (xfs dropped the option entirely as of kernel 4.19) | `findmnt -o OPTIONS -T <path.data>` | [OS] |
| LVM thin pool | A thick LV is recommended for ES data. If thin, extend before data or metadata usage reaches 80%. At 100%, writes stop | `lvs -o lv_name,data_percent,metadata_percent` | [OS] Red Hat |
| LVM snapshot | Do not keep snapshots on the ES data LV for long. Every write to the origin triggers a copy and slows writes | `lvs -o lv_name,origin` | [OS] Red Hat |
| Shared disks | Keep swap, path.repo (snapshot repository) and path.logs on a different disk from ES data | `swapon --show`, `findmnt -T <path>` | [Elastic] |
| Device error counters | If `iotmo_cnt` or `ioerr_cnt` increases, check the path and the device | `cat /sys/block/sdX/device/iotmo_cnt` | [OS] |

**Raising the PVSCSI queue depth (cmd_per_lun=254, ring_pages=32) is not a default change, even on VMware.** Apply it only when the diagnostic report confirms "Guest queue is full". It requires a reboot. [Broadcom KB 343323 (formerly 2053145)]

---

## 3. Elasticsearch settings (Elasticsearch admin)

| Item | Baseline | Why it matters | Source |
|---|---|---|---|
| JVM heap | Automatic sizing (default) is recommended. If you set it yourself, keep it at or below 50% of RAM and below the compressed oops limit (26GB is safe on most systems, 30GB on some) | The rest of RAM becomes page cache and serves segment reads | [Elastic] |
| path.data | A single path (combine multiple disks with LVM stripe in the OS) | Multiple data paths are deprecated as of 7.13 | [Elastic] |
| disk watermark | Keep the defaults (85/90/95%) and keep usage at least 10 percentage points below high. As of 8.5, if you do not set the ratios yourself, a free space rule (max_headroom 200/150/100GB) also applies, so large disks hit the watermark later | Above high, shard relocation itself becomes heavy disk load | [Elastic] |
| shard allocation awareness | VMware: per ESXi host. bare-metal: per rack or power feed. Cloud: per availability zone | ES does not know which host or rack a node is on | [Elastic] |
| Recovery rate | Keep `indices.recovery.max_bytes_per_sec` at the default 40mb (up to 250mb on dedicated cold and frozen nodes, depending on memory). Adjust it only when recovery interferes with the service | Raising it speeds up recovery but uses more disk in the meantime | [Elastic] |
| Indexing-heavy indices | Consider a longer `refresh_interval` (for example 30s) | Each refresh creates a small segment and adds merge load | [Elastic] |
| translog | Keep `durability: request` (default) | `async` reduces fsync calls, but recent data can be lost after a failure. Do not use this setting to hide disk problems | [Elastic] |
| merge threads | On HDD (bare-metal) or Hybrid vSAN, set `index.merge.scheduler.max_thread_count: 1`. The default is half the number of processors (capped at 4 on 9.3 and earlier and on 8.x) | On spinning disks, concurrent merges are slower | [Elastic] |
| Shard size | 10-50GB per shard | Too small means more segments and mmaps. Too large means long recovery | [Elastic] |

---

## 4. Continuous monitoring: alert on the same thresholds as this tool

The diagnostic tool runs only during checks. The last step of the guardline is to set up Elastic's own monitoring so it alerts on the same thresholds day to day.

### 4-1. OS disk metrics

Latency and queued I/O are available only in the `iostat` dataset of the **Linux integration** (or the Metricbeat `linux` module). The `diskio` data from the System integration and the Metricbeat `system` module does not include these values.

| Collection method | What to enable | Fields |
|---|---|---|
| Elastic Agent (Fleet) | iostat in the Linux integration (GA, TSDS from 1.1.0) | `linux.iostat.*` |
| Metricbeat | `iostat` metricset of the `linux` module | `linux.iostat.*` |

`system.diskio.iostat.*` in older documentation existed only up to Metricbeat 7.17 and was removed in 8.0. Both methods use the same source as the values this tool computes from `/proc/diskstats`.

| Alert | Field | Caution | Warning | Source |
|---|---|---|---|---|
| Read latency (vSAN) | `linux.iostat.read.await` | flash 5ms / hybrid 10ms | 10ms / 20ms | [Field practice] based on Broadcom KB 389082 |
| Read latency (bare-metal, SAN) | Same as above | NVMe 1ms / SSD 3ms / HDD 25ms | 3ms / 6ms / 30ms | [Field practice] based on the device-level alert thresholds in Broadcom KB 424485 |
| Write latency | `linux.iostat.write.await` | Same as read | Same as read | [Field practice] |
| Queued I/O | `linux.iostat.queue.avg_size` | 50% of queue_depth | 80% | [Field practice] |

- Alert on a window, such as a 5-minute average, so short spikes do not flood you with alerts.
- Do not alert on `busy` (%util). Devices that process I/O in parallel, such as vSAN, NVMe and RAID, can still have headroom at 100%.
- For striped disks, alert on both the combined device (md, dm) and each member disk. When only one disk is slow, the combined device metric dilutes it.

### 4-2. ES metrics (Stack Monitoring)

| Alert | Threshold | Notes |
|---|---|---|
| Disk usage | Use the default rule (80%) | Built into Kibana Stack Monitoring |
| Thread pool write/search rejections | Use the default rule | Built in |
| Indexing throttle | `indices.indexing.throttle_time_in_millis` increases | Check together with "now throttling indexing" in the ES log |
| Merge backlog | Queue and active counts in `thread_pool.merge` stay high (9.1+, 8.19+) | Direct signal that merges cannot keep up with the disk. Leads to indexing throttling |
| Merge disk headroom | `indices.merge.disk.watermark.high` (default 95%, 100GB headroom) | Above it, new merges stop, segments pile up and indexing slows down |
| Imbalance across nodes | A node's disk busy time is 2x the median or more | Can be checked periodically with `es_cluster_probe.sh` |

---

## 5. Principles for changing settings

This toolkit does not change anything. Follow these principles when you apply the actions in the report.

1. **Change one thing at a time.** If you change several things at once, you cannot tell which one made the difference.
2. Run this tool before and after the change, at the same time of day, and compare. A change without measurement is a guess.
3. **Write down how to roll back first.** Record the previous values of sysctl, udev and mount options before you change them.
4. **For items that need a reboot** (PVSCSI parameters, psi=1, controller changes), work from a rolling restart plan. Do one node at a time, and move to the next node only after the cluster is back to green.
5. **Do not create load for the diagnosis.** This tool measures the real production load. If the performance verdict is on hold, collect again during indexing and search peak hours instead of running a load test.

---

## 6. Check cadence

| When | What to do |
|---|---|
| Before installing ES | Check OS settings and layout with `es_disk_collect.sh --no-es` (performance verdict comes after load is in place) |
| First peak after go-live | Run the collector and keep the report as the baseline |
| Once a quarter | Run the collector and the cluster probe during peak hours and compare with the baseline |
| Before and after a configuration change | Run the collector at the same time of day and compare |
| Before adding nodes or capacity | Check imbalance with the cluster probe. Check settings and layout on new nodes with `--no-es` |
| During an incident or slowdown | Run the collector right away and use "Last 7 days" in the report to find when it started |

---

## 7. VMware admin request checklist (copy and use)

The Guest cannot see these items by design. Send this with the diagnostic report.

```
[ES node VM: ______________]  Measurement time: ______________

□ Storage policy: RAID ___ / FTT ___ / stripe ___ / IOPS limit set? ___
□ Any VM snapshots?
□ Memory reservation ___% / any limit?
□ DRS anti-affinity rule between ES node VMs?
□ esxtop at measurement time: DAVG ___ / KAVG ___ / GAVG ___ (ms)
□ vSAN performance service at measurement time: VM latency ___ / disk group latency ___
□ Resync running at measurement time?
□ vSAN network: dedicated? / speed ___ / any retransmits or latency issues?
□ Any Skyline Health warnings?
```

---

## 7-B. Hardware team request checklist (bare-metal, copy and use)

These items, such as the state behind the RAID controller, are not visible from the OS. Send this with the diagnostic report.

```
[ES node server: ______________]  Measurement time: ______________

□ RAID controller model ___ / firmware ___ / driver ___
□ Logical drive RAID level ___ / stripe size ___
□ Cache policy: currently write-back / write-through (both configured and actual)
□ Battery or cache module status, learn cycle schedule
□ Controller events around the measurement time: rebuild, consistency check, patrol read, disk errors
□ Member drive status: predictive failure, media error count
□ BIOS System Profile (performance / power saving)
□ BMC (iDRAC, iLO, XCC) temperature and power events
```

## 7-C. Storage admin request checklist (SAN, copy and use)

```
[ES node server: ______________]  Measurement time: ______________  LUN/volume: ______________

□ Volume latency (read/write) at measurement time ___ / host port latency ___
□ Array controller utilization ___ / load from other hosts at the same time
□ Volume QoS (IOPS or throughput limit) set? ___
□ SAN switch port errors (CRC, buffer credit shortage)?
□ Array replication (sync/async) and snapshot schedule
□ Number of paths and recommended multipath policy
```
