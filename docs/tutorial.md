# BuildCache 使用与维护教程

## 1. 它解决什么问题

xmake 默认会在每台机器上从源码编译依赖。BuildCache 把这件事提前做完：CI（或本机）
一次性编译好某个 SDK、某个版本、某个配置下的产物，传到 GitHub Releases，消费端只
下载不编译。

## 2. 原理（先看这一节，后面所有设计都由它推导）

xmake 自带一条**预编译产物通道**：解析依赖时，它会在所有仓库里查找

```
packages/<首字母>/<包名>/<版本>/manifest.txt
```

命中的条目用 `buildid` 匹配，然后下载、校验 sha256、解压安装，**完全不执行配方的
`on_install`**。几条硬规则（都来自 `core/package/package.lua` 与 `require/impl`）：

| 规则 | 后果 |
|---|---|
| Windows 下 key 必须形如 `<plat>-<arch>-vc<...>-<buildhash>` | 中间那段可以省略，前后缀不能动 |
| `buildhash = strhash128(plat + arch + [label] + configs + sourcehash + _toolchains)` | **文件名里不需要再写平台/架构/工具链/版本** |
| 只对「解析时带 msvc 工具链」的包做选择 | 消费端工程要以 msvc 解析依赖 |
| 解包时会**吃掉一层目录** | 包内必须是 `<字母>/<包名>/<版本>/<buildhash>/…`，且 xmake 3.1 不认 `.tar.zst`（用 7z） |
| 校验失败会**静默回退编源码** | 必须自己校验发布一致性（见 `finalize` 的 verify） |

## 3. 仓库与分支布局

| 位置 | 内容 |
|---|---|
| `main` | 框架：`scripts/`、`plugins/`、`recipes/fixups/`、`config.json`、工作流、文档 |
| `index` 分支 | 生成物：`<包名>/index.json`、`<包名>/versions/<版本>.json` |
| `assets` 分支 | 空分支，只作为 release tag 的落点，避免污染 main 的 tag |
| GitHub Releases | 产物本体，按 `<包名>_<序号>` 分片（每片 ≤800 asset） |
| `zimuya4153/xmake-repo` | 消费端仓库：前缀化配方 + `manifest.txt` |

所有发布出去的包都是 `mifcache_<原名>`，**不覆写上游包名**；官方仓库继续提供其它依赖。

## 4. 快速开始（本机）

```bash
# 依赖：Python 3.14+、已登录的 gh CLI；无需第三方 Python 库
python scripts/vendor.py                     # 生成前缀化配方
python scripts/cache.py trigger --plugin ll --first-only --local   # 编一个版本并发布
python scripts/cache.py verify               # 校验「索引 ↔ 线上 asset」一致
cd tests/consumer && xmake f -y -p windows -a x64 -m release      # 消费端验证
```

看到 `install mifcache_levilamina 26.51.6 (precompiled) .. ok` 才算真正走通了预编译；
没有 `(precompiled)` 就是在编源码，去 `cache.py verify` 找原因。

代理只在显式给出时启用：`BUILDCACHE_PROXY=http://127.0.0.1:2080`。

## 5. 消费端怎么用

```lua
add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/zimuya4153/xmake-repo.git")

set_toolchains("msvc")     -- 关键：包按 msvc 解析才会用预编译产物
set_runtimes("MD")
add_requires("mifcache_levilamina 26.51.6", {configs = {target_type = "server"}})

target("my-plugin")
    set_kind("shared")
    add_packages("mifcache_levilamina")
```

插件自身代码仍可用 clang-cl（`target` 级别再 `set_toolchains("clang-cl")`），
LL 的源码编译不受影响。

## 6. 写一个插件（接入新项目）

新建 `plugins/<项目>/plugin.py`，框架只依赖下面这些**约定函数**，都可选：

| 函数 | 作用 |
|---|---|
| `metadata()` | 包名、上游仓库、可能用到的额外包 |
| `list_versions(ctx)` | 返回 `[{version, commit}]`，一般直接读配方的 `add_versions`/`versions.txt` |
| `entries(ctx, version, commit)` | 该版本要编几个条目：`[{"id": "server", "configs": {...}}]`；**这是项目特例的归属地**（LL 的 server/client 就写在这里） |
| `configs(ctx, version, commit)` | 单条目项目的默认 configs |
| `xmake_version` / `toolchain` / `native_toolchain` | 用哪个 xmake、用哪个工具链编缓存 |
| `recipe_cutoff` / `git_cutoff` | 配方快照与「未锁版本的 git clone 回退到的日期」 |
| `extra_deps(ctx, version, commit)` | 只在上游自身 `xmake.lua` 里出现的依赖 |
| `asset_name(...)` | 自定义 asset 文件名（默认 `<原库名>-<buildhash>.<ext>`） |
| `should_skip` / `report_skipped` | 过滤与报表 |

也可以在 `config.json` 里用通用插件（`plugins/upstream.py`）零代码接入：

```json
{
  "name": "moredimensions",
  "module": "plugins/upstream.py",
  "settings": {
    "upstream": "https://github.com/LiteLDev/MoreDimensions.git",
    "upstream_repo": "LiteLDev/MoreDimensions",
    "recipe_source": "levimc-repo",
    "recipe_package": "more-dimensions",
    "entries": [{"id": "default", "configs": {}}],
    "assetName": null
  }
}
```

新增配方源只要往 `recipes.sources` 加一条（例如 `oeotyan-repo`），配方本身由
`vendor.py` 从上游拉取、改名、修好历史遗留问题（坏补丁、四段版本号）。

## 7. 日常管理（`scripts/cache.py`）

```bash
cache.py list [--package P] [--versions]      # 库存、体积、每条 asset
cache.py graph [--package P]                  # 依赖边 / 谁依赖它
cache.py verify                               # 索引 ↔ 线上 asset 一致性

cache.py remove P@V --dry-run                 # 默认只打印计划
cache.py remove P --include-deps --yes        # 连依赖一起删
cache.py remove P --include-deps --include-dependents --recursive --yes
cache.py remove '*' --all --yes --prune-releases

cache.py trigger --plugin ll --first-only --local     # 本机编
cache.py trigger --plugin ll --workflow --batch 5     # 触发 CI 工作流
cache.py trigger --plugin ll --entry client --local   # 只编某个条目
```

删除顺序：删 release asset → 从索引移除该条目（版本空了删版本、包空了删包）→ 依索引
重生成 manifest → 重刷 release body → 提交推送两个仓库。`--prune-releases` 顺带删除
因此变空的分片，所以被删的元信息不会残留在 body 里。

## 8. 一次构建都做了什么

1. 快照 `xmake-repo` / `levimc-repo`，叠加 `recipes/fixups/`，并修历史遗留（补丁空
   上下文行、xmake 解析不了的四段版本号）。
2. 装对应年代的 xmake，`XMAKE_GLOBALDIR` 指向沙箱（沙箱放在 `<盘>:\.buildcache`，
   因为 CMake 拒绝超过 250 字符的目标文件路径）。
3. PATH 前置 git shim：把**未锁版本**的 clone 回退到该版本发布日期之前；已由配方锁定
   commit 的包不受影响。
4. `xmake require --export` 导出整棵闭包，逐个打包为 `<字母>/<包名>/<版本>/<hash>/`。
5. 上传分片、写索引；`finalize` 从索引重生成 manifest、刷新 release body、校验一致性。

## 9. CI

`.github/workflows/build.yml`：`discover → build(矩阵) → finalize(串行) → verify(消费端)`。
需要的 secret 只有一个 `PACKAGE_REPO_TOKEN`（写第二个仓库用）。

## 10. 排障速查

| 现象 | 原因 / 处理 |
|---|---|
| 日志里没有 `(precompiled)` | manifest 没命中（工具链不是 msvc / configs/buildid 不一致）或 sha256 对不上 → `cache.py verify` |
| `unmatched checksum` | 索引里的 sha 与线上 asset 不一致 → 重新 build+finalize，或先跑 reconcile |
| `manifest.txt not found when installing artifacts` | 压缩包内部层级不对（必须三段后再放文件） |
| `cannot extract ... .tar.zst` | 该包容器不是 7z |
| `unable to parse semver` | 老配方有四段版本号 → `recipefix.normalize_tree` 会剔除 |
| 下载全失败 | 先确认 `gh`/`curl` 能直连；本机可显式 `BUILDCACHE_PROXY` |
