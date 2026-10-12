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
| 哈希里含工具链，消费端与编译端不同就对不上 | 可以「别名」：同一个压缩包再写一条 hash 离线重算出来的 manifest 条目（见 §6 `toolchain_aliases`） |
| 解包时会**吃掉一层目录** | 包内必须是 `<字母>/<包名>/<版本>/<buildhash>/…`，且 xmake 3.1 不认 `.tar.zst`（用 7z） |
| 校验失败**不会**回退编源码 | 选中了产物但 sha 对不上，xmake 直接报 `unmatched checksum` 让这一步失败 —— 所以发布的 sha 必须与线上字节一致（`finalize` 的 adopt + verify 负责这点；`build` 给沙箱的 manifest 也会按线上 digest 校正） |
| 依赖没编译过就不值得做产物 | header-only 库、纯数据包只是下载 + 复制文件，缓存它们省不了消费端任何时间：安装目录里没有编译器产出的依赖一律不发（`build.py::should_publish`），工程自己的包永远发 |

## 3. 仓库与分支布局

| 位置 | 内容 |
|---|---|
| `main` | 框架：`scripts/`、`plugins/`、`recipes/fixups/`、`config.json`、工作流、文档 |
| `index` 分支 | 生成物：`<包名>/index.json`、`<包名>/versions/<版本>.json` |
| `assets` 分支 | 空分支，只作为 release tag 的落点，避免污染 main 的 tag |
| GitHub Releases | 产物本体，按 `<包名>_<序号>` 分片（每片 ≤800 asset）；asset 名形如 `<原库名>-<buildhash>.7z`，release body 里是**直接下载直链** |
| `MiracleForest/xmake-repo` | **只有 manifest**，没有配方。<br>xmake 找 manifest 用的是 `repository.artifacts_manifest(name, version)`：按**正在解析的包名**在**所有**仓库里找 `packages/<包名>/<版本>/manifest.txt`，所以配方来自官方仓库、产物来自我们这边 —— 消费端写 `add_requires("levilamina 1.4.4")` 就直接命中预编译产物，包名与依赖都不需要改。没有 manifest 的包＝消费端按官方配方自行编源码 |

## 4. 快速开始（本机）

```bash
# 依赖：Python 3.14+、已登录的 gh CLI；第三方包（semver / pydantic / pytest）首次运行会自动 pip 安装
python scripts/cache.py trigger --plugin ll --first-only --local   # 编一个版本并发布
python scripts/cache.py verify               # 校验「索引 ↔ 线上 asset」一致
cd tests/consumer && xmake f -y -p windows -a x64 -m release      # 消费端验证
```

看到 `install levilamina 26.51.6 (precompiled) .. ok` 才算真正走通了预编译；
没有 `(precompiled)` 就是在编源码，去 `cache.py verify` 找原因。

代理只在显式给出时启用：`BUILDCACHE_PROXY=http://127.0.0.1:2080`。

## 5. 消费端怎么用

```lua
add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/MiracleForest/xmake-repo.git")

set_toolchains("msvc")     -- 写在工程文件里才会命中缓存（原因见下表）
set_runtimes("MD")
add_requires("levilamina 26.51.6", {configs = {target_type = "server"}})

target("my-plugin")
    set_kind("shared")
    add_packages("levilamina")
```

工程「怎么声明工具链」会被写进包的 buildhash，所以它决定命中哪条 manifest：

| 工程里的写法 | buildhash 多出来的部分 | 命中 |
|---|---|---|
| 什么都不写（Windows 默认 msvc） | 无 | 产物本身那一条 |
| `set_toolchains("msvc")` | `toolchains=msvc` | `msvc` 别名 |
| `set_toolchains("clang-cl")` | `toolchains=clang-cl` | `clang-cl` 别名 |
| `xmake f --toolchain=clang-cl` | 同 clang-cl | **不命中**，见下 |

前三种都实测打印 `install fmt 11.2.0 (precompiled) .. ok`，用的还是同一份
压缩包。最后一种是 xmake 自身的限制：命令行给的 toolchain 不会让 xmake 去 check 一个
msvc 工具链，而它的预编译选择逻辑要求「存在已 check、带 vcvars 的 msvc」，于是整条
通道被跳过，只能回源码编译。所以**把 `set_toolchains(...)` 写在 xmake.lua 里**。

插件自身代码仍可用 clang-cl（`target` 级别再 `set_toolchains("clang-cl")`），LL 的源码
编译不受影响：SDK 产物与工具链 ABI 兼容，别名只是同一个包的另一个地址。

## 6. 写一个插件（接入新项目）

新建 `plugins/<项目>/plugin.py`，框架只依赖下面这些**约定函数**，都可选：

| 函数 | 作用 |
|---|---|
| `metadata()` | 包名、上游仓库、可能用到的额外包 |
| `list_versions(ctx)` | 返回 `[{version, commit}]`，一般直接读配方的 `add_versions`/`versions.txt` |
| `entries(ctx, version, commit)` | 该版本要编几个条目：`[{"id": "server", "configs": {...}}]`；**这是项目特例的归属地**（LL 的 server/client 就写在这里） |
| `configs(ctx, version, commit)` | 单条目项目的默认 configs |
| `xmake_version` / `toolchain` / `native_toolchain` | 用哪个 xmake、用哪个工具链编缓存 |
| `toolchain_aliases(ctx, version, commit)` | 产物还能被哪些消费端工具链使用：`[{"id": "clang-cl", "toolchain": "clang-cl"}]`，命中同一份压缩包、只补 manifest 条目；只声明这个版本真的支持的那些（见下） |
| `toolchain_plan(ctx, version, commit)` | 这个年代要哪台 CI 镜像（`image`）与 `vs_toolset`；`discover` 把它写进矩阵，`should_skip` 用它判断当前镜像/本机能不能编 |
| `dependency_specs(ctx, version, commit)` | 消费端解析这个版本时拿到的那串依赖；年代 xmake 读不了今天的配方时，用它 + 当前 xmake 补编一遍依赖（见 §8） |
| `register_release_columns(table, ctx)` | 往 release body / `cache.py list` 的表格里注册项目自己的列；两处共用同一张表，空列不渲染 |
| `repair_recipes(config, source, snapshot)` | 项目特有的配方修复（历史版本号、上游改名等），在通用修复之后运行 |
| `recipe_cutoff` / `git_cutoff` | 配方快照与「未锁版本的 git clone 回退到的日期」；偏移量分别是 `recipes.cutoffOffsetDays` 与 `gitShim.offsetDays`（默认都是 3 天） |
| `extra_deps(ctx, version, commit)` | 只在上游自身 `xmake.lua` 里出现的依赖（`extra_packages` 名单内的）：既注入消费端配方，也**在构建时顶层 require**，否则只声明不编译，消费端每次都得自己编 |
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
    "toolchainAliases": [{"id": "clang-cl", "toolchain": "clang-cl"}],
    "assetName": null
  }
}
```

通用插件的全部字段（都写在 `settings` 里，除前三项外都可省）：

| 字段 | 作用 |
|---|---|
| `id` / `display` | 插件名（`cache.py`/`discover.py` 的 `--plugin` 用它）与报表里的显示名 |
| `upstream` / `upstream_repo` | 工程源码仓库（用来取该版本的 commit 日期、读它自己的 `xmake.lua`） |
| `recipe_source` / `recipe_package` | 配方在哪个源里、叫什么名字；**`recipe_package` 同时就是发布出去的包名**（消费端 `add_requires("<它> <版本>")`） |
| `extra_packages` | 工程自己的 `xmake.lua` 里 require、而**属于我们自己会发布的包**（一般是 `["levilamina"]`）：排队列时先把这些 SDK 的 job 排到前面，依赖方等着复用，不用自己再编一遍 |
| `entries` | 要编几个条目（构建维度）：`[{"id": "server", "configs": {...}}, ...]`；条目可加 `from` / `until` 版本区间（含端点），例如 `{"id": "client", "from": "0.3.0", ...}` —— 只有那几个版本真的有这个维度时才声明 |
| `configs` | 单条目项目的默认 configs（`entries` 缺省时用） |
| `toolchain` / `native_toolchain` | 编缓存用哪个工具链（默认 `msvc`）；工程自己用的是另一个时用 `native_toolchain` 说明（只记录进索引） |
| `toolchainAliases` | 还能被哪些消费端工具链命中同一份产物（见下节；只声明这个版本真支持的） |
| `recipeEdits` | 配方里必须改的文本：`[{"find": "...", "replace": "..."}]`，只改**我们沙箱里**的快照 |
| `skipBelow` / `skipVersions` | 声明"这些版本本质编不出来"（年代工具/格式问题、上游源码本身编不过）：不排队列，日志里会说明跳过了哪些 |
| `assetName` | 自定义 asset 文件名模板（默认 `<库名>-<buildhash>.<ext>`） |

接入一个项目的清单：

1. 确认它的版本能在某个配方源里查到（`recipes.sources` 里已有 `levimc-repo` / `oeotyan-repo` / `xmake-repo`；别处再加一条）；
2. 往 `config.json` 的 `plugins` 加一段上面的 `settings`；
3. 本地先看计划：`python scripts/discover.py --plugin <id> --report-skipped --out matrix.json`，
   确认版本、条目、以及"要先编哪个 SDK"都符合预期；
4. 触发一轮：`gh workflow run build.yml -f plugin=<id>`（只想先试一个版本再加 `-f version=<版本>`）；
5. `finalize` 会把 manifest 推到消费端仓库，消费端写 `add_requires("<包名> <版本>")` 即可命中。

年代的 xmake 与 CI 镜像由插件决定（`xmake_version` / `toolchain_plan`）：**工程用自己发布
时的那个 xmake 编**，镜像也随之换成那个年代的（见 §9 与「常见报错」里的 `vs not found!`）。

新增配方源只要往 `recipes.sources` 加一条（例如 `oeotyan-repo`）。构建时框架会把这些
源**快照回该版本发布前后的状态**（保证用那个年代的配方与依赖版本编出那个年代的产物），
顺手修历史遗留问题（坏补丁、xmake 解析不了的四段版本号）—— 修的是**我们沙箱里**的快照，
发布出去的只有 manifest，官方配方、依赖名、版本号一概不动。

快照有两个坑要填，都在 `scripts/lib/recipefix.py` 里：

* 快照取的是**发布之前**（cutoff 的含义），而发布那一下通常就是往配方里加这个版本的
  commit —— 于是快照里根本没有这次要编的版本，xmake 找不到源码就只能**按 tag 拉**，
  而这些仓库的 tag 带 `v`（`v0.12.1` vs 配方里的 `0.12.1`），报的是
  `fatal: Remote branch 0.12.1 not found in upstream origin`（`declare_version` 补上
  版本与 commit，就是发布 commit 后来自己写的那一行）；
* 版本声明了却没有 commit（`add_versions("0.12.1")`），xmake 同样会按 tag 拉
  （`backfill_commits` 用仓库自己的 tag 查出 commit 补上）。

### 别名（同一个产物，多个地址）

xmake 给产物寻址用的是「平台 + 架构 + configs + 源码 hash + 工具链」的哈希，所以
`set_toolchains("clang-cl")` 的工程算出来的地址和用 msvc 编出来的产物天生不一样。
只要产物在两种工具链下 ABI 兼容（Windows 上 clang-cl 与 msvc 共用同一套 ABI/STL），
就完全不必重编，改成多发一条 manifest：

* 插件用 `toolchain_aliases()` 声明还支持哪些工具链（项目特例，不放框架里）；
* 编译结束后，用沙箱里**同一个 xmake** 离线重算该 configs 下的 buildhash
  （`scripts/lib/hashprobe.py`），不重新编译；
* 只有「按产物真实工具链重算出来的 hash == 产物目录名」时才发布别名，所以永远
  不会写出一个没人能匹配的地址；头文件包/宿主工具包本身与工具链无关，直接跳过；
* asset 只有一个，别名只是 manifest 与索引里的额外地址；release body 会写明
  `also serves` 列。

改了 `toolchain_aliases()` 后不需要手工清缓存：条目里记着 `alias_profiles`，
`discover` 会把缺这条信息的旧条目自动重新排队。

`entries()` 与 `toolchain_aliases()` 都应该是**版本相关的**：老版本可能既没有 client
SDK，也没法用 clang-cl 编译，声明了只会让那一轮失败或发出一条谁也用不上的地址。
LL 插件就是这么做的 —— 扫它自己 `xmake.lua` 里 `target_type` 的写法（1.8 起才有
client；1.0–1.7 是硬编码 `is_client = false`，0.8/0.13 连选项都没有），以及
`set_toolchains("clang-cl")` 是否出现（26.20 起才用 clang-cl 编自己）。

索引里 **entry 只属于项目自己的包**：LL 的 `levilamina` 按 server/client 记
两条，依赖项（`fmt` 之类）只记一条 `default` —— 不管哪个 entry 拉进来的都是
同一份产物，把 entry 铺到闭包上只会让账本多算一倍，并把 `target_type` 这种项目特例的
概念安到无关的包上。插件自己的包同理：`levilamina` 本身也是别的插件（bsci 等）
的依赖，那时它的账仍归 LL 自己的构建。

要分清两件事：**包解析用的工具链**（进 buildhash、决定命中哪条 manifest）和**真正
编译源码的工具链**。LeviLamina 的 SDK 是 clang-cl 编出来的 —— 它自己的 `xmake.lua`
给 target 强制了 `set_toolchains("clang-cl")`，而我们的配方 `on_install` 调的正是
LL 自己的 xmake；但**包**是按 msvc 解析的（所以 buildhash 里没有 toolchains 段）。
所以 release body 里 `compiled with` 和 build hash 对应的工具链可以不同，这不是
矛盾：前者说的是「谁编的」，后者说的是「谁在用这个地址取产物」。

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

1. 取配方源：**依赖取当前最新**（消费端解析到的就是它，这样产物地址才对得上），
   **项目自己的那个包回到它的年代**（年代配方才是锁定依赖版本、并配上我们那些修复
   的那一份）；随后叠加 `recipes/fixups/`，并修历史遗留（补丁空上下文行、xmake 解析
   不了的四段版本号）。
2. 装对应年代的 xmake，`XMAKE_GLOBALDIR` 指向沙箱（沙箱放在 `<盘>:\.buildcache`，
   因为 CMake 拒绝超过 250 字符的目标文件路径）。
3. PATH 前置 git shim：把**未锁版本**的 clone 回退到该版本发布日期之前；已由配方锁定
   commit 的包不受影响。
4. 沙箱里放一份**由账本生成**的 manifest 仓库（`<沙箱根>/binary-repo`），把它当作 xmake 的
   binary repository。xmake 找预编译产物是「在**所有**已加载仓库里找
   `packages/<包名>/<版本>/manifest.txt`」，所以少了它连依赖都会重新编：LL 是每个 LL 插件
   共用的依赖 = 每个插件每一轮都重编一次整个 SDK。有了它，依赖按 buildhash 命中就直接下载
   安装（同一份 manifest 生成器也供 `finalize` 用，两边不会跑偏）。
5. `xmake require --export` 导出整棵闭包，逐个打包为 `<字母>/<包名>/<版本>/<hash>/`；
   只下载/复制文件的依赖跳过（没有编译器产出就没有可省的时间），工程自己的包必打。
   打包前先查账本：`(包, 版本, buildhash)` 已经在缓存里、且 revision 未变，就直接
   复用已有产物，不重新打包也不重传（buildhash 就是一次构建的身份）。要强制重做就
   把 `config.json` 的 `revision` 加一。压缩包**不写时间戳**，所以同一个包任何一次
   重打包都是同一份字节，manifest 里的 sha256 不会指向"另一次上传"。
6. 为每个产物离线重算其它消费工具链的 buildhash，作为 manifest 别名记进索引。
7. 上传分片、写索引；`finalize` 从索引重生成 manifest（含别名）、刷新 release body、
   校验一致性。

### 两阶段：SDK 先编，插件复用

LL 插件都依赖 `levilamina`，如果插件轮自己编 LL，就是"每个插件、每一轮都重编一遍整个
SDK"。所以 `discover` 在排队列时会先**把依赖版本提取出来**：读配方的 `add_deps` 与
版本相关的 `dependencies` 表（`["0.13.*"] = {"levilamina >=1.9.2 <1.10.0"}` 这种），
再加上上游工程自己 `xmake.lua` 里 require 的包（`extra_deps`），只保留**属于我们自己
插件的包**（`plugins.project_packages`），按 owner 配方的版本列表解析出真正会用的版本；
如果账本里还没有该版本的产物，就把 **owner 插件自己的 job 排到前面去**（同一个版本只排
一次，多个依赖方共用），依赖方放进第二阶段。

工作流因此是 `build` → `build-dependents` → `finalize`：前者产出 SDK 产物，第二阶段的 job
把前者的 `build-result-*.json`（workflow artifact）下载下来，用
`manifest.write_results` 合成一份 manifest 仓库交给 xmake —— job 之间看不到彼此的
文件，但能看到彼此发布的产物。

拿不准的一律**不排**（例如 `~`/`^` 这种我们不复刻的约束、解析不出 owner 版本的、动态加的
依赖）：依赖方留在第一阶段，按老办法把没发布的那部分自己编掉并作为依赖记录发布。方向是
安全的——任何情况下都往前走，不存在"等一个永远对不上的 hash"。

### 版本谁说了算：工程自己 `xmake.lua` 里的锁

同一个依赖往往被声明两次：配方里是**范围**（`levilamina >=1.7.6 <1.8.0`），工程自己的
`xmake.lua` 里是**它当年真正用的那个版本**（`levilamina 1.7.6`）。xmake 会在满足范围的
版本里挑最新的，而预发布版也算满足（`1.8.0-rc.2 < 1.8.0`），于是
`more-dimensions` 0.12.1 会解析到 1.8.0-rc.2 —— 一个它没写过、而且头文件本身就不是合法
C++ 的 SDK（`mc/util/ThreadOwner.h` 把 `enum class` 的模板默认参数写成 `= 0`）。

更麻烦的是：工程自己的 `add_packages("levilamina")` 不带版本，xmake 会用**沙箱里最好的
那个** —— 也就是外层配方解析出来的那个，而不是工程自己 require 的那个。所以：

* 构建时把工程自己锁定的版本**强制到整棵解析树**（`add_requireconfs("**.levilamina",
  {override = true, version = "1.7.6"})`，见 `build.py::dependency_overrides`）；工程自己
  没写版本时用年代答案，也就是 `dependency_pins` 给工程自己那个 build 的同一个版本；
* `discover` 排队列时必须做**同一个判断**（`plan.own_pins`），否则会去编一个依赖方根本
  不安装的 SDK（还让它等在那里），而真正要的 SDK 一个都没编；
* 产物地址（buildhash）只由平台/架构/configs/源码 hash/工具链决定，跟依赖版本无关，
  所以按工程自己的锁编出来的产物，消费端照样能按名字命中。

## 9. CI

`.github/workflows/build.yml`：`discover → build(矩阵) → finalize(串行)`。
需要的 secret 只有一个 `PACKAGE_REPO_TOKEN`（写第二个仓库用）。定时触发每 2 小时一轮
（`schedule`），改 `config.json` 也会触发一轮；一轮很贵，只想编某个版本时用
`gh workflow run build.yml -f version=<版本>`。
一轮的验收就是 `finalize` 里的 `verify`：索引里每个产物的 url/sha256 与线上 release
逐一比对，对不上会把条目重新排队（见 §8）。

消费端自测（`scripts/selftest.py`）**不进流水线**：它要把整个闭包装进一个沙箱，
而我们没有发布的那些包（还按上游名引用的 `snappy`/`demangler` 之类）在消费端只会
源码编译，这一步等于多编一次、又慢又容易受网络影响。它保留为本地手动工具：

```bash
python scripts/selftest.py --component levilamina                     # 默认写法
python scripts/selftest.py --component levilamina --toolchain clang-cl
```

### 署名（bot 账号）

所有由流水线产生的 commit 和 release 都署名成 GitHub Actions 官方 bot：

```json
"bot": {
  "name": "github-actions[bot]",
  "email": "41898282+github-actions[bot]@users.noreply.github.com",
  "tokenEnv": "BUILDCACHE_BOT_TOKEN"
}
```

* **commit**：`gitops` 给每个提交加 `-c user.name/-c user.email`，用的就是上面两行。
* **release / issue / API**：归属由**用的 token**决定。用默认的 `secrets.GITHUB_TOKEN`
  时，release 会显示为 `github-actions[bot]`；不要设置 `BUILDCACHE_BOT_TOKEN` 就保持这个效果。
* 只有**推送第二个仓库**（`MiracleForest/xmake-repo`）必须用 PAT（`PACKAGE_REPO_TOKEN`），
  那次推送会归属到该 PAT 的持有者——想让它也显示成 bot，就用一个 bot 账号的 PAT，
  并把 `BUILDCACHE_BOT_TOKEN` 指向它（`util.subprocess_env` 会把它设成 `GH_TOKEN`）。

## 10. 排障速查

| 现象 | 原因 / 处理 |
|---|---|
| 日志里没有 `(precompiled)` | manifest 没命中（工具链不在别名列表里 / configs/buildid 不一致）或 sha256 对不上 → `cache.py verify` |
| `unmatched checksum` | 索引里的 sha 与线上 asset 不一致 → 重新 build+finalize，或先跑 reconcile |
| `manifest.txt not found when installing artifacts` | 压缩包内部层级不对（必须三段后再放文件） |
| `cannot extract ... .tar.zst` | 该包容器不是 7z |
| `unable to parse semver` | 老配方有四段版本号 → `recipefix.normalize_tree` 会剔除 |
| `Cannot create bedrock_runtime_api.lib` | 那个年代的 `prelink` 工具与后来的数据/对象格式不同（`bsci` ≤0.1.6 用的 prelink v0.3.0 就是这种），工具本身也修不了 → 用插件配置里的 `skipBelow`/`skipVersions` 声明这些版本不参与预编译 |
| `vs not found!` | 年代 xmake 太老、不认识镜像上的 Visual Studio（例如 3.0.2 对 VS 2026）→ `lib/xmake_env.ERA_IMAGES` 会把这个年代排到 windows-2022 |
| `fatal: Remote branch <版本> not found in upstream origin` | 快照取在发布 commit 之前，配方里还没有这个版本 → `recipefix.declare_version` 会补上 |
| 下载全失败 | 先确认 `gh`/`curl` 能直连；本机可显式 `BUILDCACHE_PROXY` |
| clang-cl 工程没命中缓存 | 先确认 `set_toolchains("clang-cl")` 写在 xmake.lua 里（命令行 `--toolchain=` 不触发选择）；再看 build 日志里的 `alias skipped ...`：头文件包/宿主工具包本来就不需要别名，重算 hash 与产物不符则说明拿来重算的配方或 configs 不是产出该产物的那一份 |
