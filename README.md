# 考古地层对接 API（FastAPI + SQLite）

管理同一考古遗址最多 40 个层位的探方编号、层位编号与可能沉积年代段（整年闭区间，
可含空档），登记跨探方层位对接与“下层 → 上层”叠压关系，并自动完成：

- **分组**：直接或间接对接的层位视为同一次沉积，合并为连通组，全组共用一个年份；
- **成环检测**：合组后的叠压关系（含同组自环）不得成环；
- **年代约束**：下层沉积年严格早于上层；同组年份必须落在各成员年代段的交集内；
- **可行年代段求解**：对每个组给出可与其他全部层位约束同时成立的所有年份
  （保留空档的闭区间列表）；
- **撤销重算**：删除对接/叠压后按剩余关系重新分组、重新求解。

## 快速开始（docker compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs   （可直接在页面上录入数据）
# ReDoc:    http://localhost:8000/redoc
```

SQLite 数据文件保存在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`），
`docker compose down` 不会删除；如需清空：`docker compose down -v`。

## 本地裸跑

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认在 ./data/archaeo.db，可用环境变量 DB_PATH 覆盖
```

## 年代表示约定

- 年份均为整数；公元后用正整数，公元前用负整数（如 `-221` 表示公元前 221 年）。
- 每个层位给一个或多个闭区间 `[year_from, year_to]`，多段之间允许空档
  （如 `[-5000,-4000]` 与 `[-3000,-2000]`，中间一千年无沉积可能）。
- 同组年代域 = 组内各层位区间的交集；求解结果 `feasible_years` 同样是带空档的区间列表。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites` | 建遗址 |
| GET | `/sites` / `/sites/{id}` | 遗址列表/详情 |
| POST | `/sites/{id}/strata` | 录入层位（探方、层位号、年代段；每遗址上限 40） |
| GET | `/sites/{id}/strata` / `/strata/{id}` | 层位查询 |
| POST | `/sites/{id}/correlations` | 登记两探方层位对接 |
| GET | `/sites/{id}/correlations` | 对接列表 |
| DELETE | `/correlations/{id}` | 撤销对接，按剩余对接重算 |
| POST | `/sites/{id}/superpositions` | 登记叠压（下层 → 上层） |
| GET | `/sites/{id}/superpositions` | 叠压列表 |
| DELETE | `/superpositions/{id}` | 撤销叠压并重算 |
| GET | `/sites/{id}/solution` | 整站分组、组间边、各组全部可行年代段 |
| GET | `/strata/{id}/correlation-view` | 按原始层位查同组成员与可行年代段 |
| GET | `/sites/{id}/label-versions` | 号表版本列表（含现行版 `is_current`） |
| GET | `/sites/{id}/label-versions/{version}` | 某一历史版号表的完整编号 |
| POST | `/sites/{id}/label-versions/renumber?version=N` | 提交一批层位改号，成功当次返回新版号 |
| GET | `/sites/{id}/labels?version=N&trench=T1&layer_no=L2` | 按版号 + 探方 + 层号查当时层位及现层号 |

## 号表（标签）与批量改号

样袋上印的是归档当时的探方与层号；整理时同一探方内的若干层号可能需要（部分）
对调。系统为每个遗址维护一条只增不减的**号表版本链**：

- 库中该遗址首次出现层位时，以现有编号建立第 1 版（初版）；旧库升级后首次
  启动时，同样以现有编号补建初版。
- 每次**新增层位**成功即生成新一版号表；每次**批量改号**成功也生成新一版。
  两处均按提交顺序在同一事务内生效，返回当次新版号。
- 版本表是整表快照（层位 id ↔ 探方/层号）。层位 id、年代段、对接与叠压关系
  始终指向原层位，改号只改标签，不改变分组与年代结论。
- 批量改号只作用于一个探方：**按整批改完后的结果**检查该探方内是否重号，
  因此批内互换（如 L2/L3 对调）合法；改完仍有重号则整批拒绝。
- 提交时必须带上查询到的现行版号：版号过期、改完仍有重号或写入失败时返回
  `409`（`reason` 分别为 `STALE_LABEL_VERSION` / `DUPLICATE_LAYER_NO` /
  `LABEL_WRITE_FAILED`），现行号表与历史均保持原样。
- 历史版号永久可查，重启后依然有效；按旧版号查询层号时，即使该旧号现在已
  给另一层使用，也会按版号快照返回原层位，并附上它现在的层号。

```bash
# 查看现行版号
curl localhost:8000/sites/1/label-versions
# 在现行版 v1 上把 T1 的 L2/L3 对调（整批提交，按改完后的结果查重号）
curl -X POST 'localhost:8000/sites/1/label-versions/renumber?version=1' \
  -H 'Content-Type: application/json' -d '{
    "trench": "T1",
    "entries": [{"stratum_id": 5, "layer_no": "L3"},
                {"stratum_id": 6, "layer_no": "L2"}]
  }'
# 按归档时的版号查样袋号：即使旧号已给另一层使用，仍返回原层位
curl 'localhost:8000/sites/1/labels?version=1&trench=T1&layer_no=L2'
```

### 录入示例

```bash
SID=1
# 层位：探方 T1 第 3 层，两个可能年代段（中间有空档）
curl -X POST localhost:8000/sites/$SID/strata -H 'Content-Type: application/json' -d '{
  "trench": "T1", "layer_no": "L3",
  "intervals": [{"year_from": -5000, "year_to": -4000},
                {"year_from": -3000, "year_to": -2000}]
}'
# 叠压：下层 1 -> 上层 2
curl -X POST localhost:8000/sites/$SID/superpositions \
  -H 'Content-Type: application/json' -d '{"lower_id": 1, "upper_id": 2}'
# 跨探方对接：层位 1 与层位 3 属同一次沉积
curl -X POST localhost:8000/sites/$SID/correlations \
  -H 'Content-Type: application/json' -d '{"stratum_a_id": 1, "stratum_b_id": 3}'
```

## 无解时的行为

写入会在**同一事务**内先重算再提交。若新记录导致无解（`EMPTY_DOMAIN` /
`GROUP_CYCLE` / `ORDER_CONFLICT`），事务回滚，接口返回 `409`，响应体包含：

- `contradiction`：一组本身即矛盾的**原始记录见证**（层位年代段、对接、叠压，
  含可读原因与记录 id，便于定位）；
- `previous_solution`：回滚后库内已有对接与年代结论（保持可行、保持可用）。

## 一致性与并发

- 所有写操作经进程内同一把锁串行化，按**提交顺序**核算；每次成功写入使全局
  `version` 加 1，查询结果带 `version`，可据此确认读到的是哪一次已完成提交。
- 分组与年代结论**不物化落库**，每次查询及重启都由原始记录现场求解；
  原始记录由 SQLite 事务保证原子性，因此任何查询看到的分组/年代都来自同一次
  完整提交，不存在半写入的中间态。
- SQLite 启用 WAL 与外键约束，连接带 30s busy timeout。

## 求解算法（`app/solver.py`）

1. 并查集按对接关系求连通组；组域为成员年代域交集（支持空档的区间运算）。
2. 叠压边提升为组间边；Kahn 拓扑排序判环，DFS 三色标记提取具体环与见证。
3. DAG 上两次贪心：
   - 最早解 E：拓扑序每组取域内 `> max(E[前驱])` 的最小年份；
   - 最晚解 L：逆拓扑序每组取域内 `< min(L[后继])` 的最大年份。
   任意可行解 F 都满足 E ≤ F ≤ L，故组 g 的全部可同时成立年份恰为
   其年代域 ∩ [E[g], L[g]]（仍可为含空档的多段区间）。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖区间工具、缺口域求解、成环/空域/次序冲突及矛盾见证、40 层上限、
无解拒绝与旧结论保留、撤销重算、并发写入串行化、重启后结论恢复。
