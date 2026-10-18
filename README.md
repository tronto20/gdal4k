# gdal4k

GDAL 3.12.4를 내장한 Kotlin Multiplatform 라이브러리입니다. JVM, Android, Kotlin/Native, JS, wasmJs에서 같은 `suspend` API를 사용합니다. GDAL 설치, 데이터 디렉터리 지정, 웹 엔진 CDN 설정은 필요하지 않습니다. WASI는 제외합니다.

JVM 기본 의존성에는 바이너리가 없습니다. 아래 플랫폼 좌표 중 필요한 것만 명시적으로 추가합니다. 선택한 JAR에 GDAL·JXL·PROJ 데이터가 포함되므로 별도 설치나 경로 설정은 필요하지 않습니다.

Android는 API 28 이상, `arm64-v8a`만 지원합니다. Android x86/x86_64와 32비트 ARM은 지원하지 않습니다.

Apple 타깃은 `macosArm64`, `iosArm64`, `iosSimulatorArm64`입니다. macOS와 iOS 시뮬레이터는 Apple Silicon만 지원합니다. 실제 iPhone·iPad용 `iosArm64`와 시뮬레이터용 `iosSimulatorArm64`는 SDK·플랫폼이 달라 별도 바이너리를 제공하며, Gradle이 소비 앱의 타깃에 맞는 아티팩트를 선택합니다.

```kotlin
commonMain.dependencies {
    implementation("dev.tronto.gdal4k:gdal4k-runtime:0.1.0-SNAPSHOT")
    implementation("dev.tronto.gdal4k:gdal4k-engine:0.1.0-SNAPSHOT")
}
```

일반 JVM 앱의 예시입니다(Linux x64/glibc).

```kotlin
dependencies {
    implementation("dev.tronto.gdal4k:gdal4k-runtime:0.1.0-SNAPSHOT")
    runtimeOnly("dev.tronto.gdal4k:gdal4k-engine:0.1.0-SNAPSHOT:linux-x64-gnu")
}
```

| JVM 실행 환경 | `gdal4k-engine`의 classifier |
| --- | --- |
| Linux x64 / glibc (Ubuntu 등) | `linux-x64-gnu` |
| Linux ARM64 / glibc | `linux-arm64-gnu` |
| macOS Apple Silicon | `macos-arm64` |
| Windows x64 | `windows-x64` |

KMP 앱에서는 같은 `runtimeOnly`를 `jvmMain.dependencies`에 추가합니다. 모든 아티팩트는 같은 라이브러리 버전 `0.1.0-SNAPSHOT`을 사용합니다. GDAL 버전은 빌드 설정과 엔진 manifest에서 별도로 관리합니다. 여러 OS로 배포하는 앱은 필요한 좌표를 여러 개 추가할 수 있고, 런타임은 현재 OS/CPU에 맞는 리소스만 로드합니다. 미선택 또는 다른 플랫폼용만 선택했다면 `runtime()`에서 필요한 좌표를 안내하며 실패합니다. 자동 다운로드나 시스템 GDAL fallback은 없습니다. Alpine/musl은 지원하지 않습니다.

`gdal4k-runtime`은 엔진을 포함하지 않습니다. KMP 앱은 `commonMain`에서 runtime과 엔진 루트를 선언하면 Native·Android·JS·wasmJs에 맞는 엔진을 Gradle이 선택합니다. JVM은 배포할 OS·CPU·ABI를 classifier로 추가합니다.

```kotlin
kotlin {
    sourceSets {
        commonMain.dependencies {
            implementation("dev.tronto.gdal4k:gdal4k-runtime:0.1.0-SNAPSHOT")
            implementation("dev.tronto.gdal4k:gdal4k-engine:0.1.0-SNAPSHOT")
        }
        jvmMain.dependencies {
            runtimeOnly("dev.tronto.gdal4k:gdal4k-engine:0.1.0-SNAPSHOT:macos-arm64")
        }
    }
}
```

`gdal4k-engine`은 Native 타깃별 정적 라이브러리, 웹 Worker·Wasm 자산 또는 Android ARM64 JNI AAR을 선택합니다. JVM의 기본 variant에는 바이너리가 없으며 classifier를 명시해야 합니다. 복수 classifier를 추가하면 JVM 결과물에 선택한 OS 바이너리가 모두 포함됩니다. Compose Desktop의 플랫폼별 패키지에는 배포 대상 classifier 하나를 선택합니다. Compose에 의한 GDAL 바이너리 자동 제거는 가정하지 않습니다.

Android 엔진은 `arm64-v8a`만 제공하며 초기화 시 ARM64 실행을 지원하지 않는 환경과 32비트 프로세스를 거절합니다. 소비 앱의 ABI 설정은 AAR에서 강제할 수 없으므로 앱 배포 대상도 ARM64로 제한합니다. x86_64 네이티브 엔진은 제공하지 않습니다.

선택 후에는 모든 플랫폼에서 `Gdal4k.runtime()`으로 자동 로딩하며 GDAL 설치나 경로 설정이 필요하지 않습니다. 각 Native 타깃은 자기 플랫폼 엔진만 링크합니다. 여러 타깃을 빌드하면 필요한 각 엔진을 받지만 하나의 결과물에 모든 Native 엔진을 넣지는 않습니다.

main CI는 전체 검증이 통과하면 Maven Central을 기본 대상으로 게시합니다. 정식 버전은 서명·업로드·최종 공개까지 자동으로 진행하고 `PUBLISHED` 상태를 확인합니다. 현재 `SNAPSHOT` 버전은 Sonatype Snapshot 저장소에 게시되므로 소비 앱에도 다음 저장소를 추가합니다. 아직 게시하지 않은 버전은 [개발·검증 절차](gdal4k-engine/README.md)에 따라 로컬 Maven 저장소에서 사용합니다.

```kotlin
repositories {
    mavenCentral()
    maven("https://central.sonatype.com/repository/maven-snapshots/") {
        content { includeGroup("dev.tronto.gdal4k") }
    }
}
```

```kotlin
import dev.gdal4k.Gdal4k
import dev.gdal4k.runtime.*

suspend fun describe(bytes: ByteArray): String {
    val gdal = Gdal4k.runtime() // 첫 호출에서 자동 초기화
    return gdal.openDataset("input.tif", bytes).use { dataset ->
        gdal.datasetInfo(dataset, DatasetInfoOptions(listOf("-json")))
    }
}
```

`runtime()`은 `suspend` 함수이며 첫 호출에서 초기화하고 이후 같은 런타임을 반환합니다. 별도 `prepare()`는 선택적인 사전 로딩용입니다. 데이터셋은 `use`로 닫아 쓰기를 완료하고 자원을 정리합니다. 전역 `close()`는 앱 실행 중 엔진 컨텍스트·Worker를 명시적으로 반환할 때 선택적으로 호출합니다. 앱 종료 훅으로 반드시 호출할 필요는 없으며, 공유 런타임을 작업마다 닫지 않습니다. 명시적으로 닫은 뒤 다시 `runtime()`을 호출하면 새로 초기화됩니다.

- `gdal4k`: `GdalRuntime`, `Dataset`, `Band`, OGR·SRS 인터페이스와 공통 파일 값 타입·편의 함수. 구현·엔진에 의존하지 않습니다.
- `gdal4k-engine:<version>:<classifier>`: JVM 앱이 선택하는 리소스 JAR 4종. `gdal4k-jvm-binaries`에서 만들고 엔진 루트 좌표의 classifier로 게시합니다.
- `gdal4k-runtime`: `Gdal4k.runtime()` 진입점, 플랫폼별 Kotlin 구현·로더와 통합 테스트. `gdal4k` 인터페이스를 전이 의존성으로 제공하며 모든 플랫폼에서 엔진을 포함하지 않습니다.
- `gdal4k-engine`: Native·Android·웹 엔진을 선택하는 KMP 루트 좌표. JVM은 classifier를 추가합니다. 타깃별 실제 게시 이름은 [엔진 좌표 표](gdal4k-engine/README.md#산출물과-게시)를 참고하세요.
- `gdal4k-engine-android-arm64`: JNI 바이너리를 전달하는 Android AAR.

웹에서는 URL을 바로 열면 HTTP Range로 필요한 구간만 읽습니다. 브라우저에서 선택한 `File`도 `jsMain` / `wasmJsMain`의 `runtime.openDataset(file)`로 열 수 있습니다. Node의 로컬 경로는 디스크를 직접 읽습니다. 기존 바이트 입력은 이미 메모리에 있는 데이터에 사용합니다.

바이트 입력도 `runtime.openDataset("input.tif", bytes)`로 열고 기존 `Dataset`을 사용합니다. `translate`·`warp`·`translateVector`는 이 데이터셋의 메서드입니다.

[API·비동기 실행·지원 범위](gdal4k-runtime/README.md), [빌드·게시 산출물](gdal4k-engine/README.md), [별도 JVM 소비 앱](sample-cli/README.md)을 참고하세요. `verification`의 초기 접속성 probe는 과거 진단용이며, 제품 검증은 실제 라이브러리의 `commonTest`와 `verification/consumer.py`를 사용합니다.

### 여러 입력의 VRT·Warp

```kotlin
runtime.buildVRT(listOf(left, right)).use { mosaic ->
    mosaic.translateTo("mosaic.tif", driver = "COG")
}
runtime.warpTo(listOf(left, right), "projected.tif", target = "EPSG:3857")
val files = runtime.warp(listOf(left, right), target = "EPSG:3857")
```

`buildVRT`는 픽셀을 복사하지 않는 live Dataset을 반환합니다. 입력 핸들을 먼저 닫아도
VRT가 사용하는 네이티브 Dataset, 메모리 작업공간, Android URI descriptor·directory, 웹 File/HTTP/Node 입력 mount는 유지됩니다.
마지막 의존 VRT를 닫으면 함께 해제됩니다. 원본 파일 자체를 외부에서 삭제하거나 변경하는
동작까지 막지는 않습니다. 임시 원본 경로가 들어간 XML을 독립적인 결과물로 반환하지 않습니다.
입력 불일치는 기본 `-strict`로 실패하며, GDAL의 입력 건너뛰기는 `-non_strict`를 명시해야 합니다.

다중 입력 Warp는 목록 순서대로 처리하며 겹치는 곳은 뒤 입력의 유효 픽셀이 우선합니다.
NoData/마스크·해상도·extent·리샘플링은 GDAL 옵션을 `arguments`로 전달합니다. `warp`는
결과 파일을 메모리에 반환하고 `warpTo`는 경로에 직접 기록합니다. 큰 결과는 `warpTo`를
사용합니다. 웹의 직접 출력은 Node 파일 경로 또는 브라우저 `opfs:/`를 사용합니다.
이 두 Warp API는 실제 파일 형식에 기록하며 VRT/MEM 출력은 지원하지 않습니다.
실패·취소 후 부분 출력 파일은 호출자가 정리합니다. 기존 단일 Dataset 변환 API는 유지됩니다.

### Band 색상표·RAT·블록 접근

`runtime.createColorTable()`은 수정 가능한 색상표를 만듭니다. `band.getColorTable()`은
Band와 수명이 분리된 복사본을 반환하므로 변경을 반영하려면 `band.setColorTable(table)`을
호출합니다. `use` 또는 `close()`로 소유한 색상표를 해제합니다.

`runtime.createRasterAttributeTable()`은 Integer/Real/String 열을 가진 RAT를 만듭니다.
`readIntegerValues(column, startRow, count)` 등의 열 구간 API는 요청한 행만 읽습니다.
`band.getDefaultRAT()`은 전체 표를 복사하지 않는 참조입니다. Band/Dataset이 닫히거나
`setDefaultRAT`으로 표를 교체하면 기존 참조는 무효화됩니다. 독립 사본은 `cloneTable()`로
명시적으로 만듭니다. 변경의 파일 반영 여부는 `changesAreWrittenToFile()`과 드라이버 정책을
따르며, 자동 반영되지 않는 표는 `setDefaultRAT`으로 다시 연결합니다. Boolean/DateTime/WKB의
전용 RAT 접근은 아직 노출하지 않습니다. 값 읽기·쓰기는 열의 형식과 일치하는 메서드를
사용하며 암시적인 문자열/숫자 변환을 하지 않습니다.

`readBlock`/`writeBlock`은 변환 없는 **원래 픽셀 형식의 바이트**를 사용합니다. 버퍼는
`getBlockSize()`의 폭 × 높이 × 픽셀 바이트 수를 담아야 하며 다중 바이트 값은 네이티브
바이트 순서를 따릅니다. `getActualBlockSize`로 가장자리 유효 영역을 구할 수 있고,
읽을 때 유효 영역 밖 패딩은 0으로 채웁니다. 큰 버퍼의 나머지는 보존합니다.
타입 변환이나 리샘플링에는 기존 `readRaster`/`writeRaster`를 사용합니다. 직접 블록 접근
전에 대기 중인 RasterIO 쓰기를 flush하며, 직접 쓰기는 기존 블록 캐시도 비웁니다.

### OGR 속성 필드 스키마 편집

```kotlin
import dev.gdal4k.runtime.*

// 업데이트 가능한 Dataset의 Layer에서, 활성 Feature를 모두 닫은 뒤 실행합니다.
layer.getLayerDefn()!!.use { definition ->
    definition.getFieldDefn(0)!!.use { original ->
        original.cloneFieldDefn().use { replacement ->
            replacement.setName("label")
            check(layer.alterFieldDefn(0, replacement, FieldAlterFlags.Name) == 0)
        }
    }
}
check(layer.reorderFields(intArrayOf(2, 0, 1)) == 0) // new[i] = old[map[i]]
check(layer.deleteField(2) == 0)
```

`deleteField`, `reorderField(s)`, `alterFieldDefn`은 OGR 드라이버의 실제 스키마 변경을
호출합니다. `createField`도 같은 수명 검사와 복사 정책을 따릅니다. 레이어에서 만든 Feature와
그 clone은 모두 먼저 닫고, 속성 필터를 해제하고, 같은 Dataset의 SQL 결과도 닫아야 합니다.
저장된 피처 자체를 삭제하는 기능은 아니며, 기존 행의 값 변환·지원 여부는 드라이버 정책입니다.
`testCapability("DeleteField" / "ReorderFields" / "AlterFieldDefn")`로 지원 여부를 조회할 수 있습니다.

잘못된 인덱스·순열·flags는 호출 전에 거부합니다. 드라이버 호출까지 진행했다면 성공 여부와
관계없이 기존 borrowed FeatureDefn/FieldDefn 핸들을 폐기하므로 정의는 다시 조회해야 합니다.
독립 `cloneFieldDefn()`은 원본을 닫거나 스키마를 바꿔도 사용할 수 있습니다. borrowed 정의의
setter나 FeatureDefn 직접 구조 변경은 거부하며, clone을 편집해 Layer API에 전달합니다.
flags는 `FieldAlterFlags`를 `or`로 조합합니다. 별도 자동 트랜잭션·형 변환 fallback·복구는 없고,
GDAL 드라이버 실패는 nonzero 오류 코드로 반환합니다. 파일 전체를 bridge로 복사하지 않습니다.

### OGR Geometry 생성과 공간 조회

```kotlin
val runtime = Gdal4k.runtime()
runtime.createGeometryFromWkt("POLYGON ((0 0,2 0,2 2,0 2,0 0))").use { source ->
    runtime.createGeometryFromGeoJson(
        """{"type":"Polygon","coordinates":[[[1,1],[3,1],[3,3],[1,3],[1,1]]]}"""
    ).use { other ->
        source.intersection(other)?.use { intersection -> println(intersection.getArea()) } // 1.0
    }
}
```

`createGeometryFromWkt` / `createGeometryFromWkb` / `createGeometryFromGeoJson`는
호출자가 `use`로 닫는 독립 Geometry를 만듭니다. GeoJSON은 Geometry 객체를 받으며
Feature/FeatureCollection은 받지 않습니다. 선택적인 `spatialReference`와
`Geometry.assignSpatialReference`는 CRS 메타데이터만 붙이고 좌표를 바꾸지 않습니다.
`getSpatialReference()`는 원본을 닫아도 사용할 수 있는 독립 복사본을 반환합니다.

`intersection` / `union` / `difference`는 현재 좌표 단위의 평면 GEOS 연산입니다.
입력은 보존하며 결과는 독립 소유입니다. 자동 재투영이나 도형 복구를 하지 않고,
서로 다른 CRS가 양쪽에 명시되어 있으면 거부합니다. 한쪽 CRS가 없을 때 좌표계가 맞는지는
호출자가 확인해야 합니다. 결과가 없으면 null 또는 빈 Geometry이며, 연산 실패는 예외입니다.

```kotlin
runtime.openDataset("areas.gpkg").use { dataset ->
    requireNotNull(dataset.getLayer(0)).use { layer ->
        check(layer.setSpatialFilterRect(0.0, 0.0, 10.0, 10.0) == 0)
        println(layer.getFeatureCount(true))
        check(layer.setSpatialFilter(null) == 0) // 공간 조건 해제
    }
}
```

`setSpatialFilter(geometry)`는 입력을 복사하고, `getSpatialFilter()`도 독립 복사본을
반환합니다. 기본 geometry field의 필터만 다루며 필터 변경 시 읽기 커서를 초기화합니다.
이미 같은 필터 상태에서 되감으려면 `resetReading()`을 명시적으로 호출합니다.
공간 조건은 속성 필터와 함께 적용됩니다. GDAL/드라이버에 따라 envelope 후보가 포함될 수
있으므로 엄밀한 교차 판정이 필요하면 피처 Geometry로 추가 확인합니다. `getFeature(fid)`는
필터를 적용하지 않는 임의 접근입니다. 필터 좌표는 레이어 CRS 단위여야 하며 알려진 CRS
혼용을 거부합니다. 사각 범위는 유한한 값과 min≤max를 요구합니다.

### 분류 래스터를 폴리곤으로 변환 (Polygonize)

```kotlin
import dev.gdal4k.runtime.*

runtime.openDataset("classes.tif").use { source ->
    val files = source.polygonize("-8", driver = "GeoJSON", name = "classes.geojson")
    source.polygonizeTo("classes.gpkg", "-lco", "FID=polygon_id", layerName = "classes")
}
```

`polygonize`는 독립적인 `GdalFiles`, `polygonizeTo`는 OS/Node 파일 또는 브라우저
`opfs:/` 경로에 직접 기록합니다. 기본값은 첫 번째 밴드, GPKG 출력, `polygons` 레이어,
`value` 속성입니다. `band`, `mask`, `layerName`, `fieldName`, `driver`, `name`은 named
argument로 지정합니다. 동적 옵션은 `arguments = listOf(...)`로도 전달할 수 있습니다.

기본 연결은 상하좌우 4방향이며 `-8`은 대각선을 포함합니다. 기본 GDAL 마스크는 NoData와
유효성 마스크를 반영합니다. `-nomask`는 이를 무시하며, `mask = maskBand`는 명시적 마스크로
대체합니다(0은 제외, 나머지는 포함). 명시적 마스크의 크기는 입력 밴드와 같아야 합니다.
출력에는 입력 GeoTransform과 CRS를 사용합니다. 정수는 Int64 속성, 실수는 Real 속성으로
기록하여 실수를 정수로 자르지 않습니다. UInt64와 복소수는 지원하지 않습니다.
실수의 영역 판정은 GDALFPolygonize의 Float32 버퍼와 근접값 비교 규칙을 따르므로,
Float64의 모든 유효 숫자나 서로 매우 가까운 값의 분리는 보장하지 않습니다.
Int64 값은 GDAL의 내부 double 변환을 우회해 원본 값을 기록합니다. GeoJSON에는 정확한
정수 텍스트를 쓰지만 JSON 소비자나 드라이버의 실수형 추론으로 재조회 시 정밀도가 달라질
수 있으므로, 64비트 정수의 정확한 왕복에는 GPKG를 사용합니다.

지원 문자열 옵션은 `-8`, `-nomask`, `-dsco KEY=VALUE`, `-lco KEY=VALUE`입니다.
GDAL Python `gdal_polygonize` CLI의 모든 옵션을 파싱하는 API는 아닙니다. 입력 핸들은
호출자가 소유하고, 기존 출력 파일은 거부합니다. 실패·취소 후 부분 출력과 sidecar는 호출자가
관리합니다. 연속적인 사진 픽셀보다 분류·마스크 래스터에 적합하며, 복잡한 경계는 많은
폴리곤과 작업 메모리를 요구할 수 있습니다.

### Terrain derivatives (DEM processing)

`Dataset.demProcessing(processing, driver, name, arguments)` calls GDALDEMProcessing
and returns an independent `GdalFiles` result. `demProcessingTo(path, processing,
driver, arguments)` writes directly to an OS/Node file or a browser `opfs:/` path.
Supported modes are `hillshade`, `slope`, `aspect`, `TRI`, `TPI` and `roughness`
(case insensitive). Both APIs require a concrete output format; MEM/VRT/stream,
color-relief palette input and progress callbacks are not exposed in this API.

```kotlin
val files = dem.demProcessing("slope", "-s", "1")
dem.demProcessingTo("opfs:/terrain/slope.tif", "slope", "-p", "-compute_edges")
```

Arguments are the selected gdaldem mode's options, including band selection,
algorithm, scaling, edge handling and creation options. GDAL determines NoData,
CRS/band-unit scaling and each algorithm's output values; this wrapper does not
apply band scale/offset. Use explicit `-s` or `-xscale`/`-yscale` when source units
are ambiguous. Terrain analysis in a geographic CRS may require reprojection.
The source stays caller-owned. Direct outputs and sidecars, including partial
outputs after failure/cancellation, belong to the caller. Direct output avoids
copying the resulting file into a `GdalFiles` buffer.

### GDAL 진행률과 협력적 취소

Sieve·Proximity·FillNoData·Contour·Grid·Viewshed와 7개 레이어 overlay도 GDAL 자체 콜백에 연결됩니다. 진행률은 호출별 표본이며 연산이나 드라이버에 따라 중간 알림이 없을 수 있습니다. 제자리 수정과 이미 생성한 출력·레이어 스키마는 취소로 되돌리지 않습니다.

```kotlin
val files = withGdalProgress({ progress ->
    // 콜백은 짧고 thread-safe해야 합니다. UI에는 Flow/Channel 등으로 전달하세요.
    println("${progress.operation}: ${progress.fraction * 100}% ${progress.message}")
    true // false를 반환하면 현재 native 호출의 취소를 요청
}) {
    source.warp("EPSG:3857", "-r", "bilinear", driver = "GTiff")
}
```

`withGdalProgress`는 시그니처를 바꾸지 않고 블록의 각 GDAL 호출을 관찰합니다. 각 호출마다 fraction0~1과 메시지를 새로 보고하며, 빠른 호출은 마지막 샘플만 전달할 수 있습니다. GDAL이 중간 콜백을 제공하지 않는 드라이버·작업은 호출 경계에서만 취소를 확인합니다. Translate/Warp/VectorTranslate/DEM/Rasterize/BuildVRT/Polygonize, CreateCopy/overviews, band statistics/histograms와 RasterIO의 실제 GDAL 콜백을 연결합니다. 여러 단계의 fraction을 전체 블록의 선형 완료율로 해석하지 않습니다.

긴 작업은 관찰자를 설치하지 않아도 호출자 Job 취소를 확인합니다. 관찰자에서 false를 반환하거나 코루틴을 취소하면 CancellationException을 전달하되, native 실행 종료와 정리를 기다립니다. 관찰자 예외는 GDAL UserInterrupt로 바꾸지 않고 원래 오류(코루틴 stacktrace 복원 시 cause 포함)를 보존합니다. 콜백은 같은 런타임을 다시 호출하거나 차단하지 않아야 합니다. 서로 다른 런타임은 동일 관찰자를 동시에 호출할 수 있습니다. 중간 콜백 사이의 취소 지연이나 드라이버의 무응답을 강제 중단으로 해결하지 않습니다.

JNI·Native는 독립 control의 atomic 취소/진행률을 읽으며 GDAL native 콜백에서 Kotlin을 재진입하지 않습니다. 웹 Worker는 진행률 콜백에서 ordinary single-thread Wasm을 yield하고 취소 메시지를 실행 큐 밖에서 처리합니다. 작업·입력 핸들이 살아 있는 동안 Worker를 종료하거나 native 스레드를 강제 종료하지 않습니다. 취소/관찰자 실패 직전에 만들어진 반환 핸들도 모두 닫은 뒤 예외를 전달합니다. VRT의 입력 lease, URI/Node/OPFS mount와 directory descriptor는 실제 Dataset 해제 시점까지 유지됩니다.

입력은 호출자 소유이며 이미 쓴 출력·cursor·metadata 변경은 rollback하지 않습니다. 직접 출력의 부분 파일도 호출자가 정리합니다. HTTP·파일 mount 단계의 전송 정책은 별개이며, native 진행률 자체가 전송 제한 시간이나 무조건적인 네트워크 중단을 제공하지는 않습니다.

선택적 HDF5·netCDF, Parquet, PostgreSQL 엔진은 [드라이버 프로필](gdal4k-engine/DRIVER-PROFILES.md)에서 별도 버전·로컬 Maven 아카이브로 빌드합니다. 기본 패키지와 타깃은 유지하며, 추가 드라이버 지원은 해당 프로필의 실제 CI 검증 범위에 한정합니다.

### OGR 레이어 연산과 제한된 피처 일괄 처리

`overlayTo`는 두 입력 레이어를 새 결과 레이어로 Intersection/Union/SymDifference/Identity/Update/Clip/Erase 합니다. 두 입력은 독립된 native 레이어 커서여야 합니다. 같은 Dataset.getLayer로 다시 얻은 핸들도 같은 레이어이면 거부합니다. 서로 다른 레이어 사이에서도 커서를 공유하는 드라이버는 별도로 연 입력 Dataset을 사용해야 합니다. 결과도 두 입력과 달라야 하며, 쓰기·필드 생성이 가능하고 속성 필드·피처·필터가 없어야 합니다. 먼저 결과 레이어의 살아 있는 Feature와 같은 데이터셋의 SQL 결과를 닫습니다. 결과 스키마의 빌린 정의 핸들은 드라이버 호출 전에 무효화되므로 실패 코드 이후에도 다시 조회합니다. 좌표계가 양쪽에 알려져 있는데 다르면 거절합니다. GEOS 평면 연산이며 자동 재투영·도형 복구·트랜잭션은 없습니다. 기본 geometry field를 사용하고 입력 커서/필터에는 GDAL 의미가 적용됩니다.

```kotlin
val code = input.overlayTo(method, output, LayerOverlayOperation.Intersection,
    "INPUT_PREFIX=left_", "METHOD_PREFIX=right_")
check(code == 0)

layer.forEachFeatureBatch(batchSize = 256) { features ->
    // 이 콜백을 벗어나면 모든 임시 Feature가 닫힙니다. 보관하려면 안에서 cloneFeature().
    for (feature in features) println(feature.getFID())
}
```

`readFeatureBatch(limit)`는 현재 필터·커서에서 최대 limit개를 읽고 소유권을 호출자에게 줍니다. 빈 목록은 EOF이며 실제 GDAL 읽기 오류는 예외로 구별합니다. 모든 반환 Feature를 닫아야 합니다. `forEachFeatureBatch`는 기본적으로 먼저 rewind하고 매 콜백 뒤 실패·취소 시에도 Feature를 정리합니다. 같은 레이어를 다른 코루틴에서 동시에 순회·변경하지 않습니다.

`writeFeatureBatch`는 Create/Replace/Upsert를 순차 호출하고 첫 오류에서 `completed`와 `errorCode`를 반환합니다. 이전 쓰기와 GDAL의 FID 변경은 유지되고 입력 핸들은 닫지 않습니다. 원자성이 필요하면 명시적으로 지원되는 데이터셋 트랜잭션을 사용하고 실패 코드를 확인해 예외를 던집니다.

```kotlin
dataset.flushCache() // GPKG처럼 새 테이블 DDL을 미루는 드라이버의 스키마를 먼저 확정
// 다른 작업과 같은 Dataset 트랜잭션을 공유하지 않습니다.
dataset.withTransaction {
    val result = output.writeFeatureBatch(features, FeatureWriteMode.Create)
    check(result.successful) { "Written ${result.completed}, GDAL error ${result.errorCode}" }
}
```

`withTransaction`은 start 성공 뒤에만 트랜잭션을 소유하며 성공 시 commit, 블록 실패·취소·commit 오류 시 rollback을 시도합니다. 정리 오류는 원래 오류에 suppressed로 보존합니다. 자동 중첩/에뮬레이션/fallback은 없으며 드라이버의 구조 변경 rollback과 핸들 재조회 정책은 호출자가 관리합니다. 반환된 nonzero 값 자체는 예외가 아니므로 블록에서 검사해야 합니다. 트랜잭션 도중 외부에서 commit/rollback하거나 Dataset을 닫지 않습니다.

### 창 단위 래스터 계산·재분류·구역 통계

```kotlin
rasterCalculate(listOf(red, nir), output, RasterChunkSize(256, 256)) { values ->
    (values[1] - values[0]) / (values[1] + values[0]) // NDVI, raw input values
}
source.reclassifyTo(classes, listOf(
    RasterClass(Double.NEGATIVE_INFINITY, 0.0, 1.0),
    RasterClass(0.0, 10.0, 2.0),
    RasterClass(10.0, Double.POSITIVE_INFINITY, 3.0),
), outputNoData = -9999.0)
val statistics: Map<Long, RasterZoneStatistics> = values.zonalStatistics(zoneIds)
```

`rasterCalculate`는 같은 크기로 정렬된 여러 Band를 2차원 창 단위로 읽어 결과 Band에
씁니다. CRS·픽셀 그리드 정렬, 출력 파일 생성, 지오트랜스폼 복사는 호출자가 맡습니다.
필요한 경우 기존 Warp/Rasterize로 입력과 zone ID를 같은 그리드에 맞추세요. 자동 재투영,
resampling, scale/offset 적용은 하지 않습니다. 콜백에 넘기는 DoubleArray는 픽셀마다
재사용하므로 보관할 경우 복사해야 합니다. GDAL 마스크·밴드 NoData·비유한 입력은 출력
NoData로 전파하며 비유한 계산 결과도 동일하게 처리합니다. 밴드 NoData 메타데이터는
시작할 때 새로 읽고, 실수 NoData는 GDAL의 Float32-epsilon 비교 규칙을 따릅니다.
명시적·alpha·dataset 마스크는 GDAL의 읽기 결과를 사용합니다. 처리 중 입력과 메타데이터를
다른 작업에서 변경하지 마세요.

기본 출력 NoData는 NaN이므로 보통 Float32/Float64 결과 Band를 사용합니다. 정수 결과에는
표현 가능한 유한 정수 NoData를 명시해야 합니다. 값의 대상 타입 변환은 GDAL 규칙을 따릅니다.
정확한 Int64/UInt64와 complex 계산은 묵시적으로 Double로 바꾸지 않고 거부하므로 먼저
원하는 정책으로 명시적 변환을 하세요. 그 외 Byte/Int8/16/32 및 Float16/32/64를 지원합니다.

재분류는 겹치지 않는 반개구간 `[minimum, maximum)`을 내부에서 정렬하고 이진 탐색합니다.
범위 밖 값은 출력 NoData가 됩니다. 제자리 계산도 가능하며 원래 NoData를 처리 중 유지하고
성공 후 결과 NoData 메타데이터를 설정합니다. 예외·취소 시 이전 창의 부분 기록이 남을 수
있습니다. 행/창 경계에서 취소를 확인하고 임시 마스크 핸들을 정리하며 입력·대상은 닫지 않습니다.

구역 통계는 정수 zone ID(-2^31..2^32-1)별 count/min/max/sum/mean/모집단 표준편차를
돌려줍니다. 두 밴드의 mask/NoData를 제외하며 0도 유효 zone입니다. 표본이 없는 zone은
생략합니다. 평균·분산은 Welford, 합계는 보정 합산을 사용하고 Double 누적 overflow는 예외로
알립니다. 통계는 원시 값이며 polygon 면적 가중 통계가 아닙니다. 래스터 버퍼는 창 크기에
비례하지만 구역 누적 메모리는 서로 다른 zone 개수에 비례합니다.

### 포인트 보간과 가시권 분석

```kotlin
val grid = points.grid("-a", "invdist:power=2", "-zfield", "height",
    "-txe", "0", "1000", "-tye", "0", "1000", "-outsize", "100", "100")
points.gridTo("grid.tif", "-a", "nearest", "-zfield", "height")

val observer = ViewshedOptions(observerX = 500.0, observerY = 500.0,
    observerHeight = 2.0, maxDistance = 1000.0)
val visibility = dem.viewshed(observer)
dem.viewshedTo("visible.tif", observer, "COMPRESS=JXL", "JXL_LOSSLESS=YES")
val minimumHeight = dem.viewshed(observer.copy(output = ViewshedOutput.MinimumHeightFromGround))
```

`grid`는 실제 `GDALGrid` utility로 nearest/invdist/average/linear 등 보간, `-zfield`,
레이어/SQL/where 선택, 출력 범위·해상도·타입·creation options를 처리합니다. 필터와
읽기 cursor 동작은 GDAL 규칙을 따릅니다. `-a_srs`는 좌표 변환 없이 출력 CRS만 지정합니다.
기본 출력 타입은 GDAL의 Float64이며 JXL 압축에는 `-ot Float32` 등 지원 타입을 명시하세요.

`viewshed`는 실제 `GDALViewshedGenerate`이며 north-up, 투영 CRS가 있는 DEM을 요구합니다.
관측점은 DEM 좌표계, 수평 거리와 DEM 고도는 서로 일치하는 단위를 사용합니다. 자동 재투영,
scale/offset 변환, 결측값 보간은 하지 않습니다. GDAL은 입력 NoData를 특별히 제외하지 않아
그 위치가 가시권을 왜곡할 수 있습니다. 필요하면 먼저 DEM을 정리하세요.
`curvatureCoefficient=0.0`은 지구 곡률·굴절 보정을 끄고, 기본값은 0.85714입니다.
일반 가시권은 Byte, 최소 목표 높이 출력은 Float64입니다. `maxDistance=0`은 무제한이며
양수일 때 출력 범위가 잘립니다. `noDataValue` 음수는 출력 NoData 메타데이터를 생략합니다.

두 API는 raster Create 지원 드라이버(예: GTiff)를 사용합니다. COG가 필요하면 결과를
`translate`로 변환하세요. `*To`는 OS/Node 또는 browser `opfs:/`에 직접 쓰고 기존 파일은
거부합니다. 실패 후 부분 출력은 호출자 소유입니다. memory 결과는 `GdalFiles`이며 입력은
호출자 소유로 유지합니다. 옵션은 `List<String>` 또는 `vararg String`/`asList()`로 전달합니다.

### 래스터 정리·거리·등고선

밴드 알고리즘은 이미 연 writable 대상 밴드에 직접 씁니다. 입력과 대상의 크기가 같아야
하며 지오트랜스폼·CRS·NoData 메타데이터는 자동 복사하지 않습니다. 반환값 `0`은
GDAL 성공 코드이며, 실패 시 일부 픽셀이 기록되었을 수 있습니다.

```kotlin
sourceBand.sieveTo(targetBand, threshold = 8, connectedness = 8)
sourceBand.proximityTo(distanceBand, "VALUES=1,2", "DISTUNITS=PIXEL")
editableBand.fillNoData("INTERPOLATION=NEAREST", maxSearchDistance = 20.0)
val contours = dem.contour(10.0) // GPKG GdalFiles
// large output: OS/Node file, or browser opfs:/ path
 dem.contourTo("contours.gpkg", 10.0, "-lco", "SPATIAL_INDEX=YES")
```

- Sieve는 작은 연결 영역을 가장 큰 이웃의 값으로 바꿉니다. 기본 source mask는
  NoData를 제외합니다. `useDefaultMask=false` 또는 명시적 `mask`로 조절합니다.
  GDAL은 실수를 정수로 반올림합니다. UInt64와 complex 입력은 지원하지 않습니다.
  source와 target을 같게 지정하면 제자리 수정합니다.
- Proximity는 기본적으로 0이 아닌 픽셀까지 거리를 계산합니다. source/target은
  다른 밴드여야 합니다. GDAL의 내부 source 분류값은 Int32이며 `VALUES`, `MAXDIST`,
  `NODATA`, `USE_INPUT_NODATA`, `FIXED_BUF_VAL`을 지원합니다. GEO 거리는 GDAL의
  픽셀 크기 규칙을 따르며 측지 거리 계산이나 재투영이 아닙니다.
- FillNoData는 대상 밴드를 제자리 수정합니다. 기본 mask의 0 픽셀을 보간하며 명시적
  mask도 가능합니다. 내부 연산은 GDAL의 Float32 정밀도이며 밴드 타입으로 변환합니다.
  scale/offset과 NoData 메타데이터는 바꾸지 않습니다. 기본 scratch driver는 이식 가능한
  `MEM`입니다. 큰 입력은 `TEMP_FILE_DRIVER=GTiff`와 writable `CPL_TMPDIR`이 있는 환경을
  사용하세요. smoothing은 `NODATA=` 옵션을 고려하지 않는 GDAL 제약이 있습니다.
- Contour는 실제 `GDALContourGenerateEx`로 선 레이어를 생성합니다. 원본 CRS와 회전
  포함 geotransform을 사용하고 `id`/`elevation` 필드를 기록합니다. `FIXED_LEVELS=a,b`,
  `LEVEL_EXP_BASE=n`, `NODATA=n`, `-dsco`/`-lco`를 받습니다. 기존 출력은 덮어쓰지 않습니다.
  생성 도중 실패한 direct 파일과 sidecar는 호출자 소유입니다. `List<String>`와
  `vararg String` 옵션 모두 제공하며 vararg는 `asList()`로 위임합니다.
