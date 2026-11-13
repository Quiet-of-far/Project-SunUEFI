# 构建手册

在电脑或 CI 上用 `./build.sh` 构建 UEFI 镜像、Android 引导助手和Arch Linux ARM + niri 系统镜像。所有构建都只在本机和容器里进行，不会向连接的平板写入。

## 环境要求

| 目标 | 构建机 | 容器权限 |
| --- | --- | --- |
| UEFI 镜像、Android 引导助手模块 | x86_64 或 ARM64 的 Linux，装有 Docker | 普通容器 |
| 完整系统镜像（Arch Linux ARM + niri） | ARM64 的 Linux，装有 Docker | `--privileged`，用于 chroot 和挂载 |

GitHub Actions 的 `uefi` 和 `linux` 目标在 x86_64 runner 上构建，`arch-niri` 目标在 ARM64 runner 上构建。

## 取得源码和主机检查

```sh
./build.sh sources     # 取得固定版本的上游子模块，不递归下载测试数据
./build.sh check       # 主机检查，不需要平板
```

上游源码放在 `upstream/`，固定在指定提交。本地修改在 `patches/`、`uefi/`、`linux/`、`tools/`。构建在 `build/` 下的副本里应用补丁，不要改构建副本。

## 构建容器

基础镜像记录在 `config/build-container.json`，定义是 `containers/Dockerfile`。Debian 基础镜像优先从 ECR Public 下载，元数据下载失败时改用 Docker Hub，两个来源固定同一个 OCI 摘要。

```sh
python3 tools/build_container.py     # 生成镜像 sunuefi-builder
```

APT 包版本会被记录，但构建结果不保证逐字节一致。

## UEFI 和引导助手

```sh
docker run --rm -v "$PWD:/workspace" -w /workspace sunuefi-builder bash -euc '
  git config --global --add safe.directory /workspace
  git config --global --add safe.directory "/workspace/*"
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements-build.txt
  ./build.sh uefi
  ./build.sh module
'
```

`./build.sh uefi` 使用 `vendor/piano` 的板级输入和 `patches/firmware`，输出 `artifacts/product/PianoUEFI-product.img` 和 `manifest.json`。完整 UEFI 构建还需要本地提取的原厂材料，见[本地输入](local-inputs.md)。

`./build.sh module` 读取刚构建的产品，编译前置选择器和六个静态 ARM64 工具，生成 `artifacts/android/SunUEFI-Piano-版本.zip`、`manifest.json` 和 `SHA256SUMS`。它不需要平板、原厂 BOOT 或私有采集目录，Android 内核在模块安装时从设备当前 BOOT 提取。默认从经过签名校验的 Debian ARM64 软件包准备编译用的 sysroot，不执行其中的 ARM64 程序。输入和安装行为见 [Android 引导助手](boot/android-module.md)。

合体 BOOT 的底层工具单独提供：

```sh
./build.sh trampoline --stock-boot 当前ROM的boot.img --output 输出目录   # 生成前置选择器入口
./build.sh boot-repack                                                   # 编译原生 BOOT 重打包 / 还原工具
./build.sh boot-request --sysroot ...                                    # 编译 piano-boot-request
./build.sh module-package --inspect                                      # 模块打包工具，只检查已有输入
```

原理见[合体 BOOT](boot/combined-boot.md)。

## 完整系统

本分支只构建 Arch Linux ARM + niri，目标名为 `arch-niri`，构建目录是
`build/distros/arch-niri-piano`。UEFI、内核和硬件服务共用同一套固定源码。
`upstream/debian-piano-current` 只提供硬件服务、适配器和启动工具，
Debian builder/sysroot 用于编译工具及静态早期启动环境，不生成 Debian 系统镜像。

默认内核是 Linux `7.3`，版本后缀 `-piano-sunuefi-cachy`。硬件适配、Gunyah 和对应版本的 CachyOS BORE 调度补丁按主题维护，固定输入见 [`patches/linux/7.3/series.json`](../../patches/linux/7.3/series.json)，当前选择记录在 `build/release/source-manifest.json`。内核和模块输出到 `artifacts/kernels/release/`。只准备内核源码用 `python3 tools/prepare_release_kernel.py --refresh`。具体改动见[内核与调度配置](linux/kernel.md)。

重建内核时，旧的产物清单先作废，编译完成后才原子发布新的，避免旧 Image 配新模块。

### Arch Linux ARM 和 niri

`./build.sh arch-niri-rootfs` 使用 Arch Linux ARM 官方基础包和签名仓库，
安装 niri、greetd、foot、fuzzel、Squeekboard、PipeWire，以及共用的 Piano 内核、Mesa、
固件和硬件服务。相关源码推送会自动构建 `arch-niri`，也可手动选择该目标。
未提供基础包 SHA256 时，构建器下载官方分离签名，用固定版本、固定 SHA256 的
Arch Linux ARM 公共 keyring 验签；验签失败就停止。也可显式传入已核对的 SHA256。

先运行 `./build.sh linux`。把已核对的基础包保存为
`build/arch-niri-inputs/ArchLinuxARM-aarch64.tar.gz`，再在 ARM64 builder 中执行：

```sh
./build.sh arch-niri-rootfs \
  --device-id "$DEVICE_SERIAL" \
  --ssh-public-key /run/piano-build.pub
```

`--base-url` 可以指定同时提供 `.sig` 的官方 HTTPS 镜像。
构建特定缓存归档时，传入 `--base-sha256 "$ARCH_BASE_SHA256"` 锁定它的摘要。Podman 运行 builder 时传入
`--privileged`，挂载项目到 `/workspace`，只读挂载公钥到
`/run/piano-build.pub`。x86_64 builder 还需要已注册的 AArch64 binfmt、
只读挂载 `/proc/sys/fs/binfmt_misc`，以及通过 `--bootstrap-sysroot` 提供
含 ARM64 Debian `busybox-static` 和开发库的 sysroot。构建只绑定基本字符设备，
不需要 rootless Podman 执行 `mknod`。

`--device-id` 与 `--ssh-public-key` 必须成对使用。镜像默认启动 `sshd`，
个人公钥在身份清理后写入 `piano` 的 `authorized_keys`；SSH 不允许 root 远程登录。
仅接受 `.pub` 文件；私钥保留在电脑。SSH 禁止密码登录，主机密钥在平板首次启动时生成。
完整 rootfs 的 `manifest.json` 中，`debug_access` 记录公钥指纹、MAC 和 USB 地址。
相同设备 ID 每次构建生成相同 MAC，不依赖首次启动生成的 machine-id。

Wi-Fi 默认使用固定 MAC，并关闭扫描随机 MAC。保存新的 Wi-Fi 连接后，
用 `nmcli -f GENERAL.HWADDR device show` 核对实际地址，再在路由器上绑定该 MAC
和 DHCP 地址。已有连接若显式设置了随机 MAC，会覆盖全局默认值；
可用 `nmcli connection modify "$CONNECTION" 802-11-wireless.cloned-mac-address "$WIFI_MAC" ipv4.dhcp-client-id mac`
后重新连接。Wi-Fi 凭据通过 `nmtui` 或 `nmcli --ask device wifi connect "$SSID"` 保存。

USB 使用 NCM，插线后的平板端地址为 `10.42.0.2/24`。电脑端需要设置
`10.42.0.1/24`，不设置网关。在 Linux 电脑上先用 `ip -br link` 找到 MAC
与 manifest 中 `usb_host_mac` 相同的接口，然后执行：

```sh
USB_IF=enx替换成实际接口
sudo ip link set "$USB_IF" up
sudo ip addr replace 10.42.0.1/24 dev "$USB_IF"
ssh -i ~/.ssh/id_rsa piano@10.42.0.2
```

使用 NetworkManager 的电脑可保存连接，使以后插线自动配置：

```sh
sudo nmcli connection add type ethernet ifname "$USB_IF" \
  con-name piano-usb 802-3-ethernet.mac-address "$USB_HOST_MAC" \
  ipv4.method manual ipv4.addresses 10.42.0.1/24 \
  ipv4.never-default yes ipv6.method disabled
sudo nmcli connection up piano-usb
```

USB 由 Type-C 插线事件启动，不要求桌面登录，也不等待 Wi-Fi。
构建检查不能代替实机的 USB 枚举和 SSH 连接测试。

### 打包

```sh
./build.sh package --root-size-mib 16384
python3 tools/package_sparse_images.py --bundle artifacts/release --output artifacts/fastboot-sparse
```

`package` 默认读取 `build/distros/arch-niri-piano` 下的 rootfs、清单和 initramfs。
`./build.sh all` 顺序构建固定源码、UEFI、模块、内核、Arch+niri 和安装镜像。

已有根系统时，`./build.sh esp` 只生成启动镜像。启动镜像包含
`/EFI/Linux/sunuefi.efi`，将内核、initramfs、DTB 和参数封装为 UKI，
由 SunUEFI 直接启动。打包使用 systemd 的 `ukify`，默认从 Arch 的
`systemd` 包取 ARM64 stub；也可用 `--stub` 和 `--ukify` 指定输入。

设备固件由 `tools/stage_piano_firmware.py` 安装。`config/release.json` 的
`firmware.overlays` 选择 HyperOS DSP 等固件，来源、大小和 SHA256 记录在清单中。

电源键由 logind 处理：niri 配置保留 `input { disable-power-key-handling }`，
`/etc/systemd/logind.conf.d/50-niri-power.conf` 保留 `HandlePowerKey=suspend`。
两者配套可避免唤醒按键被 niri 再次处理为挂起。默认会话启动 polkit agent，
音量键通过 `wpctl` 调节 PipeWire 音量。

## 产物位置

| 目录 | 内容 |
| --- | --- |
| `artifacts/product/` | `PianoUEFI-product.img` 和 `manifest.json` |
| `artifacts/android/` | 引导助手模块 `SunUEFI-Piano-版本.zip`、`manifest.json`、`SHA256SUMS` |
| `artifacts/kernels/release/` | 内核 Image、模块和 `manifest.json` |
| `artifacts/release/` | `package` 的输出：`esp.img`、`root.ext4.img` 和 `manifest.json` |
| `artifacts/fastboot-sparse/` | 稀疏版 `esp.img`、`root.ext4.img`，以及 `manifest.json` 和 `SHA256SUMS` |
| `artifacts/installer/` | `installer` 的默认输出目录 |

## CI 下载

GitHub Actions 的 **Build products** 在 `main` 上有相关改动时自动运行，也可以手动选择 `all`、`uefi`、`linux` 或 `arch-niri`。只改文档不会触发构建，唯一的例外是 `docs/user/install-from-artifact.md`，改动它会触发整套产品构建。选择规则在 `tools/ci_build_targets.py`。

| 下载名称 | 内容 |
| --- | --- |
| `SunUEFI-Module` | Android 引导助手模块 ZIP |
| `SunUEFI-UEFI-Fastboot` | 用于 `fastboot boot` 的独立 UEFI 镜像 |
| `SunUEFI-ESP` | Arch 系统配套的稀疏 ESP 镜像 |
| `SunUEFI-Root-arch-niri` | Arch Linux ARM + niri 的稀疏 root 镜像 |
| `logs` | 合并的构建日志、来源清单和校验记录 |

模块、UEFI、ESP 和 Arch root 每次各提供一份。安装时把 root 镜像和同一次构建的 ESP 配套使用，步骤见[安装指南](../user/install-from-artifact.md)。

## 安装器

`./build.sh installer` 和 `./build.sh install` 是两件事。

`installer` 只在电脑上整理文件。它把 `install_piano.py`、启动脚本 `install.sh` 和 `install.cmd`、`INSTALL.md`、校验记录复制到一个新目录，`--bundle artifacts/release` 会把磁盘镜像一并放进 `bundle/`，`--product` 则只放 UEFI 镜像。目录必须是新的，已有的目录会被拒绝。

`install` 才会连接平板，由 `tools/install_piano.py` 提供三个操作：

| 操作 | 作用 |
| --- | --- |
| `inspect` | 只读检查平板的分区表（默认） |
| `plan` | 检查平板和镜像，生成安装计划 |
| `apply --plan 计划.json --bundle 镜像目录 --execute` | 更新已有的 `sunuefi_esp` 和 `sunuefi_root`，并读回校验 |

命令行安装器只更新已有的独立分区，首次分区在引导助手里完成。不带 `--execute` 时不会写入任何内容。

## 继续阅读

[仓库地图](repository-map.md)、[测试](testing.md)、[发布要求](release.md)、[贡献指南](../../CONTRIBUTING.md)。
