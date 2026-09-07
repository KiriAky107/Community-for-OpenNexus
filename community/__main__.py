"""社区部署 CLI；仅接收公钥，生产访问令牌写入指定的新文件。"""

import argparse
import base64
import os
from pathlib import Path

from .app import Registry, create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "create-author", "create-moderator", "add-key"])
    parser.add_argument("--id")
    parser.add_argument("--namespace")
    parser.add_argument("--public-key-file", type=Path)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    registry = Registry(Path(os.environ["COMMUNITY_DATABASE_PATH"]))
    if args.command == "serve":
        import uvicorn
        origins = tuple(x for x in os.getenv("COMMUNITY_ALLOWED_ORIGINS", "").split(",") if x)
        uvicorn.run(create_app(registry, allowed_origins=origins), host="127.0.0.1", port=8081, access_log=False)
    elif args.command == "add-key":
        if not args.id or not args.namespace or not args.public_key_file: parser.error("需要 id、namespace 和 public-key-file")
        registry.add_key(args.id, args.namespace, base64.b64decode(args.public_key_file.read_text().strip(), validate=True))
    else:
        if not args.id or not args.token_file: parser.error("需要 id 和 token-file")
        # 排他创建文件先于账号变更，防止覆盖现有凭据；命令不向标准输出泄漏令牌。
        fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            token = registry.add_principal(args.id, "author" if args.command == "create-author" else "moderator", args.namespace)
            stream.write(token)


if __name__ == "__main__": main()
