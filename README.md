# 单卷备份 API（FastAPI）

面向“备份机拔盘后，原挂载路径可能落回系统盘或被替换盘占用”这一场景的单卷归档上传服务。
上传前核对目标目录中**预置的卷号**，上传全程通过**固定挂载点的文件描述符**做 I/O，
收齐时再次校验入口；入口不可用、卷号不符或设备已更换一律拒收，且绝不触碰替换后的目录。

此外提供**盘点（inventory）能力**：值班员凭盘号发起盘点，服务与挂载入口当前呈现的卷号
核对后，把该盘的归档清册（booklet）写入**独立于备份盘的持久目录**；备份盘下架后，仍可
凭归档 ID 反查应接回哪块盘。

## 1. 威胁模型与安全设计

| 风险 | 对策 |
| --- | --- |
| 上传前入口已被替换盘占用 | `POST /backups` 先做路径探针：入口必须可访问，且其中的 `.volume_id` 与 `BACKUP_VOLUME_ID` 完全一致，否则返回 409 `VOLUME_ID_MISMATCH`；入口无法 stat/open 时返回 503 `VOLUME_UNAVAILABLE` |
| 拔盘后路径落回系统盘空目录 | 探针要求卷号文件存在且内容匹配，空目录没有卷号文件，直接 409；入口整体消失则 503 |
| 上传途中换盘 | 会话创建时对入口、`.staging`、会话目录各打开一个目录 fd（`O_RDONLY\|O_DIRECTORY`）。这些 fd 会**钉住原挂载**，后续所有 `openat/mkdirat/renameat/unlinkat` 都经 `dir_fd` 解析，路径再怎么变，I/O 只落在原卷；原卷真的没了就得到 EIO/ENXIO（503 `VOLUME_IO_ERROR`），不会写到替换盘 |
| 收齐时入口已无法访问原卷 | `complete` 重新探针并比对 **卷号 + `st_dev` 设备号 +  pinned fd 的设备号**，任一不符统一报 409 `VOLUME_SWITCHED`，本次上传失败，原卷也不发布该归档 |
| 失败会话清理误删新盘 | 清理只通过会话保存的 pinned fd 进行（`unlinkat`/`rmdirat`），从不按路径重新解析；清理失败只吞错留待回收，绝不升级为路径 I/O |
| 半成品出现在清单里 | 归档先写 `*.tmp`，元数据 sidecar 先临时文件再原子 rename、数据文件再原子 rename；清单只统计同时具备 sidecar 与数据文件的条目，`.tmp` 和无 sidecar 文件一律不可见 |
| 原盘接回后查旧档 | 清单/下载同样先探针；原盘接回后卷号重新匹配，历史归档立即可查。换盘期间清单接口本身也会 409 拒绝，不会列出替换盘内容 |

## 2. 目录结构

```
app/
  volume.py      # 卷号/入口探针（probe 强制预置卷号；probe_any 只核对当前呈现卷号）
  store.py       # 上传会话、分片写入、收齐发布、清单/下载（全部 *at + dir_fd）
  inventory.py   # 盘点：盘号核对、清册快照/原子提交、归档反查索引、忙等控制
  schemas.py     # Pydantic 模型
  main.py        # FastAPI 路由
tests/           # pytest + TestClient（备份 11 用例 + 盘点 16 用例）
docker/
  entrypoint.sh  # 容器内把 disk-a bind mount 到备份入口
demo/
  prepare-disks.sh        # 预置两块“盘”的卷号
  demo.sh                 # 容器内特权演示（mount/umount 模拟拔插换盘）
  local-demo.sh           # 免特权本地演示（符号链接重指模拟换盘）
  inventory-demo.sh       # 容器内盘点演示（含拔盘后凭归档 ID 查盘号）
  inventory-local-demo.sh # 免特权本地盘点演示（含重启后反查）
  api-examples.sh         # 备份接口的纯 curl 示例
  inventory-api-examples.sh  # 盘点接口的纯 curl 示例（拔盘后查盘号）
Dockerfile
docker-compose.yml       # /backups 与 /inventory 是两个互相独立的持久卷
```

## 3. 快速开始

### 方式 A：Docker Compose（推荐，最贴近真实拔盘）

```bash
./demo/prepare-disks.sh          # 生成 demo/disks/disk-{a,b}/.volume_id
docker compose up --build -d
docker compose exec backup-api /srv/app/demo/demo.sh
docker compose exec backup-api /srv/app/demo/inventory-demo.sh
```

compose 以特权模式运行，仅用于演示脚本在容器内执行 `mount --bind`/`umount`；
应用本身不调用任何 mount 命令。`/backups/data` 是挂载点，disk-a 与 disk-b 是两个
host 目录模拟的物理盘。盘点清册写入独立命名卷 `inventory-data`（挂载在 `/inventory`），
与备份挂载 `/backups` 完全分离：拔盘不会清掉清册，重启容器（不加 `-v`）也不丢失。

### 方式 B：本地免特权演示（本仓库自测使用的方式）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./demo/local-demo.sh             # 自动起 uvicorn，跑完自动回收
```

### 方式 C：接口手工调用

服务启动后（本地演示默认端口见脚本，Compose 为 8080）：

```bash
./demo/api-examples.sh                       # 或按 README 第 5 节手敲 curl
curl -s http://localhost:8080/health | jq .
```

## 4. API 摘要

| 方法/路径 | 说明 | 关键错误码 |
| --- | --- | --- |
| `GET /health` | 始终 HTTP 200，体内容 `status=ok/degraded` 反映卷状态 | — |
| `POST /backups` | 创建上传会话；先核对入口卷号，再钉住挂载并建暂存目录 | 409 `VOLUME_ID_MISMATCH`、503 `VOLUME_UNAVAILABLE` |
| `PUT /backups/{sid}/chunks/{i}` | 上传一个分片，经 pinned fd 写原卷 | 409 `CHUNK_CONFLICT`、400 `CHUNK_OUT_OF_RANGE`、503 `VOLUME_IO_ERROR`、413 `CHUNK_TOO_LARGE` |
| `POST /backups/{sid}/complete` | 收齐校验（卷号/设备号/sha256/大小），原子发布 | 409 `VOLUME_SWITCHED`、422 `CHECKSUM_MISMATCH`/`SIZE_MISMATCH`、409 `CHUNKS_INCOMPLETE` |
| `DELETE /backups/{sid}` | 中止会话，pinned fd 清理暂存 | 404 `SESSION_NOT_FOUND` |
| `GET /backups` | 清单，仅含完整发布的归档 | 409/503 同卷错误 |
| `GET /backups/archive/{id}/download` | 下载已发布归档 | 404 `ARCHIVE_NOT_FOUND` |
| `POST /inventory/volumes/{盘号}` | 发起盘点；盘号须与入口当前卷号一致，清册写入独立持久目录 | 409 `VOLUME_ID_MISMATCH`/`INVENTORY_BUSY`/`VOLUME_SWITCHED`、503 `VOLUME_UNAVAILABLE`/`INVENTORY_READ_FAILED`/`INVENTORY_WRITE_FAILED` |
| `GET /inventory/volumes/{盘号}/booklet` | 读取该盘最近一次成功盘点的清册（只读持久目录，拔盘可查） | 404 `BOOKLET_NOT_FOUND` |
| `GET /inventory/volumes` | 列出所有已有清册的盘 | — |
| `GET /inventory/archives/{归档ID}/volumes` | 反查哪些盘的最近一次成功盘点收录了该归档 | 400 `INVALID_ARCHIVE_ID`；无收录时 `volumes: []` |

错误体统一为 `{"error": "<CODE>", "detail": "<message>"}`。

## 4A. 盘点设计（凭归档 ID 查应接哪块盘）

| 需求 | 实现 |
| --- | --- |
| 盘号与挂载入口卷号核对 | `POST /inventory/volumes/{盘号}` 先用 `probe_any()` 读入口当前呈现的卷号（**不**套用上传的预置卷号约束），与传入盘号不一致即 409 `VOLUME_ID_MISMATCH`。探针返回的 fd 钉住当前挂载，整个读取经 `dir_fd` 解析，换盘不会把读取引到替换目录 |
| 清册独立于备份盘持久化 | 清册写入 `INVENTORY_ROOT`（Compose 中为独立命名卷 `/inventory`，默认本地为 `/inventory`），路径 `<root>/volumes/<卷号>/booklet.json`；备份盘被拔走后这些文件不受影响 |
| 只收录盘点开始时已发布的归档 | 读取开始时一次性 `scandir` 冻结 sidecar 名单，这就是本次清册的边界；读取元数据期间上传照常发布，新档的 sidecar 不在名单内，留到下次盘点。上传路径不被盘点阻塞 |
| 同一盘同时只能有一个盘点 | 内存中按卷号维护忙等槽位；第二个请求直接 409 `INVENTORY_BUSY`，无论成功失败槽位都会在 `finally` 中释放 |
| 提交前再次确认入口 | 读取结束、写清册之前重新探针，要求入口仍呈现同一卷号且 `st_dev` 与读取时一致；换盘/入口消失统一报 409 `VOLUME_SWITCHED`，本次不提交 |
| 换盘、读取或保存失败沿用上次清册 | 新清册只通过“临时文件 + 原子 rename + fsync”发布；读取失败根本不写，保存失败只留下临时文件，旧 `booklet.json` 原样保留 |
| 凭归档 ID 反查盘号 | 维护持久反向索引 `archive-index.json`（归档 ID → 收录它的各盘最近成功清册）。查询只访问持久目录：盘点未提交时返回上一版结果、拔盘后可查、服务重启后可查；索引文件缺失或损坏时从全部清册自动重建 |
| 上传仍只受理原先配置的卷 | 上传链路继续使用强制预置卷号的 `probe()`；盘点用的 `probe_any()` 不会放宽上传校验，换上来的 B 盘上创建上传仍是 409 |

清册 JSON 结构：

```json
{
  "version": 1,
  "inventory_id": "本次盘点ID",
  "volume_id": "盘号/卷号",
  "started_at": 1732348800.12,
  "committed_at": 1732348802.34,
  "archive_count": 1,
  "archives": [
    {"id": "归档ID", "filename": "...", "sha256": "...", "size": 123,
     "volume_id": "...", "created_at": 0.0, "uploaded_at": 0.0}
  ]
}
```

拔盘后查盘号的最小调用：

```bash
# 盘点（盘在位时做，可定期重复；每次只收录当时已发布的归档）
curl -sS -X POST http://localhost:8080/inventory/volumes/VOL-DISK-A-0001
# 拔盘之后，值班员凭归档 ID 查应接回哪块盘：
curl -sS http://localhost:8080/inventory/archives/<归档ID>/volumes
# -> {"archive_id":"...","volumes":[{"volume_id":"VOL-DISK-A-0001", ...}]}
```

更多完整示例见 `demo/inventory-api-examples.sh`。

### curl 示例

```bash
# 1) 创建会话（total_chunks/sha256/size 为整包归档的元数据）
SID=$(curl -s -X POST http://localhost:8080/backups \
  -H 'Content-Type: application/json' \
  -d '{"filename":"demo.tar","total_chunks":2,"sha256":"<64位hex>","size":11}' \
  | jq -r .session_id)

# 2) 逐片上传
curl -s -X PUT http://localhost:8080/backups/$SID/chunks/0 --data-binary @part-0
curl -s -X PUT http://localhost:8080/backups/$SID/chunks/1 --data-binary @part-1

# 3) 收齐发布
curl -s -X POST http://localhost:8080/backups/$SID/complete

# 4) 清单 / 下载
curl -s http://localhost:8080/backups | jq .
curl -OJ http://localhost:8080/backups/archive/<archive-id>/download
```

## 5. 换盘演示会验证的断言

`demo/demo.sh`（容器）与 `demo/local-demo.sh`（本地）按同一剧本执行：

1. 先换成 B 盘再创建会话 → 409，B 盘目录除卷号文件外无任何变化；
2. 卸载入口（路径落回系统盘空目录）→ 503；
3. 接回 A 盘，上传 2/4 分片后拔 A 插 B → 剩余分片仍经 pinned fd 写在 A 上，B 盘零变化；
   `complete` 返回 409 `VOLUME_SWITCHED`，清单经 B 盘访问同样 409；
4. 重新接回 A 盘 → 失败归档不在清单、暂存已回收；正常上传可以完整发布并逐字节回读；
5. （测试套件额外覆盖）旧归档在重新接回后仍可清单查询和下载。

### 盘点演示（`demo/inventory-demo.sh` / `demo/inventory-local-demo.sh`）

1. 发布一个归档并按盘号盘点 → 201，清册落在独立持久目录，备份盘上没有清册文件；
2. 盘号与入口卷号不符 → 409 `VOLUME_ID_MISMATCH`；
3. 拔盘 → 上传被拒（503），但凭归档 ID 反查盘号、读取旧清册仍正常返回；
4. 无盘时发起盘点 → 503，且上次清册保持不变；
5. 插入 B 盘可盘点 B（空清册），但上传仍只受理预置的 A 盘（409）；
6. 重新接回 A 盘 → 再次盘点成功并生成新的盘点 ID；
7. 重启服务并删除反向索引文件 → 索引从清册自动重建，反查结果不变。

## 6. 测试

```bash
pip install pytest httpx
pytest -q
```

备份用例覆盖：正常发布/回读、卷号不符拒收、入口不可用拒收、上传途中换盘、换盘后重接恢复、
sha256/大小不符、重复/越界分片、未收齐禁止 complete、清单过滤 `.tmp`/孤儿文件、
health 状态、`st_dev` 设备号不符（tmpfs 用例，无 `CAP_SYS_ADMIN` 时自动 skip）。

盘点用例（`tests/test_inventory_api.py`）覆盖：清册落独立目录与完整元数据、盘号核对（
含非法字符路径）、入口不可用、同盘并发盘点报忙且失败后释放槽位、读取期间新发布归档留待
下次盘点且上传不中断、提交前换盘拒收并保留旧清册、读取/保存失败保留旧清册、盘点未提交时
与拔盘后的反查、删索引后重启自动重建、多盘索引、上传仍被限制在预置卷。

## 7. 已知限制 / 剩余工作

- 会话状态与盘点忙等槽位在内存中，多副本部署需换成共享存储（Redis/DB）；清册和反向索引
  本身在共享持久卷上，重启不丢。单实例重启后未完成会话的暂存目录需要回收任务扫描
  `.staging`。
- 盘点为单线程串行读取元数据（每个卷同一时刻只允许一个盘点），归档数量极大时耗时随
  sidecar 数量线性增长；后续可加分页/增量盘点。
- 分片为整段读入内存，受 `BACKUP_MAX_CHUNK_BYTES`（默认 64 MiB）限制；超大归档应配合
  反向代理的请求体限制调优，后续可改为流式落盘。
- 卷号信任来自卷根预置文件，若攻击者可写该文件应配合文件权限/只读挂载或签名卷号。
- 本地演示用符号链接换盘，验证的是卷号校验与 pinned-fd 路径解析；真实 `st_dev` 变化的
  设备级换盘由测试中的 tmpfs 用例与 Compose 演示覆盖。
- 未包含鉴权/限流/TLS，生产部署应放在网关之后。
