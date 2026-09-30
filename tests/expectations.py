# -*- coding: utf-8 -*-
"""
Expected results per test scenario. Used by run_tests.py for both report languages.

Each entry: (scenario, platform, verdict class, must, must_not)
  verdict class: good | risk | bad | hold, or None to skip
  must item:     (severity|None, owner key|None, finding id, tokens)
                 finding id is the catalog key of the finding title (i18n/*.txt).
                 tokens are language-neutral values that must appear in the title (device names, media).
  must_not item: (severity|None, owner key|None, finding ids|None, text|None)
                 no finding may match; text is a language-neutral string that must not appear in any title.
The trailing comments show the English title template for readability.
"""

EXPECT = [
    ('vmware_ok', 'vmware', 'good',
     [
     ],
     [
         (None, None, ('r.0139', 'r.0145', 'r.0149', 'r.0152', 'r.0155', 'r.0181', 'r.0186', 'r.0190', 'r.0195', 'r.0198', 'r.0202', 'r.0205', 'r.0210', 'r.0213', 'r.0216', 'r.0947', 'r.0960', 'r.1120', 'r.1153', 'r.1230'), None),  # Check the 'bottleneck location' finding  / Bottleneck location on hold: could not r
     ]),
    ('vmware_outside', 'vmware', 'bad',
     [
         ('warn', 'r.0072', 'r.0155', ('VM', 'vSAN')),  # Bottleneck location: likely {}
     ],
     [
     ]),
    ('vmware_queue', 'vmware', 'bad',
     [
         ('warn', 'r.0148', 'r.0149', ()),  # Bottleneck location: Guest queue is full (queue utilization {:.0f}%)
     ],
     [
     ]),
    ('bm_nvme_ok', 'baremetal', 'good',
     [
     ],
     [
         (None, 'r.0072', None, None),
         (None, None, None, 'vSAN'),
         (None, None, ('r.0456', 'r.0457', 'r.0793'), None),  # SCSI command timeout is short. Risk of I / SCSI command timeout is short. Risk of I
     ]),
    ('bm_nvme_slow', 'baremetal', 'bad',
     [
         ('warn', None, 'r.0137', ('NVMe',)),  # Disk latency at {1} level ({0} thresholds)
         ('warn', 'r.0095', 'r.0657', ()),  # NVMe temperature reached the warning temperature ({})
         ('caution', 'r.0095', 'r.0668', ()),  # NVMe PCIe link is running below its maximum ({})
         (None, 'r.0148', 'r.0679', ()),  # CPU frequency policy favors power saving ({})
     ],
     [
         (None, 'r.0072', None, None),
     ]),
    ('bm_hdd_raid', 'baremetal', 'bad',
     [
         (None, None, 'r.0137', ('HDD',)),  # Disk latency at {1} level ({0} thresholds)
         ('warn', 'r.0095', 'r.0213', ()),  # Bottleneck location: the disk or controller itself is slow (queue has 
         (None, 'r.0095', 'r.0222', ()),  # Only writes are slow. Check the RAID controller write cache
         ('warn', 'r.0095', 'r.0306', ()),  # Disk-related errors in the kernel log
         ('caution', 'r.own_es', 'r.0536', ()),  # HDD, but the merge thread count is not the Elastic recommendation (1)
         ('info', None, 'r.0738', ()),  # RAID logical drive, so the media type is estimated
     ],
     [
         (None, 'r.0072', None, None),
         (None, None, None, 'vSAN'),
     ]),
    ('bm_md_outlier', 'baremetal', 'bad',
     [
         ('warn', 'r.0095', 'r.0238', ('sdd',)),  # Only one disk in the set is slow. Possible bad disk ({})
         ('warn', None, 'r.0672', ('md1',)),  # Software RAID {} is degraded
     ],
     [
         (None, 'r.0072', None, None),
     ]),
    ('bm_san', 'baremetal', 'bad',
     [
         (None, None, 'r.0137', ('SSD',)),  # Disk latency at {1} level ({0} thresholds)
         ('warn', 'r.0089', 'r.0210', ()),  # Bottleneck location: likely outside the server (storage array, SAN pat
     ],
     [
         (None, 'r.0072', None, None),
         (None, None, ('r.0234', 'r.0238', 'r.0242'), None),  # Only one multipath path is slow ({}) / Only one disk in the set is slow. Possib
     ]),
    ('kvm_slow', 'vm', 'bad',
     [
         (None, None, 'r.0190', ()),  # Bottleneck location on hold: this virtual disk does not expose queue_d
     ],
     [
         (None, 'r.0072', None, None),
         (None, None, None, 'root'),
     ]),
    ('container', 'vm', None,
     [
         ('caution', None, 'r.0250', ()),  # Ran inside a container. Rerun on the host is recommended
     ],
     [
         (None, 'r.0072', None, None),
     ]),
    ('bm_raid_storcli', 'baremetal', 'bad',
     [
         ('warn', 'r.0095', 'r.0691', ()),  # RAID controller battery or cache protection module is not healthy
         ('warn', 'r.0095', 'r.0702', ()),  # ES data RAID write cache is running in write-through
         (None, 'r.0095', 'r.0712', ('RAID',)),  # ES data is on parity RAID ({})
         ('warn', 'r.0095', 'r.0717', ()),  # Signs of trouble on RAID member drives
         (None, None, 'r.0137', ('HDD',)),  # Disk latency at {1} level ({0} thresholds)
     ],
     [
         (None, None, ('r.0738',), None),  # RAID logical drive, so the media type is
         (None, None, ('r.0226', 'r.0626', 'r.0733'), None),  # . No controller tool available, so cache / RAID controllers report events such as b
     ]),
    ('bm_raid_ssacli', 'baremetal', 'good',
     [
     ],
     [
         (None, None, ('r.0738',), None),  # RAID logical drive, so the media type is
         (None, None, None, 'HDD'),
         (None, 'r.0095', None, 'RAID'),
     ]),
    ('bm_raid_arcconf', 'baremetal', None,
     [
         ('warn', 'r.0095', 'r.0698', ('sda',)),  # RAID logical drive is not healthy ({}, {})
         ('warn', None, 'r.0717', ()),  # Signs of trouble on RAID member drives
     ],
     [
     ]),
    ('bm_raid_notool', 'baremetal', None,
     [
         ('info', None, 'r.0733', ()),  # No RAID controller tool, so cache, battery and member drive status wer
         ('info', None, 'r.0738', ()),  # RAID logical drive, so the media type is estimated
     ],
     [
     ]),
    ('vmware_default', 'vmware', 'bad',
     [
         (None, None, 'r.0137', ('VMware',)),  # Disk latency at {1} level ({0} thresholds)
         ('warn', 'r.0072', 'r.0155', ('vSAN',)),  # Bottleneck location: likely {}
     ],
     [
     ]),
    ('vmware_vmfs', 'vmware', 'bad',
     [
         ('warn', 'r.0072', 'r.0155', ('VM',)),  # Bottleneck location: likely {}
     ],
     [
         (None, None, None, 'vSAN'),
     ]),
    ('aws_ebs_cap', 'vm', None,
     [
         ('caution', 'r.0098', 'r.0109', ()),  # {} plateaus at a fixed ceiling. The pattern of a limit (QoS or volume 
         (None, None, 'r.0137', ()),  # Disk latency at {1} level ({0} thresholds)
     ],
     [
         (None, 'r.0072', None, None),
     ]),
    ('eck_host', 'baremetal', 'good',
     [
         ('info', None, 'r.0254', ()),  # Elasticsearch runs inside a container (Docker or Kubernetes)
     ],
     [
         (None, None, ('r.0246', 'r.0250'), None),  # Ran inside a container. Data devices wer / Ran inside a container. Rerun on the hos
     ]),
    ('bm_ceph_rbd', 'baremetal', 'risk',
     [
         (None, None, 'r.0137', ()),  # Disk latency at {1} level ({0} thresholds)
         (None, 'r.0089', 'r.0181', ()),  # Bottleneck location: likely network block storage (Ceph RBD or similar
     ],
     [
         (None, None, None, 'NVMe'),
     ]),
    ('bm_os_only', 'baremetal', 'bad',
     [
         ('warn', None, 'r.0555', ()),  # Flush (device cache flush) is slow. fsync is delayed by the same amoun
         ('caution', None, 'r.0560', ()),  # Processes other than Elasticsearch write heavily to disk
         ('warn', None, 'r.0568', ()),  # Mounted with write barriers disabled
         ('warn', None, 'r.0577', ('92',)),  # ES data is on an LVM thin pool ({:.0f}% data used)
         ('caution', None, 'r.0594', ()),  # Swap is on the ES data disk
         ('caution', 'r.own_es', 'r.0597', ()),  # Snapshot repository (path.repo) is on the same disk as ES data
         ('caution', None, 'r.0607', ('sdb', '3')),  # Device command timeouts recorded ({}, {} times)
         ('warn', None, 'r.0625', ()),  # RAID controller events in the kernel log ({} in the last 7 days)
     ],
     [
         (None, 'r.0072', None, None),
     ]),
    ('aws_ebs_throttle', 'vm', None,
     [
         ('warn', 'r.0098', 'r.0122', ('EBS',)),  # Time spent over the {} performance limit (reported by AWS)
     ],
     [
         (None, None, ('r.0122',), 'EC2'),  # Time spent over the {} performance limit
     ]),
    ('bm_mpi3mr_unparsed', 'baremetal', None,
     [
         ('info', None, 'r.0729', ()),  # RAID controller tool ran, but its output could not be parsed
     ],
     [
         (None, None, ('r.0226', 'r.0626', 'r.0733'), None),  # . No controller tool available, so cache / RAID controllers report events such as b
     ]),
    ('bm_raid_storcli2', 'baremetal', 'bad',
     [
         ('warn', 'r.0095', 'r.0691', ()),  # RAID controller battery or cache protection module is not healthy
         ('warn', 'r.0095', 'r.0702', ()),  # ES data RAID write cache is running in write-through
         (None, 'r.0095', 'r.0712', ('RAID',)),  # ES data is on parity RAID ({})
         ('warn', 'r.0095', 'r.0717', ()),  # Signs of trouble on RAID member drives
         (None, None, 'r.0137', ('HDD',)),  # Disk latency at {1} level ({0} thresholds)
     ],
     [
         (None, None, ('r.0738',), None),  # RAID logical drive, so the media type is
         (None, None, ('r.0226', 'r.0626', 'r.0733'), None),  # . No controller tool available, so cache / RAID controllers report events such as b
         (None, None, ('r.0729',), None),  # RAID controller tool ran, but its output
     ]),
    ('bm_raid_storcli_cnt', 'baremetal', 'bad',
     [
         ('warn', 'r.0095', 'r.0691', ()),  # RAID controller battery or cache protection module is not healthy
         ('warn', 'r.0095', 'r.0702', ()),  # ES data RAID write cache is running in write-through
         (None, 'r.0095', 'r.0712', ('RAID',)),  # ES data is on parity RAID ({})
         ('warn', 'r.0095', 'r.0717', ()),  # Signs of trouble on RAID member drives
         (None, None, 'r.0137', ('HDD',)),  # Disk latency at {1} level ({0} thresholds)
     ],
     [
         (None, None, ('r.0738',), None),  # RAID logical drive, so the media type is
         (None, None, ('r.0729',), None),  # RAID controller tool ran, but its output
     ]),
    ('aws_ebs_v2', 'vm', None,
     [
         ('warn', 'r.0098', 'r.0122', ('EBS',)),  # Time spent over the {} performance limit (reported by AWS)
     ],
     [
         (None, None, ('r.0122',), 'EC2'),  # Time spent over the {} performance limit
     ]),
    ('aws_ebs_v3', 'vm', None,
     [
         ('warn', 'r.0098', 'r.0122', ('EBS',)),  # Time spent over the {} performance limit (reported by AWS)
     ],
     [
         (None, None, ('r.0122',), 'EC2'),  # Time spent over the {} performance limit
     ]),
    ('idle_low_load', 'baremetal', 'hold',
     [
     ],
     [
     ]),
    ('bm_raid_storcli2_snake', 'baremetal', 'good',
     [
     ],
     [
         (None, None, ('r.0738',), None),  # RAID logical drive, so the media type is
         (None, None, ('r.0729',), None),  # RAID controller tool ran, but its output
         (None, 'r.0095', None, 'RAID'),
     ]),
    ('bm_es_merge_vector', 'baremetal', 'good',
     [
         ('info', None, 'r.0340', ()),  # Merges are queued (merge thread pool)
         ('info', None, 'r.0368', ()),  # Vector rescoring reads the disk directly, bypassing the page cache (di
     ],
     [
         (None, None, ('r.0359',), None),  # Elasticsearch metrics not collected
     ]),
    ('vmware_hybrid', 'vmware', None,
     [
         ('info', 'r.0072', 'r.0363', ()),  # vSAN Hybrid (OSA) is set to be discontinued in a future VCF release
     ],
     [
     ]),
    ('rhel_service_ns', 'vm', None,
     [
     ],
     [
         (None, None, ('r.0254',), None),  # Elasticsearch runs inside a container (D
         (None, None, ('r.0246', 'r.0250'), None),  # Ran inside a container. Data devices wer / Ran inside a container. Rerun on the hos
     ]),
    ('idx_settings_nested', 'baremetal', None,
     [
         ('info', None, 'r.0523', ()),  # Some indices fsync the translog asynchronously (durability: async)
     ],
     [
         (None, None, ('r.0530',), None),  # Index settings not collected
     ]),
]


def _hits(findings, sev, owner, fid):
    return [f for f in findings if (sev is None or f["sev"] == sev) and (owner is None or f["owner_id"] == owner)
            and (fid is None or f["id"] == fid)]


def check(out, lang):
    """out[scenario] = {"platform", "verdict_class", "findings": [{sev, owner_id, id, title}]}"""
    fails = []
    for name, plat, vcls, must, must_not in EXPECT:
        r = out.get(name)
        if r is None:
            fails.append("{} [{}]: no result".format(name, lang)); continue
        if r["platform"] != plat:
            fails.append("{} [{}]: platform {} (expected {})".format(name, lang, r["platform"], plat))
        if vcls and r["verdict_class"] != vcls:
            fails.append("{} [{}]: verdict {} (expected {})".format(name, lang, r["verdict_class"], vcls))
        for sev, owner, fid, toks in must:
            hit = [f for f in _hits(r["findings"], sev, owner, fid) if all(t in f["title"] for t in toks)]
            if not hit:
                fails.append("{} [{}]: missing {}".format(name, lang, (sev, owner, fid, toks)))
        for sev, owner, fids, txt in must_not:
            for f in _hits(r["findings"], sev, owner, None):
                if (fids is None or f["id"] in fids) and (txt is None or txt in f["title"]):
                    fails.append("{} [{}]: unexpected {} -> {}".format(name, lang, (sev, owner, fids, txt), f["title"]))
                    break
    return fails
