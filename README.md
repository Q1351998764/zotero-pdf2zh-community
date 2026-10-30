# Zotero PDF2zh Community

基于 [guaguastandup/zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) 的**非官方社区维护版**，由 Q1351998764 维护。保留 Zotero 内选中文献、调用本机翻译后端、生成双语 PDF 的用法，重点解决安装依赖、接口响应和错误诊断问题。

[下载安装包](https://github.com/Q1351998764/zotero-pdf2zh-community/releases/latest) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/Q1351998764/zotero-pdf2zh-community/issues)

## Windows 安装

1. 在 Releases 下载 **zotero-pdf2zh-community.xpi** 和 **server.zip**。
2. 首次从原版迁移，建议把 `server.zip` 解压到独立目录，例如 `D:\ZoteroPDF2zhCommunity`，便于验证和回退；原地升级需要先停止后端、备份配置并核对已有依赖环境。
3. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。已安装 uv 的电脑跳过这一步。打开 PowerShell 可运行 `winget install --id astral-sh.uv -e`；完成后重新打开终端。
4. 双击解压后的 `server\start-server.bat`。首次启动会下载 Python 3.12 和后端依赖；首次翻译还会安装翻译引擎、下载模型与字体，需要联网。翻译期间保持此窗口打开。
5. 在 Zotero「工具 → 插件」中**停用原版 Zotero PDF2zh**，然后从齿轮菜单选择「Install Add-on From File / 从文件安装插件」，选择社区版 XPI。
6. 打开社区版插件设置，确认服务器地址为 `http://localhost:8890`，选择 `pdf2zh_next` 引擎并设置自己的翻译服务、模型和 API Key，然后从一篇文献的一页开始测试。

可打开 `http://localhost:8890/health` 查看服务是否启动。若 8890 端口被占用，请先关闭正在运行的原版翻译后端。旧的「翻译.bat」仍指向旧安装，需要改用新目录内的 `start-server.bat`。

社区版使用独立插件 ID、设置前缀和更新源。首次启用时在本机复制原版已保存的设置，包括 API 配置；不会覆盖社区版已有设置。API Key 不会上传到本项目仓库。实际翻译仍会向你选择的服务商发送待翻译文本。

**4.1.11 的修复只需更新 Server，不需要更换 XPI。** 社区 Server 保留原版 XPI 使用的翻译接口，已有原版或社区版 XPI 可继续使用；尚未逐一验证所有历史 Zotero/插件版本的界面。上面的安装步骤面向完整安装社区版的用户。

旧版检查更新若提示 GitHub `403: rate limit exceeded`，可从本仓库 Releases 手动下载 `server.zip`：先停止后端、备份配置，再把包内 `server` 的文件合并到当前 `server` 目录，保留个人 `config`、引擎环境和输出文件。完成后重新运行 `start-server.bat`。新版会在 API 查询失败时尝试公开发布清单，不需要填写 GitHub Token。

### macOS / Linux

安装 uv 后，在解压后的 `server` 目录运行：

```sh
uv run --python 3.12 --with-requirements requirements.txt server.py
```

Python 测试覆盖 Windows 和 Linux；macOS 与实际 Zotero 界面操作尚未手工验证。

## NVIDIA 和 OpenAI 兼容接口

NVIDIA 的 Base URL 填：

```text
https://integrate.api.nvidia.com/v1
```

选择 OpenAI 兼容服务（`openaicompatible`），输入账户实际有权限使用的模型 ID。误填末尾 `/chat/completions` 时，社区版会自动规范化。新安装默认 QPS 为 1、线程池为 2；从原插件迁移的已有值会保留。

社区版兼容标准 `choices` 响应及 `data.choices` 包装响应；空 choices、空译文或输出截断会显示明确错误。它不能消除服务商的排队、限流、模型权限问题或网络超时，也不保证某个模型始终可用。

出现连接中断或等待很久时，先将最大 QPS 和线程池均设为 **1**，只翻译第 1 页并检查正文是否变成目标语言，再决定是否提高并发。QPS 是每秒请求数上限，不是翻译质量；较高设置可能加重排队或触发服务限制。`APIConnectionError` 本身不能区分服务端、代理和本地网络故障。

## 4.1.11 修复范围

- BabelDOC 在最终段落翻译失败时可能保留原文、继续生成 PDF 并退出 0。本版识别这类明确失败，任务标为失败并报告段落数及原因，避免把部分译文当作完整结果返回。已有部分文件保留在后端输出目录。
- 最终失败计数独立于日志轮转，并按事件身份去重子进程日志转发。可以恢复的批量翻译回退、下载备用源和一般重试警告不判为最终失败。
- 修复结构化诊断紧贴终端进度行时被漏读的问题，继续执行密钥与常见正文脱敏。
- GitHub API 查询受限或不可用时，尝试本仓库公开稳定发布清单，并确认对应 Server 包存在；不使用开发分支版本或其他仓库更新源。

本版改善失败识别与更新可用性，不会修复远端 API 的断连。未重新调用真实 NVIDIA/Cline API；使用真实引擎段落处理函数、子进程、Windows spawn 和模拟接口异常验证。内容过滤、静默原文返回和译文质量仍不由该失败事件检查完整覆盖。

### 4.1.10 已有修复

- 修复启动服务的黑色终端反复抖动刷屏：按阶段和计数去重，终端及任务日志约每两秒记录一条简短快照；退出时补齐最后状态。
- 任务百分比仍随引擎输出实时更新，警告和错误即时显示；错误消息中的 `9/10` 等数字不会被误当成翻译进度。
- 子进程使用足够的虚拟终端宽度，避免窄窗口把总进度数字截断。实际终端只显示简短状态，不再重放进度条动画。

本版修改后端进度显示及日志采集；配套 XPI 仅更新版本和说明。验证包含真实 Rich 子进程、BabelDOC 进度处理器及窄窗口回归；本轮没有重新调用 NVIDIA/Cline 翻译 API。以下 4.1.9 的资源与真实翻译验证仍为该版本的记录。

### 4.1.9 已有修复

- 首次翻译资源准备：对 BabelDOC 0.6.2 的字体、CMap、布局模型及分词缓存先行校验，最多同时下载 2 个文件；网络失败时按文件切换可用备用源，避免持续重试同一来源。分词缓存只有官方来源，采用有限重试。
- 每次下载使用 60 秒连接/读取超时和 300 秒下载预算（在读取间检查，阻塞读取仍受网络超时约束）；校验 SHA3-256 及清单中已有的文件大小后才写入正式缓存。已验证的缓存会保留，失败后重试不需要重新安装。
- 引擎警告与错误在 Rich 换行前传给后端，保留完整资源文件名、来源及错误类型，继续执行脱敏和日志限额。
- 修复已注入的富文本样式标签被模型改成双引号后显示在正文中的问题；只恢复本段已知标签，不删除未知标签或正文。

本次没有解决所有 PDF 排版问题。复杂页眉、竖排边栏、短词残留和作者姓名翻译仍可能异常；缺少左括号等畸形样式标签也可能残留，不会通过盲删正文掩盖问题。接口成功生成 PDF 不等于排版与译文已完全正确。服务商排队或超时也可能导致翻译失败。

4.1.9 已在 Windows 全新 Python、uv 缓存及模型/字体缓存下按启动脚本实测：自动下载并校验全部 182 项资源，无需人工补文件；NVIDIA Nemotron 3.5 Bolt 与 Cline DeepSeek V4.1 Flash 均生成论文第一页的中文和双语 PDF。此验证只覆盖后端及第一页，未验证 Zotero 界面和整篇论文排版。

### 4.1.8 已有修复

- `BitStream` 导入失败：检查实际导入能力，定向修复 `bitstring`；新建与更新环境使用经过验证的兼容组合。
- 旧 BabelDOC 的 RapidOCR 地址 404：改用可用资源地址，按预期大小与 SHA3-256 校验，下载完成后再写入缓存。
- `NoneType is not subscriptable`：在 `pdf2zh_next` 的 OpenAI 兼容翻译器中处理响应包装，并验证正文及术语请求的有效译文。
- 翻译失败却看不到原因：保存按任务区分的脱敏日志，将错误摘要关联到任务历史；引擎退出成功但没有 PDF 时也报告失败。
- 独立更新源：插件及后端只从本仓库的发布获取更新，不会自动回到原版。

新建 `pdf2zh_next` 环境固定为：

| 依赖 | 版本 |
| --- | --- |
| pdf2zh-next | 2.9.0 |
| BabelDOC | 0.6.2 |
| PyMuPDF | 1.25.2 |
| bitstring | 4.3.1 |
| openai | 2.3.0 |

已有健康环境会保留原引擎版本；修复适配层在独立入口加载，不直接修改第三方 `site-packages`。直接运行系统中的 `pdf2zh_next` 或使用外部 Windows exe 不会经过这层兼容处理，因此推荐使用社区版 Server 管理的 Python 环境。

## 遇到问题

先检查 `server/logs` 中对应任务的日志，以及后端窗口中的错误摘要。日志会进行密钥与常见请求正文脱敏，并限制大小、数量和保留时间；分享前仍请检查文件名等个人信息。

反馈请提供：插件与 Server 版本、操作系统、服务商和模型名称、复现步骤，以及相关脱敏错误。不要提交 API Key、完整个人配置或未经许可的论文。依赖安装失败可尝试关闭镜像：

```sh
uv run --python 3.12 --with-requirements requirements.txt server.py --enable_mirror false
```

完整参数通过 `server.py --help` 查询。`pdf2zh` 旧引擎和外部 exe 继承上游功能；本版的响应兼容修复与完整引擎回归重点覆盖 `pdf2zh_next`。

## 开发与验证

需要 Git、Node.js 22 和 Python 3.12。克隆仓库后：

```sh
cd plugin
npm ci
npm run lint:check
npm test
npm run build
cd ..
python -m pip install -r server/requirements-dev.txt
python -m compileall -q server
```

设置 `PYTHONPATH=server` 后运行 `python -m unittest discover -s server/tests -p 'test_*.py'`。完整引擎测试由 CI 安装上述固定依赖组合后执行。`python tools/build_release.py` 生成 `dist/` 中的 XPI、Server 压缩包、更新清单和 SHA-256 校验文件；本地新增源文件需经审阅并 `git add` 后再打包，未跟踪文件不进入发布包。

自动化覆盖响应兼容、空响应及截断处理、真实 SDK 与翻译器调用、Windows 多进程、依赖修复、资源校验、日志脱敏、失败 API、设置迁移和发布包结构。真实服务商延迟、所有 PDF 版式以及 Zotero 界面仍需实际使用反馈。

## 来源与许可证

原项目作者：**guaguastandup**。本版本基于上游提交 `c2fecf4`，保留历史与作者署名，修改内容见 [CHANGELOG.md](CHANGELOG.md) 和 [NOTICE](NOTICE)。[UPSTREAM_README.md](UPSTREAM_README.md) 仅保留历史上游说明；社区版以本页的下载和反馈入口为准。

遵循仓库的 [GNU AGPL v3 许可证](LICENSE)，包元数据沿用 `AGPL-3.0-or-later`。分发修改版时请保留许可证、版权和修改说明，并提供对应源代码。本项目不代表原作者、Zotero 或所用翻译服务商。
