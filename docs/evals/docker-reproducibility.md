# Docker 容器化复现记录（20260915）

目的：同一份基准在容器内复现，验证"宿主机能跑通 = 容器能跑通"，并证明镜像不含任何私有数据。
任务书对应：phase-c 任务 A。所有数据由下方命令直接产生，未人工修改。

## 1. 构建

宿主机：Apple M2（8 核）+ macOS 26.6.2；容器运行时：colima 0.10.3（VM：Ubuntu 24.04，vz，
8 vCPU / 8GiB），docker server 29.5.2。

```bash
colima start --cpu 8 --memory 8 --vm-type vz
docker compose build            # 构建镜像 personal-brain:repro
```

| 指标 | 数值 |
| --- | --- |
| 镜像 | `personal-brain:repro` |
| 基础镜像 | `python:3.12-slim`（arm64） |
| 镜像大小 | **477MB** |
| 干净重建耗时（`docker compose build --no-cache`，基础镜像已拉取） | **26.07s** |
| 依赖安装 | `uv sync --frozen`（uv==0.12.7，按 `uv.lock` 精确安装，含 `.[dev]`） |
| 运行用户 | `app`，uid/gid 1000（非 root） |

说明：本机网络直连 Docker Hub/PyPI 会被污染或限速，构建时通过
`--build-arg HTTP_PROXY/HTTPS_PROXY` 走本机代理拉取依赖；这是环境相关的网络配置，
不是镜像内容的一部分（换网络环境无需该参数）。

Dockerfile 关键点（决策 37）：

- 只 `COPY pyproject.toml uv.lock src tests evals/suites config docs`，先装依赖再拷源码，源码层热重载快；
- `.dockerignore` 再排除 `evals/private/ *.sqlite *.zip .env.local .git codex_history` 等（与 COPY 白名单构成双保险）；
- 非 root 运行；数据库/合成档案写入 `/tmp/pb`（容器内临时目录）；
- `chmod a+rX` 归一化宿主机带入的 0600 文件权限（umask 所致，否则非 root 用户读不到）。

## 2. 私有数据验证

```console
$ docker run --rm --entrypoint sh personal-brain:repro -c 'ls evals; echo ---; ls evals/private; echo "exit=$?"'
suites
---
ls: cannot access 'evals/private': No such file or directory
exit=2
$ docker run --rm --entrypoint sh personal-brain:repro -c 'ls evals/suites'
dev-synthetic-policy.json
dev-synthetic.json
```

镜像内 `evals/` 只有合成的 `suites/`，`evals/private/` 不存在，`.env.local`、
`codex_history/`、`*.sqlite` 同样未进入镜像。

## 3. 测试对齐（容器 == 宿主机）

```bash
docker compose run --rm test      # 容器内 pytest
.venv/bin/python -m pytest        # 宿主机基线
```

| 环境 | 结果 |
| --- | --- |
| 宿主机（macOS 26.6.2 / Python 3.12.14） | `280 passed in 11.05s` |
| 容器（Ubuntu 24.04 VM / Python 3.12.14） | `280 passed, 1 warning in 20.06s` |

容器 `docker compose run --rm test` 完整尾部输出（exit 0）：

```text
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
................................................................         [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/_pytest/cacheprovider.py:469
  /app/.venv/lib/python3.12/site-packages/_pytest/cacheprovider.py:469: PytestCacheWarning: could not create cache path /app/.pytest_cache/v/cache/nodeids: [Errno 13] Permission denied: '/app/pytest-cache-files-0h15s0di'
    config.cache.set("cache/nodeids", sorted(self.cached_nodeids))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
280 passed, 1 warning in 20.06s
TEST_EXIT:0
```

唯一 warning 是非 root 容器内 pytest 无法写 `.pytest_cache`，与测试结果无关。

## 4. 容器内 bench 门槛

```bash
docker compose run --rm bench     # 100k 合成语料 + §9.2 查询，写 /out/benchmark-docker.json
```

```text
规模: {"sources": 1, "events": 100000, "event_revisions": 100000, "revision_occurrences": 100000, "event_revisions_fts": 100000}
门槛: 常用查询预热 P95 ≤ 1000ms（§9.2） → 通过 ✓
  search_2char_cjk_职业              P50    395.7ms  P95    413.7ms  P99    416.4ms  命中 10
  search_4char_cjk_职业方向            P50    295.3ms  P95    308.9ms  P99    332.0ms  命中 10
  search_mixed_AI工程                P50    359.6ms  P95    375.4ms  P99    531.7ms  命中 10
  search_punct_你好世界                P50    129.3ms  P95    134.1ms  P99    135.0ms  命中 10
  recent_7d                        P50    123.2ms  P95    128.2ms  P99    140.0ms  命中 10
  get_event_by_id                  P50    121.9ms  P95    149.9ms  P99    151.0ms  命中 1
  fallback_* / search_unique_token   P50      0.0ms  P95      0.0ms  P99      0.0ms  命中 0 [单独披露]
```

宿主机同口径参照（docs/evals/benchmark-100k.json，20260906 测得）：常用查询 P50 186–507ms、
P95 267–899ms。容器侧 P95 128–414ms，与宿主机同量级，门槛 `P95 ≤ 1000ms` 两边均通过。

## 5. 环境对比（collect_environment()）

| 字段 | 宿主机（benchmark-100k.json） | 容器（benchmark-docker.json） |
| --- | --- | --- |
| platform | macOS-26.6.2-arm64-arm-64bit | Linux-6.8.0-117-generic-aarch64-with-glibc2.41 |
| machine | arm64 | aarch64 |
| python | 3.12.14 | 3.12.14 |
| sqlite | 3.53.1 | 3.46.1 |
| cpu_count | 8 | 8（colima --cpu 8） |
| db_size_bytes | 292,843,520 | 292,843,520 |

同一份 `uv.lock` + 同一 CPU 核数下，两边语料规模与查询结果一致；SQLite 小版本差异
（3.53.1 → 3.46.1，slim 镜像自带 libsqlite3）不影响门槛判定。

## 6. 复现步骤（一条龙）

```bash
colima start --cpu 8 --memory 8 --vm-type vz   # 首次需联网拉取 VM 镜像
docker compose build                            # 产出 personal-brain:repro
docker compose run --rm test                    # 280 passed
docker compose run --rm bench                   # bench 门槛 通过，写 docs/evals/benchmark-docker.json
docker compose run --rm soak                    # ladder+soak 全量（约 1.5-2h），写 docs/evals/soak-docker.json
```

容器报告落盘到 `docs/evals/`（`benchmark-docker.json` / `soak-docker.json`），命名规则
见 `docs/evals/soak-20260915.md`。本次交付中 soak 为**缩短口径冒烟**
（`soak-20260915-docker-smoke.json`，levels 1,4 × 500 次/档 + 5 分钟 soak），全量命令同上。

## 7. 已知注意事项

- 构建时的代理 build-arg 只影响拉取依赖的网络路径，镜像本身不携带代理配置；
- 非 root 容器内 pytest 的 `.pytest_cache` 警告无害，可用 `--cache-clear` 或 `-p no:cacheprovider` 消除；
- 宿主机部分源文件为 0600（umask 所致），镜像内以 `chmod a+rX` 归一化，仓库文件权限未改动。
