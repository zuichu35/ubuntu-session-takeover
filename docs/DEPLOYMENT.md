# 部署及维护边界

当前发布的是实验性源码。新机器上的完整初始部署流程尚未整理为受支持的一键安装器，本仓库没有可直接安装的 Release 二进制附件。

先阅读 [构建说明](BUILDING.md)，在匹配 GNOME/Mutter ABI 的测试环境构建并运行 [分层测试](TESTING.md)。构建输出、ABI 记录、TLS 与 RDP 凭据应由目标机器自行产生，不从开发机器复制。

## 工具分类

| 工具 | 使用范围 |
|---|---|
| `check-native-health.py` | 默认只读；`--tls` 仅在空闲时进行协议探测，不能证明桌面登录成功 |
| `native-shell.py --check` | 诊断当前构建与系统 ABI 指纹，不修改桌面 |
| `install-native.py` | 依赖匹配的本机集成测试和既有凭据；用于开发部署，不是完整初始安装器 |
| `package-native.py`、`packaging/control.py` | 实验打包/交互设置工具；必须先审核依赖、测试证据与目标布局 |
| `install-greeter-fix.py`、`install-greeter-startup-fix.py`、`install-handover-fix.py` | 有精确旧清单和原始备份的既有安装升级，缺失前置状态应拒绝执行 |
| `build-greeter-fix.py`、`build-handover-fix.py` | 对应维护 bundle 的组包工具；后者依赖旧 bundle，不能用于空目录 bootstrap |
| `disable-native.py`、`disable-login.py` | 显式回退入口，会停止或切换相关 RDP 后端；先阅读具体脚本与安装记录 |
| `reload-native-idle.py` | 等待后端空闲后重载 RDP，不能更新运行中的 Mutter |
| `tests/live_power_save.py` | 需要显式确认的实机测试，会改变显示电源与租约状态；不属于默认 CI |

日期化 `/opt` 前缀与维护脚本中的旧清单锚点属于特定升级协议。只改路径而不重新编译资源前缀会导致 GDM 无法读取资源。`--headless` GRD 在这里选择独立设置/凭据，并不表示它创建了第二个用户桌面。

## 保留当前工作

正在运行的 Wayland compositor 不能在保留其所有客户端的前提下直接替换。准备或原子发布下一次启动的文件，与重启/注销是两个阶段；必须由用户保存工作后正常重新登录或重启加载。

原生后端默认不停止旧共享桌面的服务。只有明确选择 `--retire-legacy-services` 时才执行相关迁移动作。

ABI 指纹变化当前用于诊断，启动器仍尝试加载私有 Mutter；实际动态库加载错误会报错。这不是对任意 GNOME 更新兼容性的保证。

## 实机验收

保存工作并重启后，先停留在 GDM，不要本地登录；从实际客户端连接 3392，在远程 Ubuntu 登录画面验证账号，确认桌面显示与输入。记录是否自动重定向或需要手动重连。

随后检查断开重连保留同一应用、本地密码接回撤销远程，以及保存工作后注销再登录。锁确认失败后的本地恢复界面可用，只证明恢复路径工作，不代表远程登录验收通过。

只读状态检查：

```bash
gdbus call --session --dest org.gnome.Mutter.NativeTakeover \
  --object-path /org/gnome/Mutter/NativeTakeover \
  --method org.gnome.Mutter.NativeTakeover.GetStatus
systemctl --user status ubuntu-native-takeover.service
journalctl --user -b -u ubuntu-native-takeover.service \
  -u ubuntu-native-login-guard.service
```

排查时保留失败启动的日志；仅凭 3392 监听、TLS 握手或新一次本地登录后的正常状态，不能判定此前的开机交接已修复。
