# tmux-codebuddy-pane-sync

[English](README.en.md) · [工作原理](docs/design.md) · [MIT License](LICENSE)

**把 CodeBuddy 对话名同步为 tmux pane 标题，每 30 分钟先备份、再比较、再更新，并在每轮对话结束时立即同步一次。**

同时记录**每个 pane 当前打开的是哪个对话**，机器重启后自动把 tmux 的 session/window/pane 重建出来，并在每个 pane 里 `workbuddy -r <会话 id>` 恢复**原来那个对话**（不是新开一个）。

适合同时打开多个 CodeBuddy CLI 的用户：pane 不再只有一个笼统的名字，而是“公司法了解”“全球同步-Windterm”等可读的对话名。

```
之前                                         同步后
%67  ✳ Load machine and storage info        %67  全球同步-Windterm
%77  ✳ List China Company Law chapters      %77  公司法了解
%71  codex                                  %71  codex（不是 CodeBuddy，保留）
```

同步来源是 **CodeBuddy 保存的对话名**：

1. `/rename` 写下的自定义名（transcript 里的 `custom-title`）**优先**；
2. 没手动改过名的，用 CodeBuddy 自动生成的标题（`ai-title`）。

参考项目：[tmux-codex-pane-sync](https://github.com/PukerWonderLand/tmux-codex-pane-sync)（同一套架构，Codex 版本）。

## 快速安装

在 **运行 tmux 和 CodeBuddy CLI 的 Linux 服务器上**，用它们所属的普通用户执行：

```
git clone https://github.com/PukerWonderLand/tmux-codebuddy-pane-sync.git
cd tmux-codebuddy-pane-sync
./install.sh
```

安装会：备份已有安装文件 → 安装用户级 systemd service/timer → 在 `settings.json` 里**追加**两个 hook → 立即同步一次 → 之后每 30 分钟执行。

**不需要 sudo、pip、API key，也不会启动模型请求。** 只使用 Python 标准库。

要求：Linux、Python **3.9+**、tmux、可用的 systemd 用户管理器。直接运行脚本不需要 systemd。

```
# 只看会改什么，不改名
python3 tmux_codebuddy_pane_sync.py --dry-run --verbose

# 单独执行一次同步
python3 tmux_codebuddy_pane_sync.py --apply
```

## 两种触发方式

| 方式 | 时机 | 作用 |
|---|---|---|
| CodeBuddy hook | 每次 `UserPromptSubmit` / `Stop` | `/rename` 或新标题在**本轮结束时立即生效**，只处理当前会话 |
| systemd timer | 默认每 30 分钟 | 兜底；并处理 CodeBuddy 之后又把标题覆盖回去的情况 |

hook 由 `install.sh` 追加到 `~/.codebuddy/settings.json`，**不会改动或删除**你已有的 hook 条目（例如计费/归档用的 `codebuddy_turn_hook.py`）。卸载时会原样移除，只留下你自己的条目。

> **CodeBuddy 只在启动时读取一次 hooks 快照。** 因此改动 `settings.json` 之后，正在运行的会话仍会用旧快照：在新会话里生效，或者用 `/hooks` 菜单审阅后应用。已经开着的会话由 timer 兜底。hook 命令不向 stdout 写任何内容——`UserPromptSubmit` 的 stdout 会被当作上下文加入对话。

## 重启后自动恢复对话

每次完整扫描都会把「哪个 pane 打开哪个对话」写进
`~/.local/state/tmux-codebuddy-pane-sync/restore-manifest.json`：

```json
{"version": 1, "captured_at": "2026-09-21T15:21:20+08:00",
 "panes": [{"session": "deepseek4_1_work5", "window_order": 0, "pane_order": 2,
            "cwd": "/home/codex", "session_id": "01a09eb5-f596-7494-...",
            "session_id_source": "endpoint", "title": "TMU的pane自动更新"}]}
```

重启后由 `tmux-codebuddy-pane-sync-restore.service`（`WantedBy=default.target`，你的 `Linger=yes`
所以无需登录）执行：起 tmux server → 按 manifest 重建缺失的 session/window/pane → 逐 pane 检查
**是否已经跑着 CodeBuddy**（有就跳过，绝不打扰）→ `tmux send-keys "cd <cwd> && workbuddy -r <id>"`。

```
# 先看计划，什么都不改
codebuddy_restore.py --dry-run

# 真正恢复（正常由开机服务调用）
codebuddy_restore.py --apply

# 只恢复某几个 tmux session，便于小范围验证
codebuddy_restore.py --apply --only-tmux-session deepseek4_1_work5
```

顺序与安全保证：

- **幂等**：已经在跑 CodeBuddy 的 pane 一律跳过，所以重跑服务、或你已手动开过都不会重复启动。
- **同一对话只恢复一次**：manifest 里同一 `session_id` 出现多次时只处理第一条。
- **只创建缺的**：缺 window 才建 window，缺 pane 才 split；新建的 session 会复用 `new-session`
  自带的首个 pane，不会多切一个。
- **重启后校验**：`--verify-seconds`（默认 5 秒）内没在 pane 里看到 CodeBuddy 就记
  `launch_unverified`，而不是假装成功。日志在
  `~/.local/state/tmux-codebuddy-pane-sync/restore-log.jsonl`。
- 用户目录已不存在 → `missing_cwd` 跳过；`--stagger-seconds`（默认 3 秒）避免开机瞬间同时拉起十几个 CLI。

后台任务（`codebuddy ps` 里的 `kind=bg`）不在 pane 内，本工具不处理；官方有
`codebuddy respawn <idOrName>` 可以在保留对话的前提下重启它们。

## 同步规则

1. 发现当前用户的 tmux sockets，遍历其中所有 session/window/pane。
2. 用 pane 的 PID 与子进程树找到运行 CodeBuddy 的进程，再问**进程自己的本地端点**
   `GET http://127.0.0.1:<port>/api/v1/sessions/live` 拿它当前显示的对话 ID
   （端口取自 `~/.codebuddy/sessions/<pid>.json` 的 `url`；请求头 `X-CodeBuddy-Request: 1`
   是文档写明的 CSRF 防护，不是密钥）。
3. 只读该对话的 transcript，取最后一次 `custom-title`（没有则取 `ai-title`）。
4. **先把所有 pane 的原标题、对话名和身份信息写入日志并刷到磁盘。**
5. 比较两个名字；一致就记录 `unchanged`，不一致才更新。
6. 写入前再次核对进程身份、对话和原标题；发生变化就跳过，下一轮重试。
7. 读回标题，记录成功、被覆盖或失败等结果。

比较时会先剥掉 CodeBuddy 自己写在标题前的状态符号（如 `✳`、`⠴`、`⠇`）。否则“`✳ 名字`”与“`名字`”会被判为不同，每轮都重写，与 CodeBuddy 的渲染反覆互相覆盖。

无法唯一匹配、名字含终端控制字符的 pane 都只记录、不改名。多个 CodeBuddy 会话挂在同一个 pane 时保守跳过（`ambiguous_sessions`）。**不会**把第一条用户提示词当作对话名。同一个 pane 被多个 session 共享时只处理一次。

## 文件位置

默认安装位置如下；遵循 `CODEBUDDY_HOME`、`XDG_CONFIG_HOME` 和 `XDG_STATE_HOME`，也可以在安装时指定。

| 内容 | 默认位置 |
|---|---|
| 已安装脚本 | `~/.local/bin/tmux-codebuddy-pane-sync.py` |
| 恢复脚本 | `~/.local/bin/codebuddy_restore.py` |
| CodeBuddy hook | `~/.codebuddy/hooks/codebuddy_pane_sync_hook.py` |
| 配置 | `~/.config/tmux-codebuddy-pane-sync/config.json` |
| 备份与结果日志 | `~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl` |
| 恢复清单 | `~/.local/state/tmux-codebuddy-pane-sync/restore-manifest.json` |
| 恢复日志 | `~/.local/state/tmux-codebuddy-pane-sync/restore-log.jsonl` |
| 旧版安装备份 | `~/.local/state/tmux-codebuddy-pane-sync/install-backups/` |
| systemd 单元 | `~/.config/systemd/user/tmux-codebuddy-pane-sync.{service,timer}` |
| 开机恢复单元 | `~/.config/systemd/user/tmux-codebuddy-pane-sync-restore.service` |

日志采用 JSONL，每行一条 JSON，包含时间、运行 ID、socket、session、pane ID、CodeBuddy 对话 ID、原标题、对话名和结果。`backup` 记录先于本轮任何改名，`result` 记录实际处理结果。首次创建日志权限为 `0600`。

```
# 查看最近的备份和处理结果
tail -n 20 ~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl

# 只显示真正改过名的记录
grep '"status": "updated"' ~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl

# 查看下一次执行时间
systemctl --user list-timers tmux-codebuddy-pane-sync.timer

# 查看服务运行日志
journalctl --user -u tmux-codebuddy-pane-sync.service -n 30 --no-pager

# 立即执行一次
systemctl --user start tmux-codebuddy-pane-sync.service
```

日志**仅保存在本机**，不上传 GitHub 或任何服务。它包含你的对话名，请勿直接提交到公开仓库。历史日志不会自动删除或轮转；需要时自行归档。

## 配置

```
# 更改周期（重新安装会保留历史日志）
./install.sh --interval-minutes 30

# 使用另一套 CodeBuddy 数据或其他日志目录
./install.sh --codebuddy-home /path/to/.codebuddy --state-dir /path/to/log-directory

# 只用自动标题，或只用 /rename 的名字
./install.sh --name-source ai        # auto（默认）/ custom / ai

# 只装 timer，不注册 hook（不修改 settings.json）
./install.sh --no-hook

# 非标准 tmux -S socket：只处理明确指定的服务端，可重复传入
./install.sh --socket /path/to/tmux.sock --socket /another/tmux.sock

# 不安装开机恢复服务（只做标题同步）
./install.sh --no-restore

# 指定恢复时用的启动器与开机启动间隔
./install.sh --workbuddy-command /home/codex/.local/bin/workbuddy --stagger-seconds 3
```

恢复相关配置写在同一个 `config.json`：`workbuddy_command`、`stagger_seconds`、`verify_seconds`、
`restore`（是否安装开机服务）、`manifest`（清单路径）。

没有显式 socket 时，发现 `/tmp/tmux-UID/*`、当前进程 `TMUX_TMPDIR` 下的 sockets 及 `TMUX` 指向的 socket。systemd 不一定继承交互 shell 的环境；非标准位置请使用 `--socket` 固化配置。只处理当前用户拥有的 socket。

重新安装默认保留之前的周期、数据路径、socket 与 `name_source` 配置。修改周期后需要重新运行安装脚本，才能重新生成 timer。清空显式 socket 配置可编辑 `config.json`，将 `sockets` 改为 `[]`。`hooks` 键记录本次安装是否注册 hook。

`--no-start` 只安装文件，不调用 systemd，适合先检查生成结果。无 systemd 环境可以用自己的调度器每 30 分钟运行 `python3 tmux_codebuddy_pane_sync.py --apply`。

### SSH 断开与重启

用户 timer 已启用，会随用户 systemd 管理器启动。如果希望在没有登录会话时也持续运行，检查：

```
loginctl show-user "$USER" -p Linger
```

若为 `no`，可按服务器的管理规则启用 `loginctl enable-linger "$USER"`（有些系统需要管理员权限）。本工具不会修改 linger 设置。机器重启后不会复原 tmux 会话；它只处理那时实际存在的 panes。

### 停止与卸载

```
# 暂停自动运行
systemctl --user disable --now tmux-codebuddy-pane-sync.timer

# 恢复
systemctl --user enable --now tmux-codebuddy-pane-sync.timer

# 只看开机恢复服务是否已启用
systemctl --user is-enabled tmux-codebuddy-pane-sync-restore.service

# 完全卸载脚本、hook、恢复服务和 systemd 单元；保留日志和配置
./uninstall.sh
```

卸载只删除本工具追加的 hook 条目，`settings.json` 的其余内容保持不变。卸载不会恢复已改过的标题。要手动恢复普通标题，可以从对应的 `backup` 行读取 `pane_title`，再使用 tmux `select-pane -T` 设置；先确认 pane ID 仍指向原来的 pane。

### 更新

```
git pull --ff-only
./install.sh
```

安装器自动备份旧脚本、单元、配置和 hook，历史日志保持不变；重复安装是幂等的。

## 兼容性与边界

- 依赖 Linux `/proc`，不是 Windows/macOS 原生安装器。可在 WSL 的 Linux 环境使用，但仍需满足 tmux、进程可见性和 systemd 条件。
- 靠 `~/.codebuddy/sessions/<pid>.json` 把 pane 关联到对话。CodeBuddy **不会**持有 transcript 文件句柄，因此本项目不扫描 `/proc/PID/fd`（`tmux-codex-pane-sync` 的做法在 CodeBuddy 上找不到任何东西）。
- CodeBuddy 的本地目录布局与 transcript 事件属于内部实现，未来版本可能变化；不支持的结构会报错并保留 `no_saved_chat_name` / `unreadable_transcript`，而不是猜测名字。
- **CodeBuddy 自己渲染的是自动标题，不是你的自定义名。** 因此 `/rename` 之后，CodeBuddy 下一次刷新标题时可能把自定义名覆盖掉。hook 会在每轮结束时重申，日志里的 `verification_mismatch` 可用于观察这种覆盖；本工具不保证永远赢。
- 必须能看见正在运行的进程。权限限制、容器隔离、远端 CodeBuddy 都会导致跳过。
- 此工具不安装或升级 tmux、不修改 shell/CodeBuddy 的标题设置、不向其他 pane 发送按键，也不主动给 CodeBuddy 对话改名（不输入 `/rename`）。
- 本机 tmux 3.2a 没有 `allow-set-title`，本项目不依赖该选项。
- 只扫描当前用户拥有且可访问的 tmux 服务端；不会扫描其他用户或远程主机。
- 与 `tmux-codex-pane-sync` 完全独立：不同的 App 名、配置目录、日志和 systemd 单元，可同时安装。

恢复相关的边界：

- **布局只记「顺序」，不记 tmux 下标。** 实测 tmux 的 `window_index`/`pane_index` 会随增删窗口和 pane
  漂移（几分钟内就从 `6/13` 变成 `0/0`），所以 manifest 记录的是窗口在 session 内的序号、以及 pane 在
  窗口内的序号。恢复出来的下标可能与原来不同，但窗口数、每个窗口的 pane 数和**哪个对话在哪个位置**一致。
- `/api/v1/sessions/live` 官方标注 **Beta**，字段可能调整；端点不可达时会退回
  `sessions/<pid>.json`（该文件在 `/resume` 之后会指向旧会话，所以端点才是权威）。
- **必须先记录再重启**：端口是进程本地的，重启后端口必然变化，无法事后补查。清单每 30 分钟和每轮对话
  结束都会刷新。
- `kind=bg` 的后台任务不在 pane 内，不参与恢复（用 `codebuddy respawn`）。
- 恢复会真实地在 pane 里执行 `workbuddy -r <id>`：如果清单里的 cwd 已删除会跳过（`missing_cwd`），
  已经跑着 CodeBuddy 的 pane 一律不碰。

## 开发与测试

只使用 Python 标准库，无第三方运行依赖：

```
python3 -m unittest discover -s tests -v
```

测试使用临时目录和**独立 tmux socket**，不操作已有会话。覆盖自定义名优先、`aiTitle` 回退、状态符号剥离（防互相覆盖）、`#` 转义、控制字符、备份失败禁止改名、会话唯一性、进程身份变化、共享 pane、hook 注册幂等与卸载保留、以及 `--only-session` 不写无关记录。GitHub Actions 使用 Python 3.9/3.12。

欢迎 issue 和 PR。提交复现时请用虚构对话名，避免附上个人日志或 CodeBuddy 数据。
