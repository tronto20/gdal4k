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
| 4 | Multidimensional numeric transfer | Merged in PR33: protocol 5, controlled same-class JVM comparison, full exact-HEAD CI and tested HEAD main ancestry verified |
| 5 | JVM/Native HTTP input | Merged in PR34: optional CURL profile, actual Linux/Mac source/published runtime and controlled loopback comparison, full exact-HEAD CI and tested HEAD main ancestry verified; OS RSS increased |
| 6 | Valid-data raster footprint | Merged in PR35: valid-mask/NoData polygons, holes, memory/direct outputs, actual source/published runtime and functional diagnostics; full exact-HEAD CI and tested HEAD main ancestry verified |
| 7 | Raster tile index | Merged in PR36: explicit affine extent/location index, full current source/published runtime and diagnostics; exact-HEAD primary13/profiles7 and tested HEAD main ancestry verified |
| 8 | Multidimensional conversion | In implementation: GDALMultiDimTranslate, explicit selection/type/shape/metadata loss limits, MEM/GTiff and scientific OS NC4 coverage; current producer/runtime/full CI pending |

These eight workstreams are milestones toward the broader [full GDAL support goal](GDAL_SUPPORT.md),
not its completion definition. JVM on its four current OS/ABIs is the first support priority;
coverage on the other existing targets follows with actual platform execution evidence.
Isolated optional driver profiles do not establish a combined JVM distribution or complete
GDAL API/driver support.

Performance claims require identical-input/environment speed, cumulative allocation and OS peak
RSS diagnostics with raw logs/hashes. Functional additions require correctness/error/lifetime
coverage and representative diagnostics. Preserve the default target/dependency/publication
policy. Verify actual source and independent published consumers. CI success and merge are
separate from remote package publication. Workstreams advance only after the previous tested
HEAD is confirmed in fetched main. Future worktrees/branches are retained unless separately
authorized for cleanup.
