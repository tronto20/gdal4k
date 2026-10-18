#pragma once
#if defined(GDAL4K_STATIC)
# define G4K_API
#elif defined(_WIN32)
# if defined(GDAL4K_BUILD)
#  define G4K_API __declspec(dllexport)
# else
#  define G4K_API __declspec(dllimport)
# endif
#else
# define G4K_API __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct gdal4k_context gdal4k_context;
typedef struct gdal4k_control gdal4k_control;
G4K_API gdal4k_context *gdal4k_create(const char *proj_data, const char *gdal_data);
G4K_API char *gdal4k_execute(gdal4k_context *, const char *request);
G4K_API char *gdal4k_execute_controlled(gdal4k_context *, const char *request, gdal4k_control *);
G4K_API gdal4k_control *gdal4k_control_create();
G4K_API void gdal4k_control_cancel(gdal4k_control *);
G4K_API char *gdal4k_control_snapshot(gdal4k_control *);
G4K_API void gdal4k_control_destroy(gdal4k_control *);
G4K_API void gdal4k_free(char *);
G4K_API void gdal4k_destroy(gdal4k_context *);
#ifdef __cplusplus
}
#endif
