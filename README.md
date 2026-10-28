# Zotero PDF2zh Community

基于 [guaguastandup/zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) 的**非官方社区维护版**，由 Q1351998764 维护。保留 Zotero 内选中文献、调用本机翻译后端、生成双语 PDF 的用法，重点解决安装依赖、接口响应和错误诊断问题。

[下载安装包](https://github.com/Q1351998764/zotero-pdf2zh-community/releases/latest) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/Q1351998764/zotero-pdf2zh-community/issues)

## Windows 安装

1. 在 Releases 下载 **zotero-pdf2zh-community.xpi** 和 **server.zip**。
2. 把 `server.zip` 解压到独立目录，例如 `D:\ZoteroPDF2zhCommunity`，不要覆盖原版目录。
3. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。已安装 uv 的电脑跳过这一步。打开 PowerShell 可运行 `winget install --id astral-sh.uv -e`；完成后重新打开终端。
4. 双击解压后的 `server\start-server.bat`。首次启动会下载 Python 3.12 和后端依赖；首次翻译还会安装翻译引擎、下载模型与字体，需要联网。翻译期间保持此窗口打开。
5. 在 Zotero「工具 → 插件」中**停用原版 Zotero PDF2zh**，然后从齿轮菜单选择「Install Add-on From File / 从文件安装插件」，选择社区版 XPI。
6. 打开社区版插件设置，确认服务器地址为 `http://localhost:8890`，选择 `pdf2zh_next` 引擎并设置自己的翻译服务、模型和 API Key，然后从一篇文献的一页开始测试。

可打开 `http://localhost:8890/health` 查看服务是否启动。若 8890 端口被占用，请先关闭正在运行的原版翻译后端。旧的「翻译.bat」仍指向旧安装，需要改用新目录内的 `start-server.bat`。

社区版使用独立插件 ID、设置前缀和更新源。首次启用时在本机复制原版已保存的设置，包括 API 配置；不会覆盖社区版已有设置。API Key 不会上传到本项目仓库。实际翻译仍会向你选择的服务商发送待翻译文本。

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

## 4.1.8 修复范围

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
