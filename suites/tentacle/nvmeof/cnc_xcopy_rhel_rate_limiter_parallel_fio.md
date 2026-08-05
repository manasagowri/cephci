# RHEL CNC / XCOPY System Test Additions (rate limiter / parallel fio)

These map to automation in `tier-3_nvmeof_xcopy_rhel_perf.yaml`
(`operation: rate_limiter_enforcement` and `operation: fio_parallel_cnc`).

## CNC Rate Limiter Under Overload
**Priority:** P1  |  **Initiator:** RHEL

**Description:** Overload the SPDK CNC path with concurrent large copy
commands and verify `rate-limit-bytes` actually caps CNC bandwidth versus
an unlimited/high baseline.

**Test Steps:**
- Write verified data pattern large enough for several concurrent copies.
- Set `nvmf_cnc_set_config --rate-limit-bytes 1048576` (1 MiB/s) on gateways.
- Launch 4 concurrent non-overlapping `nvme copy --format=2` commands.
- Record wall-clock time and compute throughput T_limited.
- Raise rate limit (e.g. 400 MB/s) and repeat identical workload → T_unlimited.
- Verify each destination region; sample gateway CPU/RSS.

**Expected Results:** All copies succeed with correct data; limited-path
throughput stays near the configured cap; unlimited path is significantly
faster; gateways remain stable.

## Parallel Host Fio IO with CNC
**Priority:** P1  |  **Initiator:** RHEL

**Description:** Run host fio write/read (verify=crc32c) on one LBA region
while CNC copies another non-overlapping region in parallel.

**Test Steps:**
- Partition NSID1: fio region at low LBAs; CNC source at higher LBAs.
- CNC destination on NSID2.
- Start fio write/read verify and CNC copy loop concurrently.
- Wait for both; verify fio region and CNC destination independently.
- Sample gateway resources during the window.

**Expected Results:** Both workloads complete without error; no
cross-contamination or corruption; gateway remains stable.
