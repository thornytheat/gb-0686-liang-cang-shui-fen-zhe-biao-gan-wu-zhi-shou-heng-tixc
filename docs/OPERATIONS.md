# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

退收冲销：草稿不落仓账；提交冲销为单事务，失败时退收单转“冲销失败”，仓批与原凭证保持旧版，可原地重试。`REVERSAL` 表示红冲未与原凭证对平，`STATE` 表示原凭证已被其他退收单冲销或仓批余额不足。重试同一退收单返回既有结果，不重复扣减。
