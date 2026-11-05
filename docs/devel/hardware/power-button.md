# 电源键、合盖与睡眠

内核负责按键、保护套开关、面板电源、系统挂起和唤醒。Arch+niri 的电源键策略
交给 systemd-logind，音量策略由 niri 快捷键调用 PipeWire 工具处理。

## 电源键配置

niri 的 `~/.config/niri/config.kdl` 保留：

```kdl
input {
    disable-power-key-handling
}
```

`/etc/systemd/logind.conf.d/50-niri-power.conf` 配套设置：

```ini
[Login]
HandlePowerKey=suspend
```

这两项由 `tools/assemble_rootfs.py` 安装。修改后重启会话或系统，使 niri 的
电源键 inhibitor 与 logind 配置同时生效。只禁用 niri 按键处理而没有配置 logind，
会落到 logind 的默认策略，不能单独删除其中一项。

## 唤醒后立即再次挂起

实机日志显示，原配置下内核完成恢复后，niri 会再次请求挂起；USB 短暂重新枚举
后断开。该次故障发生在桌面按键处理，不能仅凭黑屏或 USB 断连推断 DSI 恢复失败。

改由 logind 处理后，实机完成一次物理电源键挂起和唤醒；显示保持点亮，USB 和
Wi-Fi SSH 恢复，同一启动 ID 下没有再次挂起，恢复失败计数为零。这次验证使用
`mem/deep`，不代表其他睡眠模式或长时间待机耗电已经验证。

## 构建策略

构建启用 `sleep.target` 和 `suspend.target`，禁用休眠、混合睡眠及先睡眠后休眠。
不安装原 GNOME 专用的电源键助手，也不增加读取按键的常驻进程。
合盖、自动锁屏和空闲策略可在桌面定制时另行配置。
