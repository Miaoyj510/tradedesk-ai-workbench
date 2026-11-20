# 本地构建 pgvector 镜像（Docker Hub 不可达时的替代路径）
# 构建：docker build -t tradedesk/pgvector:pg16 -f docker/pgvector/Dockerfile .
# 说明：以本机已缓存的 postgres:16-alpine 为基底，用 Alpine community 源安装扩展。
# 若该源仍不可达，则改为从源码编译（需 github 可达）：
#   RUN apk add --no-cache build-base git postgresql16-dev clang21 llvm21 && \
#   git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector /tmp/pgvector && \
#   cd /tmp/pgvector && make && make install && rm -rf /tmp/pgvector
FROM postgres:16-alpine

RUN apk add --no-cache postgresql16-pgvector || \
    (apk add --no-cache build-base git postgresql16-dev clang21 llvm21 && \
     git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector /tmp/pgvector && \
     cd /tmp/pgvector && make && make install && rm -rf /tmp/pgvector && \
     apk del build-base git postgresql16-dev clang21 llvm21)

# 说明：PGXS 默认会额外生成 .bc 位码（给 JIT 内联用），那一步需要与 PostgreSQL 构建时配套的
# LLVM 版本（本镜像的 pg_config 指定 LLVM 21）。Alpine 源里没有 postgresql16-pgvector，但有 clang21/llvm21，
# 装上它们就能完成构建（PGXS 的位码规则无法用变量关掉，NO_BC=1 对它无效）。
