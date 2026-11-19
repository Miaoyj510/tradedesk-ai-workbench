# 本地构建 pgvector 镜像（Docker Hub 不可达时的替代路径）
# 构建：docker build -t tradedesk/pgvector:pg16 -f docker/pgvector/Dockerfile .
# 说明：以本机已缓存的 postgres:16-alpine 为基底，用 Alpine community 源安装扩展。
# 若该源仍不可达，则改为从源码编译（需 github 可达）：
#   RUN apk add --no-cache build-base git postgresql16-dev && \
#   git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector /tmp/pgvector && \
#   cd /tmp/pgvector && make && make install && rm -rf /tmp/pgvector
FROM postgres:16-alpine

RUN apk add --no-cache postgresql16-pgvector || \
    (apk add --no-cache build-base git postgresql16-dev && \
     git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector /tmp/pgvector && \
     cd /tmp/pgvector && make && make install && rm -rf /tmp/pgvector && \
     apk del build-base git postgresql16-dev)