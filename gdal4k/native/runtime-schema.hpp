// Included after Runtime; attribute schema operations use the real OGR driver.
inline CPLJSONObject changeLayerFields(Runtime& runtime, OGRLayerH layer, const CPLJSONObject& args, const std::string& method) {
    auto schema = OGR_L_GetLayerDefn(layer);
    require(schema != nullptr, "Layer has no field definition");
    const int count = OGR_FD_GetFieldCount(schema);
    auto validIndex = [count](int index) { require(index >= 0 && index < count, "Field index is outside the layer schema"); };
    std::unique_ptr<OGRFieldDefn> replacement;
    std::vector<int> order;
    int index = args.GetInteger("index"), flags = args.GetInteger("flags");
    if (method == "deleteField" || method == "alterFieldDefn") validIndex(index);
    if (method == "alterFieldDefn")
        require(flags > 0 && (flags & ~ALTER_ALL_FLAG) == 0, "Use explicit supported attribute FieldAlterFlags");
    if (method == "createField" || method == "alterFieldDefn") {
        auto input = runtime.arg(args[method == "createField" ? "field" : "definition"]);
        // Resolve and copy before invalidating aliases, including a borrowed input from this layer.
        replacement = std::make_unique<OGRFieldDefn>(OGRFieldDefn::FromHandle(input));
    }
    if (method == "reorderFields") {
        auto mapping = args.GetArray("newToOld");
        require(mapping.Size() == count, "Field order must contain every current field exactly once");
        std::set<int> seen;
        for (auto item : mapping) { int position = item.ToInteger(); validIndex(position);
            require(seen.insert(position).second, "Field order contains a duplicate index"); order.push_back(position); }
    }
    if (method == "reorderField") { validIndex(args.GetInteger("oldIndex")); validIndex(args.GetInteger("newIndex")); }
    auto attributeFilter = OGRLayer::FromHandle(layer)->GetAttrQueryString();
    require(!attributeFilter || !*attributeFilter,
            "Clear the attribute filter before changing fields");
    for (const auto& item : runtime.handles) {
        const auto& entry = item.second;
        require(entry.type != "Feature" || OGR_F_GetDefnRef(entry.pointer) != schema,
                "Close all Features (including clones) using this layer schema before changing fields");
        if (entry.type == "Layer" && entry.owned) {
            auto parent = runtime.handles.find(entry.parent);
            require(parent == runtime.handles.end() || parent->second.pointer != OGR_L_GetDataset(layer),
                    "Close active SQL result layers on this dataset before changing fields");
        }
    }
    // The driver may replace definitions even on an unsuccessful attempt. Never leave
    // borrowed aliases to freed native fields. Independent copies remain usable.
    runtime.invalidatePointer(schema);
    OGRErr code;
    if (method == "createField") code = OGR_L_CreateField(layer, OGRFieldDefn::ToHandle(replacement.get()), args.GetBool("approximate"));
    else if (method == "deleteField") code = OGR_L_DeleteField(layer, index);
    else if (method == "reorderFields") code = OGR_L_ReorderFields(layer, order.data());
    else if (method == "reorderField") code = OGR_L_ReorderField(layer, args.GetInteger("oldIndex"), args.GetInteger("newIndex"));
    else code = OGR_L_AlterFieldDefn(layer, index, OGRFieldDefn::ToHandle(replacement.get()), flags);
    OGR_L_ResetReading(layer);
    return value(code);
}
