# Changelog

## 0.2.0 — 2026-09-21

- 新增「记录 + 开机恢复」：把每个 pane 打开的对话写进 `restore-manifest.json`，重启后重建
  tmux 布局并在每个 pane 里 `workbuddy -r <会话 id>` 恢复**原对话**。
- 对话识别改用进程自己的本地端点 `/api/v1/sessions/live`（权威；实测 16/16，并纠正 4 个
  在 `/resume` 后已过期的 pid 文件），端点不可达时才退回 pid 文件或标题反查。
- 清单改记**布局序号**而非 tmux 下标：实测下标会在几分钟内漂移（`6/13` → `0/0`），
  绝对下标无法跨重启成立。
- 恢复保证幂等：已有 CodeBuddy 的 pane 跳过；同一对话只恢复一次；只创建缺的 window/pane；
  新 session 复用自带 pane（修掉同一对话被启动两次的 bug）。
- 新增启动校验 `--verify-seconds` 与限流 `--stagger-seconds`；预览模式严格只读。
- 新增 `tmux-codebuddy-pane-sync-restore.service`，安装时 enable 但不立即执行。

## 0.1.0 — 2026-09-14

- 同步 CodeBuddy 会话名为 tmux pane 标题；先备份、再比较、再更新。
- 按 `~/.codebuddy/sessions/<pid>.json` 关联 pane 与对话，不依赖 `/proc/PID/fd`。
- 名字优先取 `/rename` 的 `customTitle`，回退自动生成的 `aiTitle`。
- 比较前剥离 CodeBuddy 状态符号前缀，避免与其渲染互相覆盖。
- 默认 30 分钟 systemd 用户 timer，并在 `UserPromptSubmit` / `Stop` hook 上即时同步。
- 追加式注册 hook，卸载时保留 `settings.json` 的其它条目。
- 写入前复查进程身份、对话与原标题；安装/升级自动备份，卸载保留日志。
- 中文/英文文档与隔离 tmux socket 的集成测试。
