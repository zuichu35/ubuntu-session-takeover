# 实验打包工具

当前 GitHub 发布只包含源码，不附带可直接安装的 `.deb`、本机 runtime 或凭据。
完整使用边界见 [部署说明](../docs/DEPLOYMENT.md)，构建依赖见 [构建说明](../docs/BUILDING.md)。

`package-native.py` 与 `control.py` 保留用于维护实验性的单用户原生桌面包。
这个包采用 `/opt/ubuntu-native-takeover`，与日期化的 GDM 登录迁移 bundle 是不同路径；
不要混用两套安装记录、manifest、编译前缀或回滚脚本。

## 打包前提

- 已有匹配 GNOME Shell / Mutter ABI 的构建产物、完整运行依赖与 ABI 诊断记录。
- 运行过对应二进制的隔离集成测试，而非借用开发机器的旧结果。
- 核对完整对应源码、GVDB 等子项目及各依赖许可证；当前源码仓库不包含离线依赖缓存。
- 在专用测试机上验证生成包的安装、正常登录、远程接管与回退。

可先查看不执行构建的计划：

```bash
python3 scripts/package-native.py --plan
```

后续若自行生成并审核安装包，`control.py` 的 setup 会交互设置 RDP 凭据及本机 TLS
材料。此入口不等于已完成的新机器 GDM 登录安装方案。仓库不提供现成账号密码，
也不要把这些材料加入 Git。

修改 Mutter 需要用户保存工作并重新登录/重启加载；安装准备和桌面切换应分开进行。
RDP 断开保留应用，注销则会关闭应用。本机接回使用独立密码认证界面，
Ctrl+Alt+Delete 不会绕过密码认证。
