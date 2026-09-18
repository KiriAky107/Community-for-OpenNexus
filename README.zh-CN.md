# Community for OpenNexus

**简体中文** | [English](README.md)

这是独立的目录/提交/审核服务，不存储用户 Vault，不复用 Sync Token。当前实现是 Community v1 的 Alpha 原型。

```powershell
uv sync --frozen
uv run pytest
```

部署前设置 `COMMUNITY_DATABASE_PATH` 指向受管理目录，并设置逗号分隔的 `COMMUNITY_ALLOWED_ORIGINS`。`uv run python -m community serve` 仅监听 127.0.0.1:8081；使用 TLS 反向代理公开读取，作者/审核写接口可进一步限制网络访问。

初始化通过 `create-author` / `create-moderator --id … --token-file …`，作者另需 `--namespace …`。令牌仅写入指定的新文件，不打印；保管该文件并使用系统 ACL 限制读取。`add-key --id … --namespace … --public-key-file …` 导入 Base64 Ed25519 公钥。服务不接收私钥。

当前 API Token 尚无到期策略，生产发布前必须完成账号登录、轮换、撤销管理和限流。当前实现属于 Alpha 工程，不能当作已经上线的市场。

相关项目：

- [OpenNexus 桌面主程序](https://github.com/KiriAky107/OpenNexus)
- [Sync for OpenNexus](https://github.com/KiriAky107/Sync-for-OpenNexus)

## 许可证

本项目采用 [MIT License](LICENSE)。第三方组件继续适用各自的许可证与声明。
