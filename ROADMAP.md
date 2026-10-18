# Approved feature expansion

User approved the complete proposed roadmap on 2026-10-18 (Asia/Seoul).
Implementation, verification and Ready PRs are authorized. New PR merges require separate approval.

| Workstream | Deliverables | State |
| --- | --- | --- |
| Raster cleanup and contours | Band Sieve, FillNoData, Proximity; Dataset Contour memory/direct output | Local validation passed; PR/full CI pending |
| Interpolation and visibility | GDAL Grid vector-to-raster, Viewshed observer analysis | Implementation and local verification in progress |
| Raster calculation | Windowed arithmetic, explicit NoData policy, reclassification, zone statistics | Pending |
| OGR batch and layers | Bounded feature batches and layer operations, explicit transaction and ownership semantics | Pending |
| Driver profiles | Opt-in HDF5/netCDF, Parquet, PostgreSQL capabilities and per-target dependency/size validation | Pending; assess driver dependency portability before adding profiles |
| Long-running operations | Progress reporting and cancellation across JNI, Native and web workers | Pending |
| Input transport | Explicit timeout/retry/cancellation and on-demand range safeguards | Pending |

Each PR must preserve the existing target/publication policy. Tests must exercise real GDAL operations and independent published consumers. Current exact HEAD CI success and fresh review/rules/base gates are required for Ready. CI success is separate from remote package publication.
