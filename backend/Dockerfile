# 多智能体助教助学系统 - 后端镜像
#
# 构建方式说明:项目标准依赖管理是 uv(见 backend/pyproject.toml 与
# README),但 slim 镜像内再装 uv 会显著增加体积;这里用 pip 安装
# pyproject.toml(hatchling 构建后端由 pip 的 PEP 517 构建隔离自动准备),
# 并一次性带上 [embedding] 可选组(fastembed),容器内语义检索开箱可用
# (API_KNOWLEDGE_EMBEDDING=auto 时优先真实语义模型)。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── 依赖层:先复制 pyproject.toml 与空 src 占位包安装全部依赖 ──
# hatchling 构建要求 packages(src/core、src/api)存在,先放空包占位,
# 装完依赖再删掉;这样业务代码改动时不会重装依赖层(Docker 层缓存)。
COPY pyproject.toml ./
RUN mkdir -p src/core src/api \
    && touch src/core/__init__.py src/api/__init__.py \
    && pip install --no-cache-dir ".[embedding]" \
    && rm -rf src

# ── 源码层:复制真实代码(backend/src 下的 api/ 与 core/)──
COPY src ./src

EXPOSE 8000

# 与 scripts/start-stage3.ps1 同一启动方式:app 对象由工厂函数
# create_app() 创建,uvicorn 必须带 --factory;容器内监听 0.0.0.0。
CMD ["uvicorn", "api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
