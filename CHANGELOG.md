# Changelog

## 0.3.0 — 2026-09-22

macOS 支持，外加三个既有 bug —— 其中两个 Linux 也受影响。

### 新增：macOS

- **进程与启动标识抽成共享模块 `platform_compat.py`。** 此前 `processes()` /
  `is_codebuddy()` / `boot_id()` 在两个脚本里各有一份拷贝，而 `boot_id()` 的拷贝是**真的
  会坏事**：restore 把值写进 `last-restore.json`，sync 拿它判断「是不是重启过」，两份实现
  必须逐字节一致。现在两边直接绑定同一个函数，测试里断言 `sync.boot_id is
  compat.boot_id`。
- **Linux 继续走 `/proc`，macOS 走 `ps`/`sysctl`：**
  `ps -axo pid=,ppid=,lstart=` 取进程树（`lstart` 充当「同一进程实例」的令牌）、
  `ps -axo pid=,command=` 取命令行、`sysctl -n kern.bootsessionuuid` 取启动标识
  （依次回退到解析 `kern.boottime`、再回退到 pid 1 的启动时间）。
- **命令匹配要对付 macOS 的参数边界丢失**：`ps` 把 argv 用空格拼起来，所以只接受
  「首个 token」或「含 `/` 的 token」的 basename 命中——`node …/bin/codebuddy` 与
  `/bin/sh /tmp/x/bin/codebuddy` 能匹配，`vim codebuddy` 不会误匹配。
- **`processes`/`process_commands` 带 TTL 缓存**，因为恢复脚本在轮询；轮询循环改成每轮
  只读一次进程表，不再是每个候选 pid 一次 `ps`。
- **systemd → launchd。** 两个 LaunchAgent：`local.tmux-codebuddy-pane-sync.sync`
  （`RunAtLoad` + `StartInterval`）与 `…​.restore`（`RunAtLoad` + `AbandonProcessGroup`
  + `--delay-seconds 10`）。`EnvironmentVariables.PATH` 必须显式给出，因为 launchd 默认
  PATH 不含 `/usr/local/bin`，而 tmux 在那儿。
- **安装时绝不 bootstrap 恢复 agent。** `RunAtLoad` 会在安装瞬间触发一次恢复，把用户没要的
  对话全启起来。launchd 每次登录自动加载 `~/Library/LaunchAgents`，正好等价于 systemd 的
  「enable 但不 start」。测试专门断言这一点。
- **启动器默认值按平台区分**：Linux `workbuddy`，macOS `codebuddy`，未知平台兜底
  `codebuddy`。新增 `--launcher-command` / 配置键 `launcher_command`；`--workbuddy-command`
  与 `workbuddy_command` 作为永久别名保留，已有 `config.json` 不用改。
- **安装后自检**：`platform_compat.py` 必须与脚本同目录落地，且两个脚本都能 `--help`
  跑起来（`--help` 会先执行模块级 import，所以在安装时就能发现缺模块，而不是等到登录时才炸）。
- **`--delay-seconds`** 移进恢复脚本（stdlib、可测），替代 Linux unit 里的
  `ExecStartPre=/bin/sleep 20`。

### 修复

- **会话名被当作寻址键（Linux 也受影响）。** manifest 记的是 tmux 会话**名**，恢复时按名字
  下 `-t`。但 tmux 的目标语法是 `session:window.pane`，所以名为 `deepseek4.1_work1` 的会话
  被解析成 session `deepseek4` + window `1_work1`，每一次寻址都报
  `can't find window: deepseek4`，恢复直接失败。现在名字只在 Python 侧匹配，只有 `$N` 会
  进入 `-t`。manifest 新增 `tmux_session_id` 作为**提示**（不是依据——tmux 的 id 由 server
  分配，重启后会重编）。解析与创建都失败时记 `session_unaddressable` 并跳过，不中断其余恢复。
- **socket 发现漏了 `$TMPDIR`（macOS 相关）。** 原来只扫 `/tmp` 与 `$TMUX_TMPDIR`。现在按序
  扫 `$TMUX` → `$TMUX_TMPDIR` → `$TMPDIR` → `/tmp`，按 `resolve()` 去重（`/tmp` 与
  `/private/tmp` 视为同一个），并把上一轮 manifest 里记录的 socket 并入重试。
- **`ensure_position` 在「成功但空」的输出上会死循环。** `session_layout` 对空输出返回 `[]`
  而不是 `None`，原来的 `if layout is None` 判不到，`while len(layout) <= window_order`
  就会无限建窗口。改成 `if not layout`（真实 session 至少有一个 window 一个 pane）。
- **两个测试文件里 `unittest.main()` 出现在类定义之前**，导致 `TestBootAwareManifest`
  （`test_restore.py`）和 `TestPaneOrdinals` / `TestLiveEndpoint` / `TestTitleDisambiguation`
  （`test_sync.py`）**从未执行过**。修好后用例数从 92 涨到 110。
- **两处启动顺序断言会随机失败**：并发启动的 pane 谁先写日志是竞态，断言却要求固定顺序。
  已有同类断言大多用了 `sorted`，漏掉的那两处补齐了。
- **`test_apply_creates_the_pane_and_resumes_the_conversation` 在 macOS 上比较 cwd 字面量**，
  而 tmux 报 `/private/var/...`、`mkdtemp` 给 `/var/...`（同一个目录）。改成比较 `resolve()`。

### 说明

- **macOS 上做不到开机恢复。** LaunchAgent 在登录时运行；能开机运行的 LaunchDaemon 以 root
  待在系统 bootstrap 域里，够不到用户自己的 tmux server（socket 在用户自己的 `TMPDIR` 下）。
  得到的是**登录即恢复**——好在 tmux server 本身也只存在于登录会话里，两者一致。这是 macOS
  的模型，不是妥协。
- 与 `tmux-codex-pane-sync` 保持独立架构：那边**尚未**做同样的移植。

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
