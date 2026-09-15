# Changelog

## 0.1.0 — 2026-09-14

- 同步 CodeBuddy 会话名为 tmux pane 标题；先备份、再比较、再更新。
- 按 `~/.codebuddy/sessions/<pid>.json` 关联 pane 与对话，不依赖 `/proc/PID/fd`。
- 名字优先取 `/rename` 的 `customTitle`，回退自动生成的 `aiTitle`。
- 比较前剥离 CodeBuddy 状态符号前缀，避免与其渲染互相覆盖。
- 默认 30 分钟 systemd 用户 timer，并在 `UserPromptSubmit` / `Stop` hook 上即时同步。
- 追加式注册 hook，卸载时保留 `settings.json` 的其它条目。
- 写入前复查进程身份、对话与原标题；安装/升级自动备份，卸载保留日志。
- 中文/英文文档与隔离 tmux socket 的集成测试。
