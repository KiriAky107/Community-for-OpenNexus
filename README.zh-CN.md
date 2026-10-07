<div align="center">

  <img src=".github/assets/opennexus-logo.svg" alt="OpenNexus Logo" width="100" height="100" />

  <h1>Community for OpenNexus</h1>

  <p><strong>OpenNexus 的签名扩展目录</strong></p>

  <p>发现 OpenNexus 扩展，核对权限与来源，并通过独立审核发布签名扩展包。</p>

  <p>
    <a href="README.md">English</a> • <a href="#快速开始">快速开始</a> • <a href="#核心亮点">核心亮点</a> • <a href="#系统架构">系统架构</a> • <a href="#本地开发">本地开发</a> • <a href="https://github.com/KiriAky107/Community-for-OpenNexus/releases">发布日志</a>
  </p>

  <p>
    <a href="https://github.com/KiriAky107/Community-for-OpenNexus/releases/tag/v0.6.0"><img src="https://img.shields.io/badge/Version-0.6.0-5865f2?style=flat-square" alt="版本" /></a> <a href="https://github.com/KiriAky107/Community-for-OpenNexus/actions/workflows/ci.yml"><img src="https://github.com/KiriAky107/Community-for-OpenNexus/actions/workflows/ci.yml/badge.svg" alt="CI" /></a> <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square" alt="Python 3.12+" /> <img src="https://img.shields.io/badge/API-FastAPI-05998b?style=flat-square" alt="FastAPI" /> <img src="https://img.shields.io/badge/Signatures-Ed25519-6366f1?style=flat-square" alt="Ed25519" /> <img src="https://img.shields.io/badge/Metadata-SQLite-003b57?style=flat-square" alt="SQLite" /> <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="MIT License" /></a>
  </p>

</div>

---

当前版本： [v0.6.0](https://github.com/KiriAky107/Community-for-OpenNexus/releases/tag/v0.6.0)。

当前开发新增 `/` 网页目录，支持可分享的包详情、版本记录、搜索和类型筛选。浏览器在读取归档前重新确认发行与签名公钥状态，核对大小和 SHA-256 后提供明确的保存链接。离线元数据会标明状态，下载须重新连接确认。

## 0.6.0 更新

- 目录在数据库查询层过滤、计数和分页，版本按语义版本排序，并提供查询一致的 ETag。
- 配套桌面完整分页、已安装版本和更新审核，支持来源、权限、依赖与兼容信息核对。
- Persona、实验模板、MCP 配置与模型方案使用统一声明契约，模板保留源文件和输入的真实扩展名。
- 作者与审核员 CLI 支持提交校验、独立审核、撤回、举报处理和游标审计，发行保持不可变。
- 提供就绪检查、在线 SQLite 备份、摘要与签名校验及新目标恢复，GitHub CI 验证部署包和固定源码。

## 核心亮点

- **发现扩展**：搜索并分页浏览主题、Skill、Plugin、MCP 配置、Persona、模板和模型方案的版本目录。
- **安装前核对**：查看作者、兼容要求、依赖、权限和签名归档信息，再由桌面端请求安装批准。
- **作者保管私钥**：使用本地 Ed25519 私钥签名，目录接收公钥、签名元数据和归档并核对完整性。
- **独立审核**：提交唯一版本、查看实际状态，由另一位审核员携带理由批准或拒绝。
- **可追溯状态**：举报、撤回和签名密钥撤销均保留不可变版本及审计记录。
- **目录数据恢复**：检查就绪状态、创建 SQLite 在线快照，在向新目标恢复前校验签名和摘要。

## 快速开始

```powershell
uv sync --frozen
uv run python -m community serve
```

开发服务只监听 `127.0.0.1:8081`。部署前将 `COMMUNITY_DATABASE_PATH` 指向受管理目录，并将 `COMMUNITY_ALLOWED_ORIGINS` 设置为明确的逗号分隔白名单。任何外部可访问实例都需要 TLS、认证控制、限流、监控和备份。

打开 `http://127.0.0.1:8081/` 浏览网页目录。仓库中已构建的网页由同一服务进程提供，运行时无需 Node.js 进程。检查 `/health`、`/ready` 和 `/catalog/v1/packages`，再在 OpenNexus 中配置目录地址。需要发布扩展时再创建作者和审核员身份，见[管理命令](#管理命令)。

## 核心工作流

### 1. 查找并安装扩展

在网页按名称或描述搜索、选择类型并打开发行。通过 `/packages/{namespace}/{package_id}?version={version}` 分享指定版本，查看作者、许可、兼容范围、依赖、权限和原始更新说明。点击“下载归档”，大小与哈希核对后再点击“保存已校验 ZIP”。发行撤回、公钥撤销或缺失会禁用下载；公钥状态独立于目录 ETag 重新读取。缓存仅保存本次浏览器会话中相同查询的结果，并显示最后确认时间。

在 OpenNexus 中对比已安装和可用版本后再安装。桌面端验证扩展的密码学签名并请求安装批准；网页的哈希核对不会授予执行权限。

### 2. 发布并审核签名版本

准备清单和 ZIP，在本地签名规范化发行元数据，再运行 `check-package`。使用作者 Token 提交，由独立审核员身份查看并处理。当前 CLI 命令见[管理命令](#管理命令)。

### 3. 处理更新或事件

更新以新的不可变版本发布。发现问题时携带理由举报，作者或审核员可撤回已发布版本。写请求中断后，先查询提交或发行的当前状态，再决定下一步。签名撤销和发行撤回后，相关归档停止提供下载。

### 4. 备份目录

创建在线快照，将返回的哈希另行保管，并在恢复到新数据库路径前执行验证。核对恢复结果后再切换数据库，见[备份与恢复](#备份与恢复)。

## 系统架构

```mermaid
flowchart LR
    Author[命名空间作者] -->|签名元数据与 ZIP| API[Community FastAPI 服务]
    Moderator[独立审核员] -->|批准或拒绝| API
    Client[OpenNexus 客户端] -->|读取目录与压缩包| API
    API --> DB[(SQLite 目录数据库)]
    API --> Inspect[压缩包与清单检查器]
    Inspect --> Verify[Ed25519 签名与 SHA-256]
    API --> Audit[追加式审计记录]
    Client --> Host[OpenNexus 扩展安装器]
    Host -->|独立权限审查| Active[已安装扩展]
```

社区服务验证发布完整性和审核状态；安装仍然是桌面宿主的独立信任决定。已发布不代表扩展自动安全或自动获得执行权限。

## 扩展发布流程

```mermaid
sequenceDiagram
    autonumber
    participant Author as 作者
    participant Catalog as Community API
    participant DB as 目录数据库
    participant Moderator as 审核员
    participant Client as OpenNexus 客户端

    Author->>Author: 构建扩展和规范化发行元数据
    Author->>Author: 使用 Ed25519 私钥签名元数据
    Author->>Catalog: 提交元数据与 Base64 压缩包
    Catalog->>Catalog: 检查请求体和解压限制
    Catalog->>DB: 解析有效公钥与命名空间所有者
    Catalog->>Catalog: 校验签名、哈希、长度、路径和清单
    Catalog->>DB: 保存不可变 pending 提交和审计事件
    Moderator->>Catalog: 查看待审核列表
    Moderator->>Catalog: 携带理由批准或拒绝
    Catalog->>DB: 保存状态与独立审核记录
    Client->>Catalog: 使用 ETag 查询公开目录
    Catalog-->>Client: 返回元数据、兼容性和权限
    Client->>Catalog: 下载未撤回压缩包
    Catalog-->>Client: 返回禁止缓存的 ZIP
    Client->>Client: 再次校验并请求安装权限
```

## 发行状态

```mermaid
stateDiagram-v2
    [*] --> pending: 作者提交唯一版本
    pending --> published: 审核员批准
    pending --> rejected: 审核员拒绝
    published --> withdrawn: 作者或审核员撤回
    published --> reported: 已认证用户举报
    reported --> published: 仅记录审计并继续调查
    published --> unavailable: 签名公钥被撤销
    withdrawn --> unavailable: 下载返回 Gone
    rejected --> [*]
    unavailable --> [*]
```

版本按 `(namespace, package_id, version)` 永久不可变。撤回会保留元数据和审计历史，不能用不同构建静默替换同一版本。

## 数据库关系

```mermaid
erDiagram
    PRINCIPALS ||--o{ SUBMISSIONS : authors
    PRINCIPALS ||--o{ AUDIT : acts
    KEYS ||--o{ SUBMISSIONS : signs

    PRINCIPALS {
        string id PK
        string token_hash UK
        string role
        string namespace UK
        bool revoked
    }
    KEYS {
        string id PK
        string namespace
        bytes public_key
        bool revoked
    }
    SUBMISSIONS {
        string id PK
        string namespace
        string package_id
        string version
        json metadata
        bytes blob
        string author_id FK
        string state
    }
    AUDIT {
        int id PK
        string actor
        string action
        string subject
        string reason
        int timestamp
    }
```

当前 Schema 版本为 `1`。命名空间与签名者之间的逻辑关系在应用事务中校验，即使 SQLite 表没有声明对应外键也不会跳过所有权检查。

## 扩展包验证

每个发行版本必须声明命名空间、包 ID、语义化版本、类型、作者、许可证、SHA-256、长度、平台、架构、应用兼容版本、依赖、权限、变更说明、签名 Key 和 Ed25519 签名。

检查器会：

- 拒绝路径穿越、无效 Windows 路径、忽略大小写的重名、符号链接、加密、未知压缩、过多条目和解压炸弹；
- 按签名元数据校验 ZIP 长度和 SHA-256；
- 要求唯一的类型清单，并核对身份、版本和权限；
- 拒绝 JSON 类扩展中出现密钥或对话历史；
- 在审核过程中绝不执行扩展代码。

| 类型 | 必需清单 |
| --- | --- |
| Theme | `theme.yaml` |
| Skill | `skill.yaml` |
| Plugin | `plugin.yaml` |
| MCP | `mcp.json` |
| Persona | `persona.json` |
| Template | `template.json` |
| Model 方案 | `model.json` |

MCP 参数必须是字符串。凭据通过 `secret_environment_keys` 或 `secret_header_keys` 声明，明文 Token 和 Authorization 值会被拒绝。包不能携带启用、信任、测试或目标版本状态。桌面应用已审核的 MCP 配置后保持禁用，用户另行审核命令、测试连接并启用。

Model 方案需要非空的 `source`、`revision`、`license`、资源对象和验证平台列表。本地 Embedding 运行方案增加 `model_key` 与 `runtime_config`，其中 `embedding_model` 必须与该 key 相同；CPU 线程、内存和超时使用有界整数。桌面在应用设置前核对模型的固定仓库、修订和许可证。安装权重与重建索引仍需用户明确操作。[可执行配置示例](tests/fixtures/community-v1-configurations.json) 与桌面共享，并由 CI 核对；只有元数据的旧方案仍可读取。

## 生态项目

| 仓库 | 职责 |
| --- | --- |
| [OpenNexus](https://github.com/KiriAky107/OpenNexus) | 本地知识库编辑、AI 工作流与经过审核的扩展安装 |
| [Sync for OpenNexus](https://github.com/KiriAky107/Sync-for-OpenNexus) | 可选的自托管知识库同步与恢复 |
| [Community for OpenNexus](https://github.com/KiriAky107/Community-for-OpenNexus) | 独立签名扩展目录与发布审核 |

服务按需启用、分别部署。Sync 使用 `/sync/v1`，Community 使用 `/catalog/v1`；产品版本和协议版本分别维护。

## 管理命令

```powershell
uv run python -m community create-author --id AUTHOR_ID --namespace NAMESPACE --token-file C:/private/author.token
uv run python -m community create-moderator --id MODERATOR_ID --token-file C:/private/moderator.token
uv run python -m community add-key --id KEY_ID --namespace NAMESPACE --public-key-file C:/public/author-key.txt
```

Token 只写入指定的新文件，不在终端打印。服务只接收 Base64 Ed25519 公钥，绝不能接收私钥。Token 文件必须使用操作系统 ACL 限制读取。

作者和审核员可以用同一 CLI 访问 HTTPS 目录。`COMMUNITY_URL` 指定服务地址，`--token-file` 读取现有角色 Token。每次写操作都由独立命令触发，提交前先核对归档和元数据签名。

```powershell
uv run python -m community check-package --release-file release.json --archive-file package.zip --public-key-file author-key.txt
uv run python -m community submit --release-file release.json --archive-file package.zip --public-key-file author-key.txt --token-file author.token
uv run python -m community submissions --package-id PACKAGE_ID --version VERSION --token-file author.token
uv run python -m community reviews --offset 0 --limit 30 --token-file moderator.token
uv run python -m community review --submission-id SUBMISSION_ID --approve --reason "已核对包内容和权限" --token-file moderator.token
uv run python -m community reports --token-file moderator.token
uv run python -m community resolve-report --report-id REPORT_ID --decision addressed --reason "记录处理结果" --token-file moderator.token
uv run python -m community audit --after 0 --limit 30 --token-file moderator.token
```

拒绝审核使用 `--reject`；撤回使用 `withdraw --release-id ID --reason TEXT`，举报使用 `report --release-id ID --reason TEXT`，撤销签名密钥使用 `revoke-key --key-id ID --reason TEXT`。`status --submission-id ID` 返回权限范围内的真实状态。队列支持分页，审计使用游标；举报处理后仍保留原始审计记录。写请求中断时返回退出码 3 和 `OUTCOME_UNKNOWN`，不会自动重发。先查询 `submissions` 或当前状态，再决定后续操作。客户端拒绝重定向；隔离测试的本机 HTTP 地址必须显式添加 `--allow-loopback-http`。

## 备份与恢复

`/health` 检查进程，`/ready` 检查现有数据库的 schema、访问及写事务可用性，检查不改变记录；不可用时返回 503。[systemd 配置](deployment/opennexus-community.service) 和[环境变量示例](deployment/community.env.example) 使用非特权服务账号、私有数据目录和本机监听地址。将源码安装到 `/opt/opennexus-community`，创建 `opennexus-community` 账号，并将已核对的配置放到 `/etc/opennexus-community.env`。使用系统提供的 Python 3.12 或更新版本执行 `uv sync --frozen --python /usr/bin/python3 --no-managed-python`，避免解释器依赖被服务保护的用户主目录。对外的 TLS 反向代理由部署者配置。

```powershell
uv run python -m community backup --output-dir C:/private/catalog-backup
uv run python -m community verify-backup --input-dir C:/private/catalog-backup --expected-sha256 RECORDED_SHA256
uv run python -m community restore --input-dir C:/private/catalog-backup --output C:/private/restored-catalog.sqlite3 --expected-sha256 RECORDED_SHA256
```

备份采用 SQLite 在线快照 API，包含 WAL 中已提交的数据。完成清单记录数据库哈希、记录数量和签名归档检查；请另行保存返回的哈希。校验检查 SQLite 结构、元数据签名及归档摘要，保留已撤销的密钥、已撤回的发行、旧签名版本和审计记录。没有完整清单的中断备份无法通过校验。恢复必须提供已记录的哈希及新的输出路径，不会替换运行中的数据库，也不改变账号或发行状态。备份含账号 Token 哈希，需要操作系统权限保护。核对恢复结果后，先停止服务，再显式修改环境配置中的数据库路径并重启。

## API 概览

| 路由 | 访问级别 | 用途 |
| --- | --- | --- |
| `/`、`/packages/{namespace}/{package_id}` | 公开 | 网页目录与可分享版本详情 |
| `/health` | 公开 | 进程健康 |
| `/ready` | 公开 | 数据库就绪检查 |
| `/catalog/v1/sources` | 公开 | 来源身份和公钥 |
| `/catalog/v1/packages` | 公开 | 支持 ETag 的搜索和分页目录 |
| `/catalog/v1/releases/.../archive` | 有效发行公开 | 下载扩展压缩包 |
| `/catalog/v1/publish/submissions` | 作者 | 命名空间内不可变发布 |
| `/catalog/v1/moderation/reviews` | 审核员 | 独立审核队列和决定 |
| `/catalog/v1/publish/submissions/...` | 所有者或审核员 | 发布状态和丢失回执核对 |
| `/catalog/v1/moderation/audit`、`/reports/...` | 审核员 | 审计分页、举报及带理由的处理 |
| `/withdraw`、`/reports`、`/keys/.../revoke` | 相应认证角色 | 事件和生命周期控制 |

## 本地开发

```powershell
uv sync --frozen
uv run pytest
```

重新构建网页资源需要 Node.js 22+ 和 pnpm 10.28.0，在源码检出目录运行：

```powershell
cd console
pnpm install --frozen-lockfile
pnpm test
pnpm build
cd ..
```

构建输出到 `community/static/`。开发网页时，在 `127.0.0.1:18965` 运行隔离 API，再在 `console/` 执行 `pnpm dev`；Vite 只将 `/catalog` 代理到该实例。生产网页及静态资源使用同源内容策略，禁用内联脚本，并设置 `no-store` 和 `nosniff` 响应头。

测试使用临时数据库和示例扩展。作者 Token、签名私钥和生产目录数据保存在检出目录之外。

## 部署说明

当前 Bearer Token 没有到期机制。生产市场仍需要账户登录、Token 轮换与撤销流程、持久限流、更完善的审核治理、可用性监控、滥用处理和 TLS 反向代理。不得将本原型描述为已经上线的生产市场。

## 安全与参与贡献

- 扩展包不得包含密钥、私钥、个人信息、对话历史或用户 Vault 内容。
- 必须明确许可证；`unknown`、`none`、`unlicensed` 和 `tbd` 会被拒绝。
- 举报与撤回必须提供理由并保留审计记录。
- 漏洞按照 [SECURITY.md](SECURITY.md) 报告。
- 贡献遵循[贡献指南](CONTRIBUTING.md)和[社区行为准则](CODE_OF_CONDUCT.md)。
- 目录缺陷与扩展政策建议使用仓库 Issue 表单。

## 开源协议

本项目采用 [MIT License](LICENSE)。公开扩展包和第三方组件仍适用各自许可证。
