-- Minimal consumer project: it only knows the two repositories and the
-- prefixed package, exactly the way a plugin project would use BuildCache.
set_xmakever("3.0.0")

add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/zimuya4153/xmake-repo.git")

-- Packages are resolved under an MSVC toolchain: that is what xmake's
-- precompiled-artifact selection requires. LeviLamina's own sources are still
-- built with clang-cl upstream, this only affects how packages are resolved.
set_toolchains("msvc")
set_runtimes("MD")

add_requires("mifcahce_levilamina 26.51.6", {configs = {target_type = "server"}})

target("consumer-smoke")
    set_kind("shared")
    set_languages("c++20")
    add_defines("_HAS_CXX23=1")
    add_files("src/*.cpp")
    add_packages("mifcahce_levilamina")
