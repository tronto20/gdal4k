# Approved improvement program

The user authorized implementation, separate PRs, measured verification, full exact-HEAD CI,
Ready and sequential expected-SHA merges on 2026-10-18 (Asia/Seoul). Current main integration,
fresh reviews/rules/checks and tested-HEAD main ancestry remain mandatory for every PR.

Raster analysis, Grid/Viewshed, raster calculation, OGR layer/feature batches, optional driver
profiles, progress/cancellation and web input policies already have APIs on main. See README
and the driver profile documentation for their supported semantics and verification limits;
these are not missing APIs to reimplement.

| Order | Workstream | State |
| --- | --- | --- |
| 1 | Coordinate array transfer | Merged in PR29; tested HEAD confirmed in main |
| 2 | Vector column transfer | Merged in PR31: scoped GDAL Arrow C stream; controlled JVM measurements and full CI verified |
| 3 | File I/O copy reduction | Merged in PR32: negotiated binary file buffers, controlled JVM comparison, full exact-HEAD CI and tested HEAD ancestry verified |
| 4 | Multidimensional numeric transfer | Existing MDArray/MDValue/attribute JSON baseline measured and preserved; compatible optimization next, no runtime changes yet |
| 5 | JVM/Native HTTP input | Approved; extend existing web policies and compare actual bounded ranges |
| 6 | Valid-data raster footprint | Approved; wrap GDALFootprint with mask/NoData semantics |
| 7 | Raster tile index | Approved; wrap GDALTileIndex for explicit input locations |
| 8 | Multidimensional conversion | Approved; wrap GDALMultiDimTranslate with explicit type/shape/loss limits |

Performance claims require identical-input/environment speed, cumulative allocation and OS peak
RSS diagnostics with raw logs/hashes. Functional additions require correctness/error/lifetime
coverage and representative diagnostics. Preserve the default target/dependency/publication
policy. Verify actual source and independent published consumers. CI success and merge are
separate from remote package publication. Workstreams advance only after the previous tested
HEAD is confirmed in fetched main. Future worktrees/branches are retained unless separately
authorized for cleanup.
