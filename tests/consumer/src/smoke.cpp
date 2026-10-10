// Compiles against the headers shipped inside the prebuilt SDK artifact.
#include <ll/api/Global.h>

extern "C" int buildcache_consumer_smoke() {
    return ll::getLoader() ? 0 : 1;
}
