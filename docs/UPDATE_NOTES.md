# Update notes

Last checked: 2026-09-29
Checked against: Elasticsearch 9.5.4, VMware Cloud Foundation and vSphere 9.1, RHEL 10, Linux kernel master docs
Report text lives in `i18n/ko.txt` and `i18n/en.txt`. When a criterion changes, update both catalogs (`tests/i18n_check.py` checks they stay in sync).

This document covers two things.
First, update items (done and still open). Second, the thresholds to recheck when a new version ships, with their sources.
Changes already made are listed in `CHANGELOG.md` 0.10.0 under "Checked against latest official docs".

---

## 1. Open update items

Everything that could be done without real device output is done. The method: feed inputs built in several expected formats and check that the same conclusion comes out even when the format differs a little.
This proves the parsers do not break, and that they say so when they cannot read something. It does not prove the real key names are correct.

### Done (2026-09-29)

| Item | Change | Verification |
|---|---|---|
| storcli2 and perccli2 JSON | Reads key names (spaces, snake_case, count fields next to lists) and value spellings (Optimal/OPTIMAL, Write Back/WB, etc.) and maps them to storcli notation. The shell summary does the same | 3 variants + 1 unparseable |
| AWS `nvme amzn stats` | Single-line and multi-line JSON, ebsnvme text, "name : value" table, text with units | Python and shell each check that all 4 read as the same values |
| merge thread pool | Info finding when the queue is backed up at both the start and end points | 1 scenario |
| ES vector direct IO | For nodes with `-Dvector.rescoring.directio=true`, the report notes that reads bypass the page cache | 1 scenario |
| vSAN OSA hybrid planned deprecation | Analyzing with `-s hybrid` shows the VCF 9.0 notice | 1 scenario |
| Pre-deployment check (bench) | Built, then removed. `es_disk_bench.sh` was deleted under the rule that the diagnostic tool never writes, deletes, or creates load. Checked on 32 bundles that under real production load, running a bench or not does not change the verdict | N/A |

### Needs real devices or docs to finish

| # | Item | Current state | To finish |
|---|---|---|---|
| 1 | storcli2 and perccli2 real key names | Variant handling should read most of it, but not confirmed. If parsing fails, raw output is kept and the report says so | 1 `show all J` output from MegaRAID 96xx or PERC 12 |
| 2 | storcli, ssacli, arcconf real output | Verified only with synthetic output built from public labels | Real output from each tool (1 healthy, 1 faulty) |
| 3 | nvme-cli `amzn stats` real text | Handles 4 possible formats | 1 output from EC2 |
| 4 | HPE Gen11 and Gen12 tool mapping | Assumes ssacli for SR, storcli for MR | HPE docs or the server's `proc_name` |
| 5 | Lenovo and Supermicro RAID | Assumes storcli since they are Broadcom based | Vendor docs |
| 6 | Microchip arcconf + smartpqi | Assumes arcconf for SmartRAID 3100 and 3200 | Microchip user guide |
| 7 | GCP Hyperdisk model name | Assumes `nvme_card-pd` | Model name from a C3 or H3 instance |
| 8 | Azure and GCP limit-exceeded metrics | No metric visible inside the VM, so only a pattern-based verdict | Add when guest tools or docs appear |
| 9 | vSAN ESA specific latency threshold | Uses the flash threshold (5ms) | Replace when Broadcom publishes ESA thresholds |
| 10 | ES `on_disk_rescore` (9.3 preview) | Mapping option, so not collected | Add a mapping query when vector node diagnostics are needed |

---|---|---|---|---|
| 1 | storcli2 and perccli2 JSON parsing | Queried, but key names are not confirmed by public docs. If parsing fails, the raw output stays in the bundle and the report flags it | `storcli2 /call show all J`, `/call/vall show all J`, `/c0/eall/sall show all J` output from a MegaRAID 96xx or Dell PERC 12 (H965i etc.) device | `es_disk_render.py` parse_storcli, `es_disk_summary.sh` raid block, `tests/make_bundle.py` |
| 2 | Verify RAID parsers with real device output | storcli, ssacli, arcconf parsers were verified only with synthetic output built from public labels | Real output from each tool (1 healthy and 1 with a cache or battery fault is enough) | Same locations, replace test scenarios |
| 3 | AWS `nvme amzn stats` output format | Reads both JSON (`-o json`) and the ebsnvme human-readable format. The nvme-cli text format has not been seen on a real system | `nvme amzn stats /dev/nvme1n1` and `-o json` output from an EC2 instance using EBS | `parse_ebs_stats`, ebs block in the shell summary |
| 4 | HPE Gen11 and Gen12 tool mapping | Assumes ssacli for SR controllers and storcli for MR controllers. The MR Gen11 driver (megaraid_sas or mpi3mr) and the Gen12 tool are not confirmed | HPE QuickSpecs or `/sys/class/scsi_host/*/proc_name` on a real server | Collector RAID block |
| 5 | Lenovo and Supermicro RAID | Assumed to work with storcli since they are Broadcom based, but not confirmed | Vendor docs or a real device | README.md RAID tool table |
| 6 | Microchip arcconf with smartpqi | SmartRAID 3100 and 3200 are assumed to use the smartpqi driver and arcconf, but not confirmed in primary docs | Microchip user guide | Collector arcconf condition |
| 7 | GCP Hyperdisk model name | PD attached as NVMe is `nvme_card-pd`. Not confirmed whether Hyperdisk uses the same name | `/sys/block/nvme*/device/model` on a C3 or H3 instance | `CLOUD_BLOCK_MODEL` |
| 8 | Azure and GCP limit-exceeded metrics | No limit-exceeded metric found that is visible inside the VM. For now only an estimated "looks like it hit the limit" verdict | Add when new guest tools or docs appear | Limit verdict block |
| 9 | vSAN ESA specific latency threshold | Broadcom does not publish separate ESA numbers, so the flash threshold (5ms) is used. The docs only say "ESA has much higher performance limits" | Replace when Broadcom publishes ESA thresholds | `LAT_TH`, `LAT_SRC` |
| 10 | vSAN OSA hybrid end of support | The VCF 9.0 notice says it will be "discontinued in a future release". Once discontinued, mark the `-s hybrid` notice as legacy | VCF release notes | README.md, report text |
| 11 | ES vector direct IO | 9.1 `vector.rescoring.directio` and 9.3 `on_disk_rescore` read disk directly and bypass the page cache. On vector search nodes, reads can rise with no cache hits, which changes how results are read. Not in the verdict yet | Decide how to collect JVM options and mapping info from target nodes | Collector ES queries, report Info items |
| 12 | merge thread pool verdict | `thread_pool.merge` is collected but not used in the verdict | Define a threshold for backlog using the start and end values | `es_disk_render.py` Elasticsearch impact verdict |

---

## 2. Thresholds to recheck on new versions

These items have changed across versions or may change.
When a new Elasticsearch minor version, vSphere or VCF update, or RHEL major version ships, go through these tables once.

### Elasticsearch

| Criterion | Current value | Change history | Where to check |
|---|---|---|---|
| vm.max_map_count | Minimum 262144, recommended 1048576 | Recommended value raised in 8.16 | Bootstrap checks, `vm-max-map-count` docs |
| disk watermark | 85/90/95%, max_headroom 200/150/100GB | headroom added in 8.5 | Cluster-level shard allocation and routing settings |
| merge disk watermark | `indices.merge.disk.watermark.high` 95%, 100GB free | New setting | Merge settings |
| merge thread default | Half the processor count | Max-4 cap removed in 9.4, based on JVM processor count in 9.4.7 and 9.5.1 | Merge settings, `MergeSchedulerConfig` source |
| merge thread pool | `thread_pool.merge` | Added in 9.1, 8.19 | Thread pools |
| JVM heap | Auto sizing recommended, compressed oops limit (26GB in most cases, 30GB on some) | | JVM settings |
| Recovery speed | 40mb, dedicated cold and frozen nodes up to 250mb depending on memory | | Index recovery settings |
| Multiple data paths | Deprecated since 7.13, still present through 9.5 | | Path settings |
| readahead | 128KiB | Per-file-type read advice since 9.2 (RANDOM for vectors) | Tune for search speed, `FsDirectoryFactory` source |
| Throttle log text | `now throttling indexing` | Format after the message changed in 9.1, 8.19 | `InternalEngine` source |
| Continuous monitoring fields | `linux.iostat.*` | `system.diskio.iostat.*` removed in 8.0 | Linux integration package |

### VMware (Broadcom)

| Criterion | Current value | Where to check |
|---|---|---|
| vSAN latency | Healthy below flash 5ms, hybrid 20ms (vSAN 7 and 8) | Broadcom KB 389082 |
| Device latency | NVMe 0.5ms, SSD 1ms or less, HDD 10-20ms. Alarm at NVMe 1ms, SSD 3ms, HDD 25ms. Above 30ms is critical | Broadcom KB 424485 |
| esxtop | DAVG, KAVG, GAVG sustained at 10ms means a problem | Broadcom KB 344099 |
| PVSCSI queue | Default 64/254. When raised, cmd_per_lun 254, ring_pages 32 | Broadcom KB 343323 (formerly 2053145) |
| NIC | VMXNET3 | Broadcom KB 321259 (formerly 1001805) |
| Controller | PVSCSI. With several disks, spread across up to 4. ESA uses vNVMe | Broadcom KB 313507, 392848, Troubleshooting vSAN Performance |
| General | | Performance Best Practices for VMware vSphere (currently the 9.1 edition) |

Broadcom has moved KB numbers before. If a cited KB does not open, search for it by title.

### Linux and RHEL

| Criterion | Current value | Where to check |
|---|---|---|
| diskstats fields | 1-11 base, 12-15 discard (4.19+), 16-17 flush (5.5+) | kernel `Documentation/admin-guide/iostats.rst` |
| Scheduler recommendation | HDD mq-deadline or bfq, high-performance SSD none or kyber, VM mq-deadline (none for multi-queue HBA) | RHEL "Setting the disk scheduler" |
| TuneD | Physical servers throughput-performance, VMs virtual-guest | RHEL "Optimizing system performance with TuneD" |
| PSI | Disabled by default on RHEL 8, 9, 10. `psi=1` | Red Hat docs, kernel `PSI_DEFAULT_DISABLED` |
| systemd-detect-virt | Includes vm-other, container-other | systemd `src/basic/virt.c` |
| NVMe state and temperature | state string, temp1_max = current over-temperature threshold | kernel `drivers/nvme/host/sysfs.c`, `hwmon.c` |

### RAID and cloud

| Criterion | Current value | Where to check |
|---|---|---|
| New Broadcom generation | MegaRAID 96xx uses storcli2, driver mpi3mr | Broadcom StorCLI2 User Guide |
| Dell | PERC 11 perccli, PERC 12 and 13 perccli2 (mpi3mr) | Dell PERC CLI Reference Guide |
| megaraid_sas SCSI address | channel 0 and 1 physical disks, 2 and above logical drives | kernel `megaraid_sas.h` |
| AWS | EBS and instance store model names, `nvme amzn stats` fields | AWS EBS detailed performance statistics |
| Azure | MSFT NVMe Accelerator v1, Microsoft NVMe Direct Disk v1 and v2, SCSI is Msft Virtual Disk | Azure `azure-vm-utils` disk identification |
| GCP | nvme_card-pd, nvme_card(N) | GoogleCloudPlatform `guest-configs` udev rules |

---

## 3. Update procedure

1. Open the sources in the tables above and check whether any value changed.
2. Fix changed values together in the code (`es_disk_render.py` verdicts and source text, the same verdicts in `es_disk_summary.sh`), the threshold source table in `README.md` and `README.ko.md`, and `GUARDLINE.md` and `GUARDLINE.ko.md`.
3. For new behavior, add a scenario to `tests/make_bundle.py` and write the expected verdict in `tests/expectations.py`.
4. Run `python3 tests/run_tests.py`. It fails if the shell summary and the HTML verdict differ.
5. Record what changed and why in `CHANGELOG.md`, then update the "Last checked" date and the tables in this document.
