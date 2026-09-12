# mail2agent

**用邮件远程继续你的 Codex 桌面任务。**

在 Mac 上发起任务，保持电脑清醒、联网、Codex App 打开。任务完成后收到邮件；直接回复那封邮件，Codex 在原任务中继续执行，再把结果回到同一邮件串。

这是一个本地运行的 macOS MVP：Python 后台负责收发、校验和排队，Codex 负责执行。没有新邮件时，不调用模型，也不消耗模型额度。项目使用 [MIT License](LICENSE)。

## 已实现

- 任务通知：默认一轮工作超过 10 分钟后通知；可明确让某个任务每轮都发，或仅发送一次。
- 同一 Codex 任务沿用同一个邮件串；每封邮件有独立 Message-ID。
- 只允许用户手动创建、明确启用邮件的已有任务；邮件不能创建新任务。
- App 保持打开。邮件与 App 共用原生执行服务，避免两个 CLI 进程争抢任务。
- 当前任务忙碌时等待；用原生队列提交后续指令，不抢占正在运行的一轮。
- 断线后找回原执行结果；不盲目重复执行。发送失败保留同一结果事件重试。
- 邮件授权码保存在 macOS Keychain，账户与状态保存在本机私有目录。

```mermaid
flowchart LR
    Reply[回复任务通知邮件] --> Verify[邮箱适配器验证回复]
    Verify --> Queue[已有任务的执行队列]
    Queue --> Shared[原生 Codex App-server]
    App[Codex App 保持打开] <--> Shared
    Shared --> Result[结果回到同一邮件串]
```

## 安装与邮箱适配器

需要 macOS、Python **3.11+** 和已登录的 Codex App。先克隆项目，再按对应适配器的说明配置邮箱：

```sh
git clone https://github.com/KurosawaGeeker/mail2agent.git
cd mail2agent
```

| 邮箱适配器 | 当前状态 | 配置说明 |
| --- | --- | --- |
| QQ Mail | 已实现并完成真实邮件往返验证 | [安装与配置](docs/providers/qq.md) |
| 其他邮箱平台 | 待适配 | 尚未提供支持 |

项目按邮箱平台中立的方向组织：任务通知、回复验证、执行队列和结果回传构成统一工作流；账号认证、文件夹及邮件头差异由适配器处理。当前实现仍使用首个适配器，支持范围以上表为准。

安装器提供 dry-run，建立独立运行环境、安装 Skill 和 MCP、配置本地启动服务。已有安装、账户状态或自定义 CLI 设置会被保护。

完成正在执行的任务后，首次重开 App 一次以加载共享连接。之后远程使用时保持 App 打开即可。凭据通过 macOS Keychain 配置，不放进命令、`.env` 或聊天。

详见 [安装与验收](docs/install.md)。原生集成测试使用 Codex bundled CLI **0.153.4**；App-server 协议仍是实验性的，升级 App 后应检查兼容性。

## 使用

在你手动创建的 Codex 任务中说明：

> 给这个任务启用邮件回复，并发一封测试邮件。

收到邮件后直接回复，例如：

> 请继续检查测试失败的原因，修好后告诉我结果。

要取消该任务的 10 分钟门槛：

> 这个任务以后每一轮完成都发邮件，不管用了多久。

只发这一次：

> 把这次结果发到我的邮箱。

安装的 Skill 指导 Codex 使用当前真实任务 ID。通知规则由 Skill 和工具调用执行；安装后台本身不会自动接管所有任务。首次验收建议让邮件指令读取一个无敏感内容的本地文件，确认 **原任务出现记录、文件内容正确、结果邮件收到**。

## 为什么别人发来的邮件不能直接执行

监听器读取配置账户经过认证的 **Sent Messages（已发送）** 文件夹，并同时核对启用时的基线、已发通知的 Message-ID 和邮件回复链。普通收件箱来信不会进入执行队列。已发送文件夹是信任边界，请勿将别人的邮件移入其中来绕过校验。

邮件串 UUID 用于定位任务，不是身份认证。`From` 地址或 UUID 单独匹配也不能启动执行。邮箱服务改写发送邮件的 Message-ID 时，适配器需根据本账户的已发送副本校正对应关系。

## 边界与故障处理

- 电脑休眠、合盖或断网时，无法保证及时处理；恢复连接后会重新检查。
- 目前支持本机、单个已适配邮箱账户及明确启用的已有任务。
- 原生工具照常执行；需要审批、用户输入或未知客户端工具请求时会报告状态，不自动批准或伪造结果。
- 若执行结果不确定，保留原 claim 和执行记录，不重新提交原指令。
- `accepted` 表示 SMTP 已接收，不等于用户已看到邮件。SMTP 结果不确定时，需确认收件或明确授权可能重复的重发。
- 私有状态可能包含待处理邮件和执行输出。不要把状态数据库、日志或邮件原文上传到 issue。

恢复和权限边界见 [Skill](skills/qq-mail/SKILL.md) 与 [安全说明](SECURITY.md)。

## 开发与验证

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/check_release.py
```

常规测试使用合成邮件和临时目录，不连接真实邮箱或调用真实模型。原生集成测试的命令见 [贡献说明](CONTRIBUTING.md)。

它使用临时 Codex 配置和本地固定响应，验证同一任务上下文、工具执行、忙碌等待与丢失提交回执后的恢复。原生协议测试不能代替你的真实 App 和邮箱验收。

```text
scripts/                 收发服务、执行适配器、安装与发布检查
tests/                   合成邮件、状态恢复和原生集成测试
skills/qq-mail/SKILL.md   Codex 使用规则
docs/install.md          通用安装与验收
docs/providers/          各邮箱适配器的配置说明
```

发布前还应扫描整个 Git 历史，并人工检查发布文件；`check_release.py` 只是基础保护。贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)。本项目是独立社区工具，与 OpenAI 及邮箱服务商无隶属关系。
