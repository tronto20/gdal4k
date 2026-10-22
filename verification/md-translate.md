# Multidimensional conversion

The implementation wraps pinned GDAL 3.12.4 [GDALMultiDimTranslate](https://github.com/OSGeo/gdal/blob/v3.12.4/apps/gdal_utils.h)
and its [option and conversion implementation](https://github.com/OSGeo/gdal/blob/v3.12.4/apps/gdalmdimtranslate_lib.cpp).
It uses existing multidimensional dataset, array and typed-value APIs; these were already
available before this conversion wrapper. This is one milestone in [GDAL_SUPPORT.md](../GDAL_SUPPORT.md),
with JVM first and no claim that the complete GDAL surface is now supported.

`Dataset.multiDimTranslate(arguments)` returns an independent MEM dataset. The source handle
remains caller-owned, and can be closed after completion without invalidating the result.
The whitelist permits array name/destination/transpose/view, group name/destination/recursion,
coordinate `-subset` and step-based `-scaleaxes`. GDAL applies transpose before view. For example,
`name=cube,dstname=selected,transpose=[0,2,1],view=[1,:,:]` converts `(time,y,x)` into `(x,y)`
by selecting the second time plane after the spatial permutation. Selection uses GDAL's
file reopening path, so the source and explicit VRT dependencies must remain readable through
completion. Anonymous MEM inputs support full copy; they cannot be reopened for selectors.

Full memory copies retain arrays/groups, exact stored types and values, attributes, raw typed
NoData, units, scale/offset and dimension indexing variables. Scale and offset are metadata,
not an instruction to rescale values. Typed `readValues` is required for exact wide integers,
complex and structured values. Numeric coordinate subsets use GDAL's Float64 comparison
domain. All Int64/UInt64 and complex coordinate arrays are rejected for value-based subsets,
even when individual integer values fit in Float64. String coordinates, including date
strings, use lexicographic selection. Value-based subsets require an indexing variable;
index-based views preserve the selected array's original exact value type. Resampling,
output type conversion, autoscale and output
override/overwrite options are rejected rather than silently dropping information.

Some drivers expose array-local axes without registering them in the source group's dimension
inventory. Pinned GDAL materializes these axes as destination group dimensions, reuses an
existing same-name/equal-size dimension and prefixes differing-size name collisions with the
array name. Validation preserves the registered source dimensions and each array's axis
metadata and values; it does not promise unchanged group inventory or global alias identity.

`multiDimTranslateFiles` returns complete concrete output buffers. Initial GTiff output is
restricted to one two-dimensional numeric array without group/array attributes, indexing
variables or unsupported metadata. It is not a portable serializer of arbitrary MD groups.
`multiDimTranslateTo` writes a new direct file. Existing paths are rejected, and partial files
after a failure or cancellation belong to the caller. Optional netCDF output requires its
scientific engine profile, NC4 format and a real OS path. netCDF cannot write to `/vsimem/`,
so it is not accepted by the buffer-returning API. Bounded strings are rejected because the
NC_CHAR representation introduces a dimension. Driver normalization that changes stored
types/shape/values/NoData/attributes is an error, not successful lossy conversion. Conversion
closes and reopens file outputs for validation; extra driver-created metadata may be present
only when the source metadata has been retained exactly.

Common tests cover typed metadata/tree copying and independent ownership, file-backed
selection/permutation/index views, coordinate subset and odd step behavior, group selection,
GTiff memory/direct outputs and existing-output ownership, cancellation/callback failures,
and invalid or lossy requests. They are copied to independent published consumers. A JVM
case uses actual OS files and an additional JVM case checks native GDAL progress callbacks.
The optional scientific fixture extends its existing actual HDF5/netCDF checks with HDF5
to MEM and MEM to an owned OS NC4 output, then closes the writer and reopens/validates the
stored type, shape, wide integer/complex/string values, typed NoData, attributes and scale/offset.
Scientific execution is scoped to the actual Linux x64 and macOS ARM64 source/published
JVM/Native profile hosts; no other HDF5/netCDF platform support is inferred.

`MultiDimTranslateMemoryProbe` and `md-translate-memory-report.py` use a retained, self-contained
OS VRT file with shape `(3, side+7, side+11)`, default `side=128`, Float64 values
`(time*1000000 + y*1000 + x + 1)/4`. The file writer closes before GDAL opens the source.
Three alternating trials of memory/files/direct each launch a fresh JVM, for nine processes
per host. Each process performs one warmup and three measured rounds. The same selector
produces shape `(side+11, side+7)`; every exact value/type/shape is checked after closing the
input handle. An independent closed-form weighted checksum checks the transposed time=1
plane. The Python auditor parses every retained VRT value and retains stdout/OS time logs,
fixture/class/source/native hashes, manifest identity and settings. CI uploads probe class
bytes, complete raw logs and fixture XML.

Elapsed time and allocation include source opening/closing, conversion and wrapper postflight,
output reopening/full-value validation/closing, and direct deletion. Fixture setup and warmup
are excluded from elapsed/allocation but included in whole-process OS peak RSS. Cumulative
JVM allocation, heap sampled every 2ms and OS RSS are different measurements; the sampler can
miss peaks, and native allocations are outside the JVM allocation counter. Logical result
bytes mean array element bytes, not serialized MEM bytes. The three result APIs are not a
before/after improvement comparison. Public buffers and GDAL intermediate arrays remain;
there is no streaming, zero-copy, total-memory bound or universal/Native/Web/Android/scientific
performance claim.

Source/template compilation is separate from actual pinned producer/runtime validation.
Every support claim requires current source and independent published execution, these raw
diagnostics and the exact-HEAD workflow/artifact gates. Preserve the actual results and merge
proofs separately from this source contract.
