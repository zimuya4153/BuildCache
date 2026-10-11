-- Minimal consumer project: the official repositories plus ours, and the
-- *upstream* package name -- exactly the way a plugin project uses BuildCache.
-- Our repository carries manifests only, so nothing upstream is renamed,
-- shadowed or copied: a package we have no manifest for simply builds here.
set_xmakever("3.0.0")

add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/MiracleForest/xmake-repo.git")

-- Packages are resolved under an MSVC toolchain: that is what xmake's
-- precompiled-artifact selection requires. LeviLamina's own sources are still
-- built with clang-cl upstream, this only affects how packages are resolved.
set_toolchains("msvc")
set_runtimes("MD")

add_requires("levilamina 26.51.6", {configs = {target_type = "server"}})

target("consumer-smoke")
    set_kind("shared")
    set_languages("c++20")
    add_defines("_HAS_CXX23=1")
    add_files("src/*.cpp")
    add_packages("levilamina")
