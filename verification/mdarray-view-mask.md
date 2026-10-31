# Multidimensional string views and masks

`MDArray.getView(viewExpr)` returns a parent-scoped lazy alias. Brackets accept
integer indexes, half-open `start:stop:step` ranges, an ellipsis, inserted
`newaxis`/`np.newaxis` dimensions, or one quoted compound field. Brackets can be
chained. Negative indexes and steps work; clipped empty ranges fail because
GDAL does not allow size-zero dimensions. Integer indexing can produce scalar
results, whose I/O takes empty start/count arrays. Slicing an already scalar
source remains unsupported; `transpose` already supports scalar axis insertion.

Basic slices retain the stored schema and write through when the source permits
writes. Typed I/O preserves Int64/UInt64, both complex components, nullable text,
JSON-string subtypes and compound fields. Field extraction reads the selected
component; its native implementation has no partial-field write operation.
Field writes fail without a successful parent write. Metadata getters follow
the source, while setters can be unsupported. Inserted axes have zero block
hints. Signed CRS mappings retain their source sign; removed or unmapped axes
map to zero. Reversing a range does not add a new CRS-direction policy.

`MDArray.getMask(options)` requires a numeric source and returns a read-only
Byte view: `0` is invalid and `1` is valid. It combines raw NoData, NaN and CF
`missing_value`, `_FillValue`, `valid_min`, `valid_max`, and `valid_range`.
The two-element `valid_range` overrides separate minimum/maximum attributes.
CF attributes are captured when the mask is constructed. Source samples remain
live, and each read captures the current raw NoData once into owned native
bytes. Removing or changing raw NoData affects an existing mask's next read.

`UNMASK_FLAGS=meaning1,meaning2` selects CF flag meanings. Requested flags match
by OR; all active missing/range conditions still apply. Flags support integer
attribute types up to 32 bits. Missing attributes, unknown meanings, inconsistent
lengths and unsupported flag types fail. Complex masking uses the real component
only: imaginary NaN alone does not invalidate a sample, and NoData equality does
not compare imaginary components. Unscaled views can inherit CF thresholds in
raw units; the mask does not silently convert those thresholds to physical units.

Both constructors snapshot their text inputs before queued work. Embedded NUL
is rejected. Results and descendants depend on the source wrapper and its
ancestors. Closing a result leaves source and siblings usable. CRS copies remain
independent. Cancellation is checked at operation boundaries, and a known newly
allocated wrapper is released on a completion cancellation, observer failure or
malformed result. Neither constructor has a native progress callback, so this
contract does not promise interruption inside GDAL or atomic caller handoff.

## Pinned native corrections

The additional port patch applies after the existing transpose and unscaled
patches on GDAL 3.12.4, with port-version 6. It fixes failed intermediate view
chaining, ellipsis/newaxis rank accounting, checked signed-64 parsing and clipped
range arithmetic, nested step multiplication, and sliced signed CRS mappings.
The parser preserves GDAL's nonempty-dimension rule and supports signed-64
integer tokens; it does not promise Python's arbitrary-precision integer grammar.
The shared parser also serves existing GDAL multidimensional-conversion views,
so that workstream's regressions remain required.

Mask samples and raw NoData compare in the exact Int64/UInt64 type before any
binary64 conversion. Int64/UInt64 CF attributes are retained exactly for those
integer sample paths, with checked cross-signed representability. Floating and
complex sample paths retain the existing double metadata conversion. Other
nonrepresentable criteria remain disabled. Floating-to-integer metadata casts
use safe exclusive upper bounds, Float16 NaN is excluded, and nonfinite or
out-of-range floating flag samples are rejected before uint32 conversion.
Defined in-range flag truncation and integral modulo conversion are preserved.

## Verification scope

`MDArrayViewApiTest` covers aliased exact writes, reversed/noncontiguous slices,
ellipsis and inserted axes, scalar results, clipped ranges, invalid chains and
extreme tokens, text/JSON/compound fields, signed CRS, block hints, read-only
sources and detached CRS lifetimes. Known-dangerous invalid expressions must
only run against a fresh engine containing this patch.

`MDArrayMaskApiTest` covers combined criteria, metadata timing, scalar and array
exact64 sentinel neighbors/extrema, CF64 attributes, safe cast boundaries, half
NaN, complex real-component policy, CF flag forms and failures, read-only masks,
nonnumeric rejection and parent closure. `MDArrayViewMaskWireTest` covers input
snapshots, owned control, queued/completion cancellation, canonical UInt64
handles, malformed results, observer failures and cleanup suppression. Existing
transpose, unscaled and metadata wire regressions remain required.

The scientific/combined driver template closes a compressed NC4 writer before
an independent read-only reopen. Raw Int16 slice/mask composition verifies stored
units and unchanged metadata/data. Additional NC4 Int64/UInt64 arrays and scalars
check exact sentinel neighbors and typed CF64 range/missing criteria. Positive
alias writes use MEM; read-only NC4 slice and mask writes must fail. The explicit
published-consumer copy list includes both new common API test classes.

This is source implementation and fixture preparation. No C17 engine build,
native probe, test worker, CI run or publication has been accepted yet. Fresh
patched engines and applicable source plus independent published consumers are
required on all four JVM classifiers first, followed by the other current
platform contracts. Cross-production, actual ARM64 execution, iOS device
compile/link, simulator execution and Android native-bridge emulation keep their
existing distinct evidence labels. This cohort does not complete the broader
GDAL API/driver/platform goal.
