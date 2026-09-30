# Writing style and glossary

This file applies to every user-facing text in the toolkit: `i18n/ko.txt`, `i18n/en.txt`, the README and GUARDLINE files, and help output.
`tests/run_tests.py` checks the forbidden characters and phrases below.

## Common rules (both languages)

- Lead with the fact and the number. Explanation comes after.
- Short sentences. One idea per sentence.
- No em dash (U+2014) or en dash (U+2013) anywhere. For ranges write `10~20 ms` (Korean) or `10-20 ms` (English).
- Keep technical terms in their original form: page cache, queue depth, fsync, write-back, write-through, JVM heap, off-heap, aqu-sz, PSI, D state, merge, refresh, translog, shard, replica, watermark, readahead, scheduler, multipath, thin pool.
- Product and tool names exactly as the vendor writes them: vSAN, ESXi, PVSCSI, VMXNET3, storcli, perccli, ssacli, arcconf, smartctl, Elasticsearch.
- Keep source tags short and consistent (see glossary).
- No marketing words. The tool reports measurements and gives actions.

## Korean

- Statements end with `~합니다` / `~입니다`. Actions end with `~하세요`.
- Avoid translation-style phrases: `~하는 것이 중요합니다`, `살펴보겠습니다`, `다양한`, `효과적으로`, `~를 통해`.
- Do not spell out technical terms in Korean (write `JVM heap`, not `힙 밖`).

## English

- Plain operations-document English. American spelling.
- Actions are imperative: "Check ...", "Ask the VMware admin to ...", "Move ... to ...".
- Findings are short statements: "Write latency p95 is above the warning line".
- Avoid: "It's worth noting", "crucial", "robust", "seamless", "leverage", "delve", "comprehensive", "furthermore", "moreover", "utilize", "in order to", "plays a key role".
- Write from the English reader's view. Do not translate Korean sentence structure word by word.

## Glossary

| Korean | English |
|---|---|
| 정상 / 참고 / 주의 / 경고 / 위험 | OK / Info / Caution / Warning / Critical |
| 디스크는 정상입니다 | Disk is healthy |
| 지금은 버티고 있지만 위험 요인이 있습니다 | Holding up for now, but risk factors exist |
| 디스크 성능 저하 징후가 있습니다 | Signs of disk performance degradation |
| 설정 점검은 완료, 성능 판정은 보류합니다 | Configuration checked, performance verdict on hold |
| ES에 처리 지연 신호가 있지만, 디스크 응답은 정상입니다 | Elasticsearch shows processing delays, but disk latency is normal |
| 판정 (전체) / 판정 항목 | verdict / finding |
| 근거 / 이유 / 조치 / 출처 | evidence / why it matters / action / source |
| 담당자 | owner |
| 지연 / 포화 / 오류 | Latency / Saturation / Errors |
| ES 영향 | Elasticsearch impact |
| 메모리·캐시 | Memory and cache |
| 설정 | Configuration |
| VMware 자원 / 가상화 자원 / 하드웨어 | VMware resources / Virtualization resources / Hardware |
| 네트워크(보조) | Network (secondary) |
| 클러스터 | Cluster |
| 측정 환경 | Measurement environment |
| 서버 담당자 | System admin |
| VMware 관리자 | VMware admin |
| 하드웨어 담당자 | Hardware team |
| 스토리지 관리자 | Storage admin |
| 가상화·클라우드 관리자 | Cloud and virtualization admin |
| ES 설정 (담당자로 쓰일 때) | Elasticsearch admin |
| 원인 분리 필요 | Isolate the cause |
| 참고 (담당자로 쓰일 때) | Info |
| 응답시간 | latency |
| 처리량 | throughput |
| 큐 사용률 | queue utilization |
| 병목 위치 | bottleneck location |
| VM 안 / VM 바깥 | inside the VM / outside the VM |
| 판정 기준 | thresholds |
| 측정 구간 | measurement window |
| 피크 시간대 | peak hours |
| 매체 | media |
| 논리 디스크 / 구성 디스크 | logical drive / member drive |
| 쓰기 캐시 | write cache |
| 배터리·캐시 보호 모듈 | battery or cache protection module |
| 데이터스토어 | datastore |
| 번들 / 수집기 / 분석기 / 셸 요약 / 리포트 | bundle / collector / analyzer / shell summary / report |
| 추정 | estimated |
| 조치 불필요 | No action needed |
| 미수집 / 미확인 / 해당 없음 / 없음 / 있음 | Not collected / Unknown / N/A / None / Present |
| [Elastic 공식] / [VMware 공식] / [Red Hat 공식] | [Elastic official] / [VMware official] / [Red Hat official] |
| [AWS 공식] | [AWS official] |
| [실무 기준] / [실무] | [Field practice] |
| [OS] / [참고] | [OS] / [Reference] |
