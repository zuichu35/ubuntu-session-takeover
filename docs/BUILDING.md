# 源码构建与验证边界

这是 Ubuntu 26.04 **amd64、GNOME/Mutter 50、libmutter ABI 18** 的实验源码。
构建脚本不等于通用安装器。不要在有重要桌面工作的机器上直接执行安装、迁移或
`tests/live*.py`。推荐使用同版本的独立构建机/虚拟机；本页的 apt 命令会改变该构建机
的软件包，构建步骤本身只写项目的 `vendor/`、`build/` 和 `dist/`。

发布整理时验证了固定 Git 对象获取、补丁应用和轻量单元测试；**没有从全新操作系统
重新完成整个 Mutter/GRD 编译、实体 GDM/PAM 登录或 Windows/macOS 客户端验收**。
源码 commit 固定不意味着 apt 依赖、编译器和输出二进制位级可复现。

## 1. 基础工具与依赖

需要 Ubuntu 26.04 amd64 对应的软件源和 `deb-src`。在专用构建机准备依赖，例如：

```bash
sudo apt-get update
sudo apt-get install git build-essential pkgconf meson ninja-build python3 \
  python3-gi python3-cairo python3-pil gettext python3-docutils \
  gnome-shell libmutter-18-0 gir1.2-mutter-18 \
  libpam0g-dev glslc spirv-tools
sudo apt-get build-dep mutter gnome-remote-desktop
```

上游要求 Meson ≥1.5（Mutter）、GLib ≥2.81.1、FreeRDP ≥3.22、libei ≥1.3.901。
GRD 默认 RDP 构建还需要 CUDA/NVENC **头文件**（Ubuntu `libffmpeg-nvenc-dev`）、
FreeRDP/WinPR、PipeWire、FUSE3、TPM2、Secret、Polkit、Vulkan 与 AAC 等开发依赖；
`glslc` 和 `spirv-opt` 用于生成 shader。使用该发行版的 `build-dep` 解决完整传递依赖，
不能仅安装 README 中几项开发包就假定足够。不要求为纯 CPU 隔离测试安装 NVIDIA 驱动。

隔离集成测试另外需要 `dbus-daemon pipewire wireplumber xvfb xdotool openssl
iproute2 util-linux gir1.2-gtk-4.0`，以及可用的 FreeRDP X11 客户端。当前测试客户端位置
仍为 `vendor/ubuntu-freerdp/root/usr/bin/xfreerdp`。
`scripts/prepare-test-packages.sh` 从 apt 下载固定版本 FreeRDP 和旧 Mutter 测试程序；
这些具体版本在当前镜像中可能不可取得，不能静默改成另一版本后沿用旧验收结论。
这不是基础 Python 单元测试的先决条件。

`scripts/prepare-native-deps.py` 保留为历史的本地 `.deb` 解压助手：它只下载当前主机
缺少的依赖，依赖当前 apt 状态，**不是完整或锁定的 bootstrap**。本页采用专用构建机
安装完整依赖的路径；`vendor/native-deps` 与 `vendor/grd-deps` 可以为空，pkg-config
仍会查询系统标准路径。不要把旧机器的依赖缓存上传到仓库。

## 2. 获取固定上游源码

```bash
python3 scripts/fetch-upstreams.py --plan
python3 scripts/fetch-upstreams.py
python3 scripts/fetch-upstreams.py --check
```

| 目录 | 上游 | 固定 commit |
|---|---|---|
| `vendor/mutter` | `https://github.com/GNOME/mutter.git`，50.1 | `8e9bb806da4a6a4fc87aa4a481b228dba6735cbd` |
| `vendor/gnome-remote-desktop` | `https://github.com/GNOME/gnome-remote-desktop.git`，50.2 | `60423c896a54e3eacb65bd93167e91c1ce5e648c` |
| `vendor/mutter/subprojects/gvdb` | `https://gitlab.gnome.org/GNOME/gvdb.git` | `b54bc5da25127ef416858a3ad92e57159ff565b3` |

GVDB 是 Mutter 的固定子项目；这里提前获取，避免在 Meson 阶段隐式下载。
本项目构建关闭 docs/profiler，不需要其可选 gi-docgen/sysprof 子项目。GDM 是系统运行
依赖，不编译私有 GDM，因而不下载 GDM 源码。

获取脚本只创建缺失目录。已有 checkout 的 revision 和 origin 不符时拒绝；符合时
包括脏工作树在内均保留原样，`--check` 的成功仅证明基线和来源匹配，**不证明修改内容
已经审核**。脚本不执行 reset/clean。可选 `--object-cache /path/to/existing/vendor`
只读取本地 Git 对象，不复制其未提交修改，也不写缓存；适合离线补丁检查。
`--plan` 不联网、不创建目录；`--check` 不联网、不改工作树。

## 3. 应用补丁并构建开发版本

```bash
python3 scripts/patch-native.py
python3 scripts/patch-grd.py
python3 scripts/build-native.py
python3 scripts/build-grd.py --plan
python3 scripts/build-grd.py
python3 scripts/record-native-abi.py --plan
python3 scripts/record-native-abi.py
python3 scripts/record-native-abi.py --check
```

补丁脚本核对完整 Git revision，拒绝无法识别的已有编辑。Mutter 基线是上游 50.1，
加 `patches/ubuntu-50.1-0ubuntu2.4/series.json` 中四个有 hash 的 Ubuntu 补丁，再加项目
接管修改；不是 Ubuntu 的完整补丁集。生成的 `mutter-native-takeover.patch` **已经包含**
这四个补丁，不能重复应用。修改 `takeover/native-*.c/.h` 后重新运行 patch 脚本。

开发构建安装到项目内的 `build/native-prefix` 和 `build/grd-prefix`，不要 sudo。
GRD 的 sysusers/tmpfiles 目标也固定到私有前缀；Meson 的全部安装目标会在安装前检查，
越界则拒绝。PAM helper 由本仓库的 `takeover/local-auth.c` 编译，不从已部署程序复制。
系统 Shell 的 ABI 18 typelibs 被复制到私有前缀，因此需要已安装的匹配系统包。

ABI 记录脚本只读取七个固定的公开系统文件，默认写项目内
`build/native-system-abi.json`；不会查询凭据、改变配置或启动服务。它是诊断快照，
**不是 ABI 兼容性或测试通过的证明**。系统更新后需要重新评估、构建和验证，不能只刷新
hash 就认定兼容。当前 launcher 对系统 hash 差异采取提示策略，也不代表所有未来版本安全。

开发 prefix 编译后包含当前构建目录的绝对资源路径，不能直接搬到 `/opt`，也不能让
无权访问该 HOME 的 GDM 用户使用。用于 GDM 的 GRD 必须按最终 `/opt` 前缀重新编译，
尤其是 PTX、shader、schemas 和其他运行资源。

## 4. 单元测试与隔离测试

```bash
python3 -m unittest discover -s tests -p 'test_source_bootstrap.py' -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

后一个命令包括编译小型 C fixture 的测试，要求开发依赖齐全；某些历史打包测试需要
额外本地 fixture。实际 compositor/RDP 测试需要已构建的二进制和私有网络、D-Bus、
PipeWire、运行目录。不要直接运行 live 测试或在真实 seat0 注入测试状态。

`tests/native_integration.py`、`tests/native_startup_lock.py` 覆盖双锁状态、超时恢复、
错误密码、认证后撤销、旧回调、远程重连、GDM 提前重定向与目标等待。
`scripts/test-*-candidate.py` 是维护者的隔离 harness；部分仍要求预先组装的 test-opt
或特定 bundle，不是 clone 后即用的通用测试入口。headless 假认证通过不能替代真实
用户 PAM、物理屏幕/键鼠和 Windows App/macOS 客户端验收。

## 5. 维护者候选构建与打包

以下计划模式只打印输入/输出，不编译或部署：

```bash
python3 scripts/build-startup-lock-candidate.py --plan
python3 scripts/build-greeter-fix.py --plan
python3 scripts/package-native.py --plan
```

`build-startup-lock-candidate.py` 和 `build-grd-greeter-candidate.py` 使用固定运行前缀
`/opt/ubuntu-native-takeover-login-20261028/build/{native,grd}-prefix`，但只安装到
`build/*/stage` 的 DESTDIR，不写在线 `/opt`。前者直接编译本地 PAM helper，后者检查
PTX/资源前缀和 `/home` 依赖。构建后才可用 `build-greeter-fix.py` 从两个 stage 组装
本地 bundle；locale、helper 都来自新构建，不依赖个人 HOME 或现有安装。

这些日期固定的迁移工具针对既有配置/state，不能当作首次安装器。
`build-handover-fix.py` 仍依赖旧 bundle；installer 的 state/manifest 校验也需要既有部署。
`package-native.py` 保留的是另一个历史 `/opt/ubuntu-native-takeover` **实验性用户服务包**，
不等于新的 GDM 登录迁移包。本轮只检查源代码和轻量路径，尚未重新验证其完整构建与安装。
不要发布本机生成的 `.deb` 或旧 dist；若未来发布二进制，需重新核对完整对应源码
（包括 GVDB）、所有第三方 notices、运行依赖和目标平台验收。
