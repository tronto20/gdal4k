#include "gdal4k.h"
#include <jni.h>
#if defined(_WIN32)
#undef JNIEXPORT
#define JNIEXPORT __declspec(dllexport)
#endif
#include <string>
static std::string bytes(JNIEnv *env, jbyteArray input) {
    const auto n = env->GetArrayLength(input);
    std::string value(n, '\0');
    env->GetByteArrayRegion(input, 0, n, reinterpret_cast<jbyte *>(value.data()));
    return value;
}
#include "jni-inputs.hpp"
extern "C" JNIEXPORT jlong JNICALL Java_dev_gdal4k_JniBridge_create(JNIEnv *env, jobject, jbyteArray proj, jbyteArray data) {
    return reinterpret_cast<jlong>(gdal4k_create(bytes(env, proj).c_str(), bytes(env, data).c_str()));
}
static jbyteArray executeJni(JNIEnv *env, jlong handle, jbyteArray request, gdal4k_control* control) {
    const auto input = bytes(env, request);
    auto* context = reinterpret_cast<gdal4k_context *>(handle);
    char *result = gdal4k_execute_controlled(context, input.c_str(), control);
    if (auto failure = g4k_jni_inputs::takeFailure(handle)) {
        // A driver may ignore an optional sidecar error and still return a live dataset.
        // Close that new dataset before the caller releases its directory descriptors.
        CPLJSONDocument requestDoc, responseDoc;
        if (result && requestDoc.LoadMemory(input) && responseDoc.LoadMemory(result)) {
            auto args = requestDoc.GetRoot().GetObj("args");
            auto id = responseDoc.GetRoot().GetObj("result").GetString("value");
            if (args.GetString("method") == "openDataset_String_DatasetOpenOptions" && !id.empty()) {
                CPLJSONObject closeArgs, closeRequest;
                closeArgs.Add("type", "Handle"); closeArgs.Add("method", "close"); closeArgs.Add("handle", id);
                closeArgs.Add("parameters", CPLJSONObject());
                closeRequest.Add("op", "runtime"); closeRequest.Add("args", closeArgs);
                gdal4k_free(gdal4k_execute(context, closeRequest.Format(CPLJSONObject::PrettyFormat::Plain).c_str()));
            }
        }
        gdal4k_free(result); env->Throw(failure); env->DeleteGlobalRef(failure); return nullptr;
    }
    if (!result) { env->ThrowNew(env->FindClass("java/lang/OutOfMemoryError"), "GDAL bridge allocation failed"); return nullptr; }
    const std::string value(result);
    gdal4k_free(result);
    auto output = env->NewByteArray(static_cast<jsize>(value.size()));
    if (output) env->SetByteArrayRegion(output, 0, static_cast<jsize>(value.size()), reinterpret_cast<const jbyte *>(value.data()));
    return output;
}
extern "C" JNIEXPORT jbyteArray JNICALL Java_dev_gdal4k_JniBridge_execute(JNIEnv *env, jobject, jlong handle, jbyteArray request) { return executeJni(env, handle, request, nullptr); }
extern "C" JNIEXPORT jbyteArray JNICALL Java_dev_gdal4k_JniBridge_executeControlled(JNIEnv *env, jobject, jlong handle, jbyteArray request, jlong control) { return executeJni(env, handle, request, reinterpret_cast<gdal4k_control*>(control)); }
extern "C" JNIEXPORT jlong JNICALL Java_dev_gdal4k_JniBridge_controlCreate(JNIEnv*, jobject) { return reinterpret_cast<jlong>(gdal4k_control_create()); }
extern "C" JNIEXPORT void JNICALL Java_dev_gdal4k_JniBridge_controlCancel(JNIEnv*, jobject, jlong control) { gdal4k_control_cancel(reinterpret_cast<gdal4k_control*>(control)); }
extern "C" JNIEXPORT jbyteArray JNICALL Java_dev_gdal4k_JniBridge_controlSnapshot(JNIEnv* env, jobject, jlong control) {
    auto text = gdal4k_control_snapshot(reinterpret_cast<gdal4k_control*>(control));
    if (!text) { env->ThrowNew(env->FindClass("java/lang/OutOfMemoryError"), "GDAL progress allocation failed"); return nullptr; }
    const auto length = static_cast<jsize>(std::strlen(text));
    auto result = env->NewByteArray(length);
    if (result) env->SetByteArrayRegion(result, 0, length, reinterpret_cast<const jbyte*>(text));
    gdal4k_free(text); return result;
}
extern "C" JNIEXPORT void JNICALL Java_dev_gdal4k_JniBridge_controlDestroy(JNIEnv*, jobject, jlong control) { gdal4k_control_destroy(reinterpret_cast<gdal4k_control*>(control)); }
extern "C" JNIEXPORT void JNICALL Java_dev_gdal4k_JniBridge_destroy(JNIEnv *, jobject, jlong handle) { gdal4k_destroy(reinterpret_cast<gdal4k_context *>(handle)); }

extern "C" JNIEXPORT jint JNICALL Java_dev_gdal4k_JniBridge_binaryVersion(JNIEnv*,jobject) { return gdal4k_binary_version(); }
extern "C" JNIEXPORT jobjectArray JNICALL Java_dev_gdal4k_JniBridge_executeBinary(JNIEnv* env,jobject,jlong handle,jbyteArray request,jbyteArray input,jlong control) {
    try {
    const auto text=bytes(env,request); if(env->ExceptionCheck())return nullptr;
    std::string data; if(input)data=bytes(env,input); if(env->ExceptionCheck())return nullptr;
    auto result=gdal4k_execute_binary(reinterpret_cast<gdal4k_context*>(handle),text.c_str(),reinterpret_cast<const unsigned char*>(data.data()),static_cast<int>(data.size()),reinterpret_cast<gdal4k_control*>(control));
    struct Guard {gdal4k_binary_result* result;~Guard(){gdal4k_binary_destroy(result);}} guard{result};
    if(auto failure=g4k_jni_inputs::takeFailure(handle)){env->Throw(failure);env->DeleteGlobalRef(failure);return nullptr;}
    if(!result){env->ThrowNew(env->FindClass("java/lang/IllegalStateException"),"Binary GDAL bridge failed");return nullptr;}
    const auto json=gdal4k_binary_json(result); const auto size=gdal4k_binary_size(result);
    auto metadata=env->NewByteArray(static_cast<jsize>(std::strlen(json)));if(!metadata)return nullptr;
    env->SetByteArrayRegion(metadata,0,static_cast<jsize>(std::strlen(json)),reinterpret_cast<const jbyte*>(json));
    auto output=env->NewByteArray(size);if(!output)return nullptr;
    if(size)env->SetByteArrayRegion(output,0,size,reinterpret_cast<const jbyte*>(gdal4k_binary_data(result)));
    auto cls=env->FindClass("[B");if(!cls)return nullptr;
    auto response=env->NewObjectArray(2,cls,nullptr);if(!response)return nullptr;
    env->SetObjectArrayElement(response,0,metadata);env->SetObjectArrayElement(response,1,output);
    return response;
    } catch (const std::bad_alloc&) {
        if(!env->ExceptionCheck())if(auto cls=env->FindClass("java/lang/OutOfMemoryError"))env->ThrowNew(cls,"Unable to allocate binary raster staging memory");
        return nullptr;
    } catch (const std::exception& error) {
        if(!env->ExceptionCheck())if(auto cls=env->FindClass("java/lang/IllegalStateException"))env->ThrowNew(cls,error.what());
        return nullptr;
    } catch (...) {
        if(!env->ExceptionCheck())if(auto cls=env->FindClass("java/lang/IllegalStateException"))env->ThrowNew(cls,"Unknown binary raster transport failure");
        return nullptr;
    }
}
