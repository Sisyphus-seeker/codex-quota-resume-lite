# Codex Quota Resume Lite 0.1.2

发布者：[Sisyphus-seeker](https://github.com/Sisyphus-seeker) · [MIT 许可证](LICENSE) · Windows x64 预览版

2026-11-20 修复：支持通过同一 Codex 桌面程序的中间进程启动引擎，逐层核验进程身份，仍要求唯一的数据库持有者。Codemoss IntelliJ 创建的聊天，在当前桌面引擎确实持有聊天锁时也能监控；未加载和归属未知时仍不接管。

2026-11-18 修复：支持 Codex 安装包内的 `app/resources/codex.exe`，保留精确桌面归属及数据库锁核对；状态增加 `health` 和具体兼容性故障原因。旧版可能出现后台仍运行、但扫描持续失败的情况，建议更新。

Windows 简化版额度续接插件。Codex 持续运行时，监控所有新发生的额度中断；额度恢复后向原聊天发送统一的继续消息，无需逐个聊天开启。

已实现真实只读观察、额度查询、精确聊天排队、送达核对、独立后台进程和安装卸载。**当前为预览版**：65 项自动测试通过，已验证一次自然额度中断后的真实自动恢复；兼容性矩阵仍有限。完整范围见 [验证记录](验证报告.md)。

这是第三方工具。它等待原有额度恢复，不增加或绕过额度。

从 Releases 下载 `QuotaResumeLite-v0.1.2-win-x64.zip` 和同名 `.sha256` 文件。GitHub 自动生成的 Source code 压缩包用于查看或开发源码，不包含独立运行时，不能作为安装包使用。

在 PowerShell 中检查 ZIP 的 SHA-256 与同版本校验文件一致后再解压：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\QuotaResumeLite-v0.1.2-win-x64.zip'
```

## 安装和使用

1. 解压发布 ZIP，保持 Codex 桌面版运行，双击 `Install.cmd`。无需另装 Python，默认安装到 `%USERPROFILE%\.codex-quota-resume-lite`，注册本用户登录启动的 Windows 计划任务。
2. 安装初始状态为暂停，未开启真实发送。确认旧的 `codex-auto-resume-windows` 已暂停后，在**安装目录**双击 `Enable.cmd`。只需启用一次，此后自动处理所有新额度故障。旧工具可能仍在发送时，本工具阻止启用，不自行修改旧插件。
3. 保持 Codex 和 Windows 登录会话运行。后台通常每 30 秒观察一次，在额度重置时间后加 30 秒缓冲再确认实际额度；周额度仍不足就继续等待。
4. 安装目录 `Status.cmd` 查看状态，`Pause.cmd` 暂停，再次 `Enable.cmd` 恢复。已经开始的回合不强行停止。

首次启用不接管历史故障。电脑休眠时不能执行，唤醒后重新检查。Codex 关闭、聊天未被桌面引擎加载或状态未知时等待。**本工具不主动加载聊天；切换聊天后能否自动续接取决于桌面引擎是否仍加载该聊天，双聊天真实投递尚未验证。**

安装目录 `Uninstall.cmd` 移除本工具的计划任务、插件登记和程序，默认保留设置及恢复记录。卸载时保持 Codex 运行。解压目录 `RemoveFromArchive.cmd` 也能卸载默认安装；其参数 `--purge` 同时删除设置。不卸载旧插件。

## 全局消息和命令

默认消息：

> 请检查当前进度，继续完成尚未完成的任务，避免重复已经完成的操作。

把自定义消息写入 UTF-8 文件，再运行：

```powershell
$liteRoot = Join-Path $env:USERPROFILE '.codex-quota-resume-lite'
& "$liteRoot\runtime\python.exe" "$liteRoot\app\scripts\quota_resume.py" message --file 'D:\resume-message.txt'
& "$liteRoot\runtime\python.exe" "$liteRoot\app\scripts\quota_resume.py" status
& "$liteRoot\runtime\python.exe" "$liteRoot\app\scripts\quota_resume.py" doctor --usage
```

消息限 1–2000 字符，状态不输出正文。还支持 `pause`、`resume`、`disable`、`start`、`stop`。`resume` 只解除暂停，`enable` 才开启真实发送并启动已安装任务。`stop` 退出自己的 worker，不终止 Codex。`doctor --usage` 联网读取额度，不发送用户消息。

使用自定义数据目录时，在安装前设置 `CODEX_HOME`，后台会保存此目录。命令失败返回非零退出码，不应视为完成。

## 边界和状态

- 只识别结构化 `usageLimitExceeded`，不处理网络错误、HTTP 429 或任意未完成聊天。
- 发送前复核最新回合、实际额度、排队输入、桌面进程及聊天锁。用户已继续或有新输入时取消旧恢复。
- `waiting` 为等待，`queued` 只证明排队接受，`delivered` 表示标记出现在新回合的首条用户输入，不代表任务已完成。
- 提交结果未知或提交期间崩溃，保留 `uncertain`，不自动重发。续接消息附带去重标记。
- 暂停和正常退出会尝试撤回自己的排队消息；已开始的回合不撤销。桌面不可访问或撤回失败时不能保证已移除。
- 不写 Codex 数据库、不提取认证凭据。只读访问元数据、会话首行和核对自有标记所需的局部历史。自己的状态位于安装目录 `config`。

依赖当前 Codex 私有数据库结构、引擎 `queue` 命令和 Windows 进程锁证据。Codex 升级可能令本工具停止工作；无法核实时不发送。包内 SHA-256 用于完整性检查，不提供发布者身份认证。Python 运行时有 PSF 数字签名，插件本身未签名。

## 开发验证

Python 3.12+，无第三方依赖：

```powershell
python -m unittest discover -s tests -q
python -m quota_resume.cli demo
python -m quota_resume.cli demo --uncertain
python -m quota_resume.cli observe
```

演示使用临时目录和虚拟时钟。`observe` 只读真实元数据，不发送、不创建自己的状态。源码默认状态为 `.local`，与安装版独立。

构建发布包需要 PSF Python 3.13 嵌入式运行时：

```powershell
python scripts/build_release.py --runtime 'D:\python-embed'
```

生成独立 ZIP、逐文件校验清单及 ZIP SHA-256，不包含用户配置、聊天、测试缓存。构建校验 Python 主程序、窗口程序和 DLL 的 PSF 签名。安装器只接受构建后的完整发布目录。

数据访问范围见 [隐私说明](PRIVACY.md)，版本变化见 [更新记录](CHANGELOG.md)。第三方许可见 `THIRD_PARTY_NOTICES.md`，Python 许可在包内 `runtime/LICENSE.txt`。

本项目当前没有自动更新器。更新时下载新版本安装包、重新运行 `Install.cmd`，检查安装结果后再次运行安装目录的 `Enable.cmd`；安装会暂停后台并保留原有配置和恢复记录。

报告问题请使用仓库 Issues。提供插件版本、Codex 版本、复现步骤和已脱敏的状态信息；不要上传认证文件、完整聊天记录或状态数据库。
