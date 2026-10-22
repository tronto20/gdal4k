// Included inside namespace g4k after the shared file utilities.
// This wrapper deliberately does not serialize a planning VRT: the pinned VRT
// serializer represents NoData through Float64, which cannot preserve all types.
#pragma once

struct MDTErrorScope {
    bool failed = false;
    std::string message;
    static void CPL_STDCALL collect(CPLErr level, CPLErrorNum, const char* text) {
        auto self = static_cast<MDTErrorScope*>(CPLGetErrorHandlerUserData());
        if (!self || level < CE_Failure) return;
        self->failed = true;
        // Never let a C++ allocation exception cross the GDAL C callback.
        try { if (self->message.empty()) self->message = text ? text : "GDAL failure"; } catch (...) {}
    }
    MDTErrorScope() { CPLErrorReset(); CPLPushErrorHandlerEx(collect, this); }
    ~MDTErrorScope() { CPLPopErrorHandler(); }
    void check() const { require(!failed, "Multidimensional conversion failed: " + message); }
};

using MDTDataset = std::unique_ptr<void, decltype(&GDALClose)>;

inline bool mdtTypeEqual(const GDALExtendedDataType& a, const GDALExtendedDataType& b) {
    if (a.GetClass() != b.GetClass() || a.GetSubType() != b.GetSubType()) return false;
    if (a.GetClass() == GEDTC_NUMERIC) return a.GetNumericDataType() == b.GetNumericDataType();
    if (a.GetClass() == GEDTC_STRING) return a.GetMaxStringLength() == b.GetMaxStringLength();
    if (a.GetName() != b.GetName() || a.GetComponents().size() != b.GetComponents().size()) return false;
    for (size_t i = 0; i < a.GetComponents().size(); ++i) {
        const auto& x = a.GetComponents()[i]; const auto& y = b.GetComponents()[i];
        if (x->GetName() != y->GetName() || !mdtTypeEqual(x->GetType(), y->GetType())) return false;
    }
    return true; // Native compound padding and offsets are not portable schema.
}
inline bool mdtValueEqual(const void* a, const GDALExtendedDataType& at,
                          const void* b, const GDALExtendedDataType& bt) {
    if (!mdtTypeEqual(at, bt)) return false;
    if (at.GetClass() == GEDTC_STRING) {
        const char* x = nullptr; const char* y = nullptr;
        std::memcpy(&x, a, sizeof(x)); std::memcpy(&y, b, sizeof(y));
        return (!x && !y) || (x && y && std::strcmp(x, y) == 0);
    }
    if (at.GetClass() == GEDTC_COMPOUND) {
        for (size_t i = 0; i < at.GetComponents().size(); ++i) {
            const auto& x = at.GetComponents()[i]; const auto& y = bt.GetComponents()[i];
            if (!mdtValueEqual(static_cast<const GByte*>(a) + x->GetOffset(), x->GetType(),
                               static_cast<const GByte*>(b) + y->GetOffset(), y->GetType())) return false;
        }
        return true;
    }
    if (at.GetSize() == bt.GetSize() && std::memcmp(a, b, at.GetSize()) == 0) return true;
    const auto type = at.GetNumericDataType(), base = GDALGetNonComplexDataType(type);
    if (!GDALDataTypeIsFloating(base)) return false;
    const auto parts = GDALDataTypeIsComplex(type) ? 2 : 1;
    const auto size = GDALGetDataTypeSizeBytes(base);
    for (int i = 0; i < parts; ++i) {
        const auto x = static_cast<const GByte*>(a) + i * size;
        const auto y = static_cast<const GByte*>(b) + i * size;
        if (std::memcmp(x, y, size) == 0) continue;
        double xd = 0, yd = 0;
        GDALCopyWords(x, base, 0, &xd, GDT_Float64, 0, 1);
        GDALCopyWords(y, base, 0, &yd, GDT_Float64, 0, 1);
        if (!(std::isnan(xd) && std::isnan(yd))) return false;
    }
    return true; // NaN payload bits are not a cross-driver preservation promise.
}
struct MDTBuffer {
    GDALExtendedDataType type;
    std::vector<GByte> bytes;
    size_t count;
    MDTBuffer(const GDALExtendedDataType& t, size_t n): type(t), count(n) {
        require(type.GetSize() && n <= SIZE_MAX / type.GetSize(), "MD comparison buffer overflow");
        bytes.resize(n * type.GetSize());
    }
    ~MDTBuffer() {
        if (type.NeedsFreeDynamicMemory())
            for (size_t i = 0; i < count; ++i) type.FreeDynamicMemory(bytes.data() + i * type.GetSize());
    }
};
inline void mdtCompareValues(const std::shared_ptr<GDALMDArray>& source,
                             const std::shared_ptr<GDALMDArray>& output) {
    const auto& dims = source->GetDimensions();
    require(dims.size() == output->GetDimensionCount(), "MD output rank changed unexpectedly");
    for (size_t i = 0; i < dims.size(); ++i) {
        require(dims[i]->GetSize() == output->GetDimensions()[i]->GetSize(), "MD output shape changed unexpectedly");
        if (!dims[i]->GetSize()) return;
    }
    const auto& st = source->GetDataType(); const auto& dt = output->GetDataType();
    require(mdtTypeEqual(st, dt), "MD output changed the extended data type");
    const auto limit = std::max<size_t>(1, 65536 / std::max(st.GetSize(), dt.GetSize()));
    std::vector<GUInt64> start(dims.size(), 0);
    std::vector<size_t> count(dims.size(), 1);
    bool done = false;
    do {
        binaryCheckpoint();
        size_t n = 1;
        if (!dims.empty()) {
            n = static_cast<size_t>(std::min<GUInt64>(limit, dims.back()->GetSize() - start.back()));
            count.back() = n;
        }
        MDTBuffer x(st, n), y(dt, n);
        fileEnsure(source->Read(start.data(), count.data(), nullptr, nullptr, st, x.bytes.data(), x.bytes.data(), x.bytes.size()), "Cannot validate MD source values");
        fileEnsure(output->Read(start.data(), count.data(), nullptr, nullptr, dt, y.bytes.data(), y.bytes.data(), y.bytes.size()), "Cannot validate MD output values");
        for (size_t i = 0; i < n; ++i)
            require(mdtValueEqual(x.bytes.data() + i * st.GetSize(), st, y.bytes.data() + i * dt.GetSize(), dt),
                    "MD output changed a value; the selected driver cannot preserve this source");
        if (dims.empty()) done = true;
        else {
            start.back() += n;
            for (size_t i = dims.size(); i-- > 0;) {
                if (start[i] < dims[i]->GetSize()) break;
                start[i] = 0;
                if (i == 0) done = true;
                else ++start[i - 1];
            }
        }
    } while (!done);
}
inline void mdtCompareAttributes(const std::vector<std::shared_ptr<GDALAttribute>>& source,
                                const std::vector<std::shared_ptr<GDALAttribute>>& output,
                                bool exact) {
    if (exact) require(source.size() == output.size(), "MD output attribute inventory changed");
    for (const auto& attr : source) {
        binaryCheckpoint();
        std::shared_ptr<GDALAttribute> found;
        for (const auto& candidate : output) if (candidate->GetName() == attr->GetName()) { found = candidate; break; }
        require(found != nullptr && attr->GetDimensionsSize() == found->GetDimensionsSize() &&
                mdtTypeEqual(attr->GetDataType(), found->GetDataType()), "MD output lost or changed an attribute schema: " + attr->GetName());
        auto x = attr->ReadAsRaw(); auto y = found->ReadAsRaw();
        const auto& xt = attr->GetDataType(); const auto& yt = found->GetDataType();
        const auto n = attr->GetTotalElementsCount();
        require(n <= SIZE_MAX / xt.GetSize() && n <= SIZE_MAX / yt.GetSize() &&
                x.size() == n * xt.GetSize() && y.size() == n * yt.GetSize(), "Cannot validate MD attribute bytes");
        for (size_t i = 0; i < n; ++i)
            require(mdtValueEqual(x.data() + i * xt.GetSize(), xt, y.data() + i * yt.GetSize(), yt),
                    "MD output changed an attribute value: " + attr->GetName());
    }
}
inline void mdtCompareArrayMetadata(const std::shared_ptr<GDALMDArray>& source,
                                   const std::shared_ptr<GDALMDArray>& output, bool exactAttributes) {
    require(mdtTypeEqual(source->GetDataType(), output->GetDataType()), "MD output changed the extended data type");
    const auto a = source->GetRawNoDataValue(), b = output->GetRawNoDataValue();
    require((!a && !b) || (a && b && mdtValueEqual(a, source->GetDataType(), b, output->GetDataType())),
            "MD output lost or changed typed NoData");
    require(source->GetUnit() == output->GetUnit(), "MD output lost or changed the array unit");
    bool ah = false, bh = false;
    double av = source->GetScale(&ah), bv = output->GetScale(&bh);
    require(ah == bh && (!ah || av == bv || (std::isnan(av) && std::isnan(bv))), "MD output lost or changed scale");
    av = source->GetOffset(&ah); bv = output->GetOffset(&bh);
    require(ah == bh && (!ah || av == bv || (std::isnan(av) && std::isnan(bv))), "MD output lost or changed offset");
    auto as = source->GetSpatialRef(), bs = output->GetSpatialRef();
    require((!as && !bs) || (as && bs && as->IsSame(bs.get())), "MD output lost or changed the array CRS");
    mdtCompareAttributes(source->GetAttributes(), output->GetAttributes(), exactAttributes);
}

inline std::vector<std::string> mdtSpecTokens(const std::string& text) {
    std::vector<std::string> result; size_t start = 0; int depth = 0;
    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] == '[') ++depth;
        if (text[i] == ']') --depth;
        require(depth >= 0, "Unbalanced MD selector brackets");
        if (!depth && text[i] == ',') { result.push_back(text.substr(start, i - start)); start = i + 1; }
    }
    require(depth == 0, "Unbalanced MD selector brackets");
    result.push_back(text.substr(start)); return result;
}
struct MDTSelection {
    std::string name, destination, view;
    std::vector<int> transpose;
    int band = 0;
    bool recursive = true;
};
inline MDTSelection mdtSelection(const std::string& text, bool group) {
    MDTSelection out;
    if (text.rfind("name=", 0) != 0 && (group || text.rfind("band=", 0) != 0)) out.name = text;
    else {
        std::set<std::string> keys;
        for (const auto& token : mdtSpecTokens(text)) {
            const auto equal = token.find('='); require(equal != std::string::npos, "Invalid MD selector");
            const auto key = token.substr(0, equal), val = token.substr(equal + 1);
            require(keys.insert(key).second && !val.empty(), "Duplicate or empty MD selector field");
            if (key == "name") out.name = val;
            else if (key == "dstname") { fileBasename(val); out.destination = val; }
            else if (group && key == "recursive") { require(val == "yes" || val == "no", "MD recursive must be yes or no"); out.recursive = val == "yes"; }
            else if (!group && key == "band") {
                const auto parsed = std::from_chars(val.data(), val.data() + val.size(), out.band);
                require(parsed.ec == std::errc{} && parsed.ptr == val.data() + val.size() && out.band > 0, "Invalid MD source band");
            } else if (!group && key == "view") {
                require(val.front() == '[' && val.back() == ']' && val.find_first_not_of("[]0123456789,:.- ") == std::string::npos,
                        "MD view supports numeric slicing and reordering, not field/type conversion"); out.view = val;
            } else if (!group && key == "transpose") {
                require(val.front() == '[' && val.back() == ']', "Invalid MD transpose");
                for (const auto& axis : mdtSpecTokens(val.substr(1, val.size() - 2))) {
                    int n = -1; const auto parsed = std::from_chars(axis.data(), axis.data() + axis.size(), n);
                    require(parsed.ec == std::errc{} && parsed.ptr == axis.data() + axis.size() && n >= 0, "Invalid MD transpose axis"); out.transpose.push_back(n);
                }
            } else throw std::runtime_error("Unsupported or lossy MD selector field: " + key);
        }
    }
    require((!out.name.empty()) != (out.band > 0), "MD selector requires one array/group name or band");
    if (out.destination.empty()) out.destination = out.band ? "Band" + std::to_string(out.band) : out.name.substr(out.name.find_last_of('/') + 1);
    require(group || !out.destination.empty(), "MD selector requires a non-root destination array name");
    return out;
}
struct MDTSubset { std::string name, low, high; bool slice = false; };
struct MDTOptions {
    FileOptions native{CPLJSONArray{}};
    std::vector<MDTSelection> arrays, groups;
    std::map<std::string, MDTSubset> subsets;
    std::map<std::string, unsigned long long> scales;
    std::set<std::string> usedDimensions;
    bool transformed = false;
    MDTOptions(const CPLJSONArray& arguments, int mode, const std::string& driver) {
        for (int i = 0; i < arguments.Size(); ++i) {
            const auto option = arguments[i].ToString();
            if (option == "-strict") { native.add(option); continue; }
            require(option == "-array" || option == "-group" || option == "-subset" || option == "-scaleaxes" || option == "-co",
                    "Unsupported MD conversion option (output overrides, resampling and type conversion are not accepted): " + option);
            require(i + 1 < arguments.Size(), "Missing MD conversion option value");
            const auto text = arguments[++i].ToString();
            require(!text.empty() && text.find('\0') == std::string::npos, "Invalid MD conversion option value");
            if (option == "-array") arrays.push_back(mdtSelection(text, false));
            else if (option == "-group") groups.push_back(mdtSelection(text, true));
            else if (option == "-subset") {
                const auto open = text.find('('); require(open != std::string::npos && open && text.back() == ')', "Invalid MD subset syntax");
                auto tokens = CSLTokenizeString2(text.substr(open + 1, text.size() - open - 2).c_str(), ",", CSLT_HONOURSTRINGS);
                const auto n = CSLCount(tokens); MDTSubset subset; subset.name = text.substr(0, open);
                if (n == 1 || n == 2) { subset.low = tokens[0]; subset.high = tokens[n - 1]; subset.slice = n == 1; }
                CSLDestroy(tokens); require(n == 1 || n == 2, "MD subset requires one slice value or two bounds");
                require(subsets.emplace(subset.name, subset).second, "Duplicate MD subset dimension");
            } else if (option == "-scaleaxes") {
                for (const auto& scale : mdtSpecTokens(text)) {
                    const auto open = scale.find('('); require(open != std::string::npos && open && scale.back() == ')', "Invalid MD scaleaxes syntax");
                    const auto number = scale.substr(open + 1, scale.size() - open - 2); unsigned long long n = 0;
                    const auto parsed = std::from_chars(number.data(), number.data() + number.size(), n);
                    require(parsed.ec == std::errc{} && parsed.ptr == number.data() + number.size() && n && scales.emplace(scale.substr(0, open), n).second,
                            "MD scaleaxes requires unique dimensions and positive integer factors");
                }
            } else {
                require(mode != 0, "MEM multidimensional conversion does not accept file creation options");
                const auto eq = text.find('='); require(eq != std::string::npos, "Invalid MD creation option");
                auto key = text.substr(0, eq), val = text.substr(eq + 1);
                for (auto& c : key) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
                for (auto& c : val) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
                if (driver == "netCDF") require(key == "FORMAT" && val == "NC4", "MD netCDF conversion currently requires lossless FORMAT=NC4");
                else {
                    const bool compression = key == "COMPRESS" && (val == "NONE" || val == "LZW" || val == "DEFLATE" || val == "ZSTD" || val == "PACKBITS");
                    const bool boolean = key == "TILED" && (val == "YES" || val == "NO");
                    const bool big = key == "BIGTIFF" && (val == "YES" || val == "NO" || val == "IF_NEEDED" || val == "IF_SAFER");
                    const bool predictor = key == "PREDICTOR" && (val == "1" || val == "2" || val == "3");
                    require(compression || boolean || big || predictor, "Unsupported or potentially lossy MD GTiff creation option: " + key);
                }
            }
            native.add(option);
            // The pinned structured name= parser otherwise leaves a full source
            // path in dstname, unlike its bare-name syntax. Make our basename
            // mapping explicit rather than allowing a hidden group/path rename.
            if (option == "-array" && text.rfind("name=", 0) == 0 && text.find("dstname=") == std::string::npos)
                native.add(text + ",dstname=" + arrays.back().destination);
            else native.add(text);
        }
        require(arrays.empty() || groups.empty(), "MD array and group selectors are mutually exclusive");
        for (const auto& array : arrays) require(array.view.empty() || (subsets.empty() && scales.empty()), "MD view cannot be combined with subset or scaleaxes");
        transformed = !arrays.empty() || !groups.empty() || !subsets.empty() || !scales.empty();
        native.add("-strict"); native.add("-of"); native.add(driver);
        if (driver == "netCDF") { native.add("-co"); native.add("FORMAT=NC4"); }
    }
};

struct MDTDimChange { GUInt64 start = 0, count = 0, step = 1; bool slice = false; };
inline MDTDimChange mdtDimension(MDTOptions& options, const std::shared_ptr<GDALDimension>& dim) {
    MDTDimChange out; out.count = dim->GetSize();
    auto subset = options.subsets.find(dim->GetName());
    if (subset != options.subsets.end()) {
        options.usedDimensions.insert(dim->GetName()); const auto& spec = subset->second;
        auto coordinate = dim->GetIndexingVariable();
        require(coordinate && coordinate->GetDimensionCount() == 1 && coordinate->GetDimensions()[0]->GetSize() == dim->GetSize(), "MD subset requires a one-dimensional indexing variable");
        const auto& type = coordinate->GetDataType(); const bool numeric = type.GetClass() == GEDTC_NUMERIC;
        const auto dt = numeric ? type.GetNumericDataType() : GDT_Unknown;
        require((numeric && !GDALDataTypeIsComplex(dt) && dt != GDT_Int64 && dt != GDT_UInt64) || type.GetClass() == GEDTC_STRING,
                "MD value-based subset cannot preserve Int64/UInt64 or complex coordinate selection; use an index-based view");
        const auto readType = numeric ? GDALExtendedDataType::Create(GDT_Float64) : GDALExtendedDataType::CreateString();
        double low = 0, high = 0, previousNumber = 0; std::string stringLow = spec.low, stringHigh = spec.high, previousString;
        if (numeric) {
            require(CPLGetValueType(spec.low.c_str()) != CPL_VALUE_STRING && CPLGetValueType(spec.high.c_str()) != CPL_VALUE_STRING, "Numeric MD subset requires numeric bounds");
            low = CPLAtof(spec.low.c_str()); high = CPLAtof(spec.high.c_str()); require(std::isfinite(low) && std::isfinite(high), "MD subset bounds must be finite"); if (low > high) std::swap(low, high);
        } else if (stringLow > stringHigh) std::swap(stringLow, stringHigh);
        const auto epsilon = std::max(1e-10, std::max(std::fabs(low), std::fabs(high)) / 1e10);
        bool first = true, found = false; int direction = 0; GUInt64 firstIndex = 0, lastIndex = 0;
        for (GUInt64 offset = 0; offset < dim->GetSize();) {
            binaryCheckpoint(); const auto count = static_cast<size_t>(std::min<GUInt64>(8192, dim->GetSize() - offset)); MDTBuffer buffer(readType, count);
            fileEnsure(coordinate->Read(&offset, &count, nullptr, nullptr, readType, buffer.bytes.data(), buffer.bytes.data(), buffer.bytes.size()), "Cannot read MD indexing variable");
            for (size_t i = 0; i < count; ++i) {
                bool selected = false; int order = 0;
                if (numeric) {
                    double current = 0; std::memcpy(&current, buffer.bytes.data() + i * sizeof(double), sizeof(double));
                    require(std::isfinite(current), "MD subset indexing values must be finite");
                    if (!first) order = current > previousNumber ? 1 : current < previousNumber ? -1 : 0;
                    previousNumber = current; selected = current >= low - epsilon && current <= high + epsilon;
                } else {
                    const char* current = nullptr; std::memcpy(&current, buffer.bytes.data() + i * sizeof(char*), sizeof(current));
                    require(current != nullptr, "MD subset indexing strings cannot be null");
                    if (!first) order = std::strcmp(current, previousString.c_str()) > 0 ? 1 : std::strcmp(current, previousString.c_str()) < 0 ? -1 : 0;
                    previousString = current; selected = previousString >= stringLow && previousString <= stringHigh;
                }
                if (!first && order) { require(!direction || direction == order, "MD subset indexing values must be monotonic"); direction = order; }
                first = false;
                if (selected) { if (!found) firstIndex = offset + i; found = true; lastIndex = offset + i; }
            }
            offset += count;
        }
        require(found, "MD subset results in an empty set"); out.start = firstIndex; out.count = lastIndex - firstIndex + 1; out.slice = spec.slice;
    }
    auto scale = options.scales.find(dim->GetName());
    if (scale != options.scales.end()) {
        options.usedDimensions.insert(dim->GetName()); out.step = scale->second; out.count = std::max<GUInt64>(1, out.count / out.step);
    }
    return out;
}
struct MDTArrayPlan {
    std::shared_ptr<GDALMDArray> source, view;
    std::string destination;
};
struct MDTGroupPlan { std::shared_ptr<GDALGroup> source; std::string destination; bool recursive; };
struct MDTPlan {
    std::vector<MDTArrayPlan> arrays;
    std::vector<MDTGroupPlan> groups;
    std::shared_ptr<GDALGroup> rootAttributes;
};
inline std::string mdtJoin(const std::string& group, const std::string& name) { return group == "/" ? "/" + name : group + "/" + name; }
inline std::shared_ptr<GDALMDArray> mdtArrayView(MDTOptions& options, std::shared_ptr<GDALMDArray> source, const MDTSelection& selection) {
    auto view = source;
    if (!selection.transpose.empty()) { view = view->Transpose(selection.transpose); fileEnsure(view != nullptr, "Invalid MD transpose"); }
    if (!selection.view.empty()) { view = view->GetView(selection.view); fileEnsure(view != nullptr, "Invalid MD view"); }
    else if (!options.subsets.empty() || !options.scales.empty()) {
        std::string expression = "["; bool first = true;
        for (const auto& dim : view->GetDimensions()) {
            auto change = mdtDimension(options, dim); if (!first) expression += ','; first = false;
            if (change.slice) expression += std::to_string(change.start);
            else {
                require(change.count <= (std::numeric_limits<GUInt64>::max() - change.start) / change.step, "MD view range overflow");
                auto end = std::min<GUInt64>(dim->GetSize(), change.start + change.count * change.step);
                expression += std::to_string(change.start) + ":" + std::to_string(end) + ":" + std::to_string(change.step);
            }
        }
        expression += ']';
        if (!view->GetDimensions().empty()) { view = view->GetView(expression); fileEnsure(view != nullptr, "Cannot construct the MD subset validation view"); }
    }
    return view;
}
inline void mdtPlanGroup(MDTPlan& plan, MDTOptions& options, const std::shared_ptr<GDALGroup>& source,
                         const std::string& destination, bool recursive) {
    binaryCheckpoint(); plan.groups.push_back({source, destination, recursive});
    for (const auto& name : source->GetMDArrayNames()) {
        auto array = source->OpenMDArray(name); fileEnsure(array != nullptr, "Cannot open source MD array");
        plan.arrays.push_back({array, mdtArrayView(options, array, MDTSelection{}), mdtJoin(destination, name)});
    }
    if (recursive) for (const auto& name : source->GetGroupNames()) {
        auto group = source->OpenGroup(name); fileEnsure(group != nullptr, "Cannot open source MD group");
        mdtPlanGroup(plan, options, group, mdtJoin(destination, name), true);
    }
}
inline MDTPlan mdtPlan(MDTOptions& options, GDALDataset* source) {
    MDTPlan plan; auto root = source->GetRootGroup();
    if (!options.arrays.empty()) {
        plan.rootAttributes = root; std::set<std::string> names;
        for (const auto& selection : options.arrays) {
            std::shared_ptr<GDALMDArray> array;
            if (selection.band) {
                require(!root && selection.band <= source->GetRasterCount(), "MD band selector requires an existing classic raster band");
                array = source->GetRasterBand(selection.band)->AsMDArray();
            } else {
                require(root != nullptr, "Classic raster conversion requires an explicit band= selector");
                array = selection.name.front() == '/' ? root->OpenMDArrayFromFullname(selection.name) : root->OpenMDArray(selection.name);
            }
            fileEnsure(array != nullptr, "Cannot find selected MD array");
            require(names.insert(selection.destination).second, "Duplicate MD destination array name");
            plan.arrays.push_back({array, mdtArrayView(options, array, selection), "/" + selection.destination});
        }
    } else if (!options.groups.empty()) {
        require(root != nullptr, "MD group selection requires a multidimensional source"); std::set<std::string> names;
        for (const auto& selection : options.groups) {
            auto group = selection.name == "/" ? root : root->OpenGroupFromFullname(selection.name.front() == '/' ? selection.name : "/" + selection.name);
            fileEnsure(group != nullptr, "Cannot find selected MD group");
            const auto destination = options.groups.size() == 1 ? "/" : "/" + selection.destination;
            require(names.insert(destination).second, "Duplicate MD destination group");
            if (destination == "/") plan.rootAttributes = group;
            mdtPlanGroup(plan, options, group, destination, selection.recursive);
        }
    } else {
        require(root != nullptr, "Multidimensional conversion requires a root group or an explicit classic band selector");
        plan.rootAttributes = root; mdtPlanGroup(plan, options, root, "/", true);
    }
    for (const auto& entry : options.subsets) require(options.usedDimensions.count(entry.first), "MD subset dimension is not selected: " + entry.first);
    for (const auto& entry : options.scales) require(options.usedDimensions.count(entry.first), "MD scaleaxes dimension is not selected: " + entry.first);
    return plan;
}
inline void mdtRestoreConventions(const MDTPlan& plan, const std::shared_ptr<GDALGroup>& root) {
    require(root != nullptr, "MD output does not have a root group");
    if (!plan.rootAttributes) return;
    auto source = plan.rootAttributes->GetAttribute("Conventions"); if (!source) return;
    auto output = root->GetAttribute("Conventions");
    if (output && (!mdtTypeEqual(source->GetDataType(), output->GetDataType()) || source->GetDimensionsSize() != output->GetDimensionsSize())) {
        fileEnsure(root->DeleteAttribute("Conventions"), "Cannot preserve the Conventions attribute type"); output.reset();
    }
    if (!output) output = root->CreateAttribute("Conventions", source->GetDimensionsSize(), source->GetDataType());
    fileEnsure(output != nullptr, "Cannot preserve the Conventions attribute");
    auto raw = source->ReadAsRaw(); fileEnsure(output->Write(raw.data(), raw.size()), "Cannot preserve the Conventions attribute value");
}
inline void mdtVerifyDimensionInventory(const MDTPlan& plan, const std::shared_ptr<GDALGroup>& output) {
    // A driver may expose dimensions only on arrays (for example ordinary HDF5
    // datasets), while MEM registers every dimension in its containing group.
    // Model the pinned GDALGroup::CopyFrom allocation order, rather than allowing
    // arbitrary extra dimensions or requiring those two inventories to match.
    using Dimensions = std::map<std::string, std::shared_ptr<GDALDimension>>;
    Dimensions fullNames;
    std::map<std::string, Dimensions> groupDimensions;
    std::map<std::string, const MDTArrayPlan*> arrays;
    std::map<std::string, std::vector<std::string>> bindings;
    for (const auto& array : plan.arrays) arrays.emplace(array.destination, &array);
    for (const auto& group : plan.groups) {
        auto& local = groupDimensions[group.destination];
        std::set<std::string> indexingArrays;
        for (const auto& dim : group.source->GetDimensions()) {
            require(local.emplace(dim->GetName(), dim).second, "Duplicate source MD dimension name");
            require(fullNames.emplace(mdtJoin(group.destination, dim->GetName()), dim).second,
                    "Duplicate destination MD dimension name");
            if (auto indexing = dim->GetIndexingVariable()) indexingArrays.insert(indexing->GetName());
        }
        const auto names = group.source->GetMDArrayNames();
        // GDAL copies indexing arrays first, then regular arrays, preserving the
        // driver's order in each pass. That order determines collision names.
        for (int pass = 0; pass < 2; ++pass) for (const auto& name : names) {
            if ((indexingArrays.count(name) != 0) != (pass == 0)) continue;
            const auto destination = mdtJoin(group.destination, name);
            const auto array = arrays.find(destination);
            require(array != arrays.end(), "MD dimension plan lost a source array");
            auto& axisBindings = bindings[destination];
            for (const auto& dim : array->second->source->GetDimensions()) {
                binaryCheckpoint();
                auto absolute = fullNames.find(dim->GetFullName());
                if (!dim->GetFullName().empty() && dim->GetFullName().front() == '/' &&
                    absolute != fullNames.end() && absolute->second->GetSize() == dim->GetSize()) {
                    axisBindings.push_back(absolute->first);
                    continue;
                }
                const auto sameName = local.find(dim->GetName());
                if (sameName != local.end() && sameName->second->GetSize() == dim->GetSize()) {
                    axisBindings.push_back(mdtJoin(group.destination, sameName->first));
                    continue;
                }
                std::string newName = dim->GetName();
                if (sameName != local.end()) {
                    const auto prefix = name + "_" + dim->GetName();
                    newName = prefix;
                    unsigned long long suffix = 2;
                    while (local.count(newName)) newName = prefix + "_" + std::to_string(suffix++);
                }
                const auto fullName = mdtJoin(group.destination, newName);
                require(local.emplace(newName, dim).second && fullNames.emplace(fullName, dim).second,
                        "Duplicate materialized MD dimension name");
                axisBindings.push_back(fullName);
            }
        }
    }
    for (const auto& group : plan.groups) {
        auto dest = group.destination == "/" ? output : output->OpenGroupFromFullname(group.destination);
        require(dest != nullptr, "MD output lost a selected group");
        const auto actual = dest->GetDimensions();
        const auto& expected = groupDimensions.at(group.destination);
        require(actual.size() == expected.size(), "MD output changed the materialized dimension inventory");
        std::set<std::string> seen;
        for (const auto& dim : actual) {
            const auto found = expected.find(dim->GetName());
            require(found != expected.end() && seen.insert(dim->GetName()).second &&
                    dim->GetFullName() == mdtJoin(group.destination, dim->GetName()) &&
                    dim->GetSize() == found->second->GetSize() && dim->GetType() == found->second->GetType() &&
                    dim->GetDirection() == found->second->GetDirection(), "MD output changed dimension metadata");
            auto sourceIndex = found->second->GetIndexingVariable(), outputIndex = dim->GetIndexingVariable();
            if (sourceIndex) {
                require(outputIndex != nullptr, "MD output lost a dimension indexing variable");
                mdtCompareArrayMetadata(sourceIndex, outputIndex, true);
                mdtCompareValues(sourceIndex, outputIndex);
            }
        }
    }
    for (const auto& array : plan.arrays) {
        auto dest = output->OpenMDArrayFromFullname(array.destination);
        require(dest != nullptr, "MD output lost a selected array: " + array.destination);
        const auto actual = dest->GetDimensions();
        const auto& expected = bindings.at(array.destination);
        require(actual.size() == expected.size(), "MD output changed the dimension binding rank");
        for (size_t i = 0; i < actual.size(); ++i)
            require(actual[i]->GetFullName() == expected[i], "MD output changed an array dimension binding");
    }
}
inline void mdtVerifyPlan(const MDTPlan& plan, const std::shared_ptr<GDALGroup>& output,
                          bool exact, bool unfiltered) {
    require(output != nullptr, "MD output does not have a root group");
    if (exact && unfiltered) mdtVerifyDimensionInventory(plan, output);
    if (plan.rootAttributes) mdtCompareAttributes(plan.rootAttributes->GetAttributes(), output->GetAttributes(), exact && unfiltered);
    for (const auto& group : plan.groups) {
        auto dest = group.destination == "/" ? output : output->OpenGroupFromFullname(group.destination);
        require(dest != nullptr, "MD output lost a selected group");
        mdtCompareAttributes(group.source->GetAttributes(), dest->GetAttributes(), exact && unfiltered);
        if (exact && unfiltered) {
            const auto sg = group.source->GetGroupNames(), dg = dest->GetGroupNames();
            const auto sa = group.source->GetMDArrayNames(), da = dest->GetMDArrayNames();
            require(std::set<std::string>(sg.begin(), sg.end()) == std::set<std::string>(dg.begin(), dg.end()) &&
                    std::set<std::string>(sa.begin(), sa.end()) == std::set<std::string>(da.begin(), da.end()), "MD output changed the group/array inventory");
        }
    }
    for (const auto& array : plan.arrays) {
        binaryCheckpoint(); auto dest = output->OpenMDArrayFromFullname(array.destination);
        require(dest != nullptr, "MD output lost a selected array: " + array.destination);
        mdtCompareArrayMetadata(array.source, dest, exact && unfiltered);
        mdtCompareValues(array.view, dest);
        const auto x = array.view->GetDimensions(), y = dest->GetDimensions();
        for (size_t i = 0; i < x.size(); ++i) {
            require(x[i]->GetType() == y[i]->GetType() && x[i]->GetDirection() == y[i]->GetDirection(), "MD output changed dimension type or direction");
            auto xi = x[i]->GetIndexingVariable(), yi = y[i]->GetIndexingVariable();
            if (xi) { require(yi != nullptr, "MD output lost an indexing variable"); mdtCompareArrayMetadata(xi, yi, exact && unfiltered); mdtCompareValues(xi, yi); }
        }
    }
}
inline void mdtLocalPath(const std::string& path, const std::string& role) {
    require(!path.empty() && path.find('\0') == std::string::npos && path.find_first_of("*?\n\r") == std::string::npos, "Invalid MD " + role + " path");
#ifdef __EMSCRIPTEN__
    // The existing Worker output provider rewrites Node/OPFS destinations to
    // this registered VSI prefix. Its suffix can include a Windows drive colon.
    // This is an output provider only, not implicit input mounting/traversal.
    if (role == "output" && path.rfind("/vsigdal4k-output/", 0) == 0) {
        require(path.size() > std::strlen("/vsigdal4k-output/"), "MD output provider requires a file path");
        return;
    }
#endif
    const auto colon = path.find(':');
    require(colon == std::string::npos || (colon == 1 && std::isalpha(static_cast<unsigned char>(path[0])) && path.size() > 2 && (path[2] == '/' || path[2] == '\\')), "MD conversion does not accept connection strings or URLs");
    require(path.rfind("/vsi", 0) != 0 || path.rfind("/vsimem/", 0) == 0, "MD conversion requires local OS or /vsimem files");
#ifdef __EMSCRIPTEN__
    require(path.rfind("/vsimem/", 0) == 0, "Web MD conversion requires same-runtime /vsimem files");
#endif
}
inline void mdtNewPath(const std::string& path, bool gtiff) {
    mdtLocalPath(path, "output"); VSIStatBufL st;
    require(VSIStatL(path.c_str(), &st) != 0, "MD conversion output already exists");
    for (const auto* suffix : {".aux.xml", ".ovr", ".msk"}) require(VSIStatL((path + suffix).c_str(), &st) != 0, "MD output sidecar already exists");
    if (gtiff) for (const auto* extension : {"tfw", "tifw", "wld", "rpb", "RPB", "imd", "IMD"})
        require(VSIStatL(CPLResetExtensionSafe(path.c_str(), extension).c_str(), &st) != 0, "MD output sidecar already exists");
}
inline void mdtNoBoundedStrings(const std::shared_ptr<GDALMDArray>& array) {
    const auto check = [](const auto& self, const GDALExtendedDataType& type) -> void {
        require(type.GetClass() != GEDTC_STRING || type.GetMaxStringLength() == 0, "netCDF MD output cannot preserve a bounded string schema; use unbounded strings");
        if (type.GetClass() == GEDTC_COMPOUND) for (const auto& component : type.GetComponents()) self(self, component->GetType());
    };
    check(check, array->GetDataType()); for (const auto& attr : array->GetAttributes()) check(check, attr->GetDataType());
}
inline const MDTArrayPlan& mdtClassicPlan(const MDTPlan& plan) {
    const MDTArrayPlan* chosen = nullptr;
    for (const auto& group : plan.groups) require(group.destination == "/" && group.source->GetGroupNames().empty() && group.source->GetAttributes().empty(), "GTiff MD output cannot preserve group hierarchy or group attributes");
    if (plan.rootAttributes) require(plan.rootAttributes->GetAttributes().empty(), "GTiff MD output cannot preserve root-group attributes");
    for (const auto& candidate : plan.arrays) {
        const auto& dims = candidate.view->GetDimensions();
        const bool indexing = dims.size() == 1 && dims[0]->GetIndexingVariable() && dims[0]->GetIndexingVariable()->GetFullName() == candidate.source->GetFullName();
        if (indexing) continue;
        require(chosen == nullptr, "GTiff MD output requires one selected array"); chosen = &candidate;
    }
    require(chosen && chosen->view->GetDimensionCount() == 2 && chosen->source->GetDataType().GetClass() == GEDTC_NUMERIC,
            "GTiff MD output requires one two-dimensional numeric array; slice other dimensions explicitly");
    require(chosen->source->GetAttributes().empty(), "GTiff MD output cannot preserve array attributes");
    for (const auto& dim : chosen->view->GetDimensions()) require(!dim->GetIndexingVariable(), "GTiff MD output does not preserve explicit indexing variables; use MEM or netCDF");
    return *chosen;
}
inline CPLJSONObject multiDimTranslateDataset(Runtime& runtime, GDALDatasetH handle, const CPLJSONObject& args, int mode) {
    require(mode >= 0 && mode <= 2, "Invalid MD conversion mode");
    runtime.requireNoColumnStream(nullptr, handle); binaryCheckpoint();
    auto source = GDALDataset::FromHandle(handle); require(source != nullptr, "Missing MD source dataset");
    const auto driver = mode == 0 ? std::string("MEM") : args.GetString("driver");
    require(driver == "MEM" || driver == "GTiff" || driver == "netCDF", "MD conversion currently supports MEM handles, GTiff files and optional netCDF OS files; reference/directory outputs are rejected");
    require(mode == 0 || driver != "MEM", "MEM MD output must be returned as a Dataset, not files");
    require(mode != 1 || driver == "GTiff", "In-memory MD files currently require GTiff; netCDF creation requires an OS path");
    auto nativeDriver = GDALGetDriverByName(driver.c_str()); require(nativeDriver != nullptr, "MD output driver is not available in this engine profile");
    MDTOptions options(args.GetArray("arguments"), mode, driver);
    MDTErrorScope errors;
    fileEnsure(GDALFlushCache(handle) == CE_None, "Cannot flush MD source before conversion");
    if (options.transformed) {
        require(!EQUAL(source->GetDriverName(), "MEM"), "MD selectors require a reopenable file-backed source; MEM sources support whole-tree copying only");
        const auto description = std::string(source->GetDescription()); mdtLocalPath(description, "source"); VSIStatBufL st;
        require(VSIStatL(description.c_str(), &st) == 0 && !VSI_ISDIR(st.st_mode), "MD selectors require an existing source file");
        MDTDataset reopened(GDALOpenEx(description.c_str(), GDAL_OF_MULTIDIM_RASTER | GDAL_OF_RASTER | GDAL_OF_VERBOSE_ERROR, nullptr, nullptr, nullptr), GDALClose);
        fileEnsure(reopened != nullptr, "Cannot reopen MD selector source"); errors.check();
    }
    auto plan = mdtPlan(options, source);
    const MDTArrayPlan* classic = driver == "GTiff" ? &mdtClassicPlan(plan) : nullptr;
    if (driver == "netCDF") for (const auto& array : plan.arrays) mdtNoBoundedStrings(array.source);
    Directory files;
    std::string name, path;
    if (mode == 1) { name = args.GetString("name"); fileBasename(name); path = files.path + "/" + name; }
    else if (mode == 2) path = args.GetString("path");
    if (mode != 0) {
        mdtNewPath(path, driver == "GTiff");
        require(driver != "netCDF" || path.rfind("/vsi", 0) != 0, "netCDF multidimensional creation requires an OS path, not VSI");
    }
    auto opts = std::unique_ptr<GDALMultiDimTranslateOptions, decltype(&GDALMultiDimTranslateOptionsFree)>(GDALMultiDimTranslateOptionsNew(options.native.list, nullptr), GDALMultiDimTranslateOptionsFree);
    fileEnsure(opts != nullptr, "Invalid MD conversion options"); errors.check();
    GDALMultiDimTranslateOptionsSetProgress(opts.get(), multiDimTranslateProgress, activeProgress);
    OutputTemporaryDirectory temporary(path, mode != 0); int usage = 0;
    // Never supply a destination handle: pinned GDAL closes unsupported hDstDS.
    MDTDataset output(GDALMultiDimTranslate(path.c_str(), nullptr, 1, &handle, opts.get(), &usage), GDALClose);
    fileEnsure(output != nullptr && !usage, "GDAL multidimensional conversion failed"); errors.check();
    if (!classic) mdtRestoreConventions(plan, GDALDataset::FromHandle(output.get())->GetRootGroup());
    fileEnsure(GDALFlushCache(output.get()) == CE_None, "Cannot flush MD output"); errors.check();
    if (mode != 0) {
        const auto closing = output.release(); fileEnsure(GDALClose(closing) == CE_None, "Cannot close MD output"); errors.check();
        output.reset(GDALOpenEx(path.c_str(), (classic ? GDAL_OF_RASTER : GDAL_OF_MULTIDIM_RASTER) | GDAL_OF_VERBOSE_ERROR, nullptr, nullptr, nullptr));
        fileEnsure(output != nullptr, "Cannot reopen completed MD output"); errors.check();
    }
    if (classic) {
        auto dataset = GDALDataset::FromHandle(output.get()); require(dataset->GetRasterCount() == 1, "GTiff MD output must contain exactly one band");
        size_t x = 1, y = 0; bool foundX = false, foundY = false;
        for (size_t i = 0; i < 2; ++i) {
            if (classic->view->GetDimensions()[i]->GetType() == GDAL_DIM_TYPE_HORIZONTAL_X) { x = i; foundX = true; }
            if (classic->view->GetDimensions()[i]->GetType() == GDAL_DIM_TYPE_HORIZONTAL_Y) { y = i; foundY = true; }
        }
        if (!foundX || !foundY) { x = 1; y = 0; }
        MDTDataset expected(classic->view->AsClassicDataset(x, y), GDALClose); fileEnsure(expected != nullptr, "Cannot validate classic MD axis representation");
        auto expectedArray = GDALDataset::FromHandle(expected.get())->GetRasterBand(1)->AsMDArray();
        auto actualArray = dataset->GetRasterBand(1)->AsMDArray();
        mdtCompareArrayMetadata(classic->source, actualArray, false);
        mdtCompareValues(expectedArray, actualArray);
    } else mdtVerifyPlan(plan, GDALDataset::FromHandle(output.get())->GetRootGroup(), mode == 0, !options.transformed);
    errors.check(); binaryCheckpoint();
    if (mode == 0) { auto result = runtime.result(output.get(), "Dataset", true); output.release(); return result; }
    const auto closing = output.release(); fileEnsure(GDALClose(closing) == CE_None, "Cannot close verified MD output"); errors.check();
    return mode == 1 ? outputFiles(files, name) : nullValue();
}
