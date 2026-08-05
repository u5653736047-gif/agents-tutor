# 多智能体助教助学系统 - 前端镜像(多阶段)
#
# standalone 说明:项目当前没有 next.config(Next 16 默认配置),未启用
# output: "standalone",因此不走官方 standalone 产物,运行阶段直接复制
# 完整 node_modules(含 devDependencies 的 postcss/tailwind 等,避免
# next start 运行期解析缺失)。若后续启用 standalone,可改为只复制
# .next/standalone 与静态资源,镜像体积更小——需改前端生产配置,
# 由主线程定夺,本文件不擅自修改。

# ── 构建阶段:安装依赖并产出 .next 构建产物 ──
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# Next.js 在构建时把 NEXT_PUBLIC_* 内联进客户端产物,运行时改环境变量
# 不生效,因此 API 地址必须经构建参数(ARG)传入;docker-compose.yml 中
# frontend.build.args 会覆盖此默认值。
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

# ── 运行阶段:只保留产物,提供生产服务 ──
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production

COPY --from=build /app/package.json ./package.json
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
# 本项目没有 public/ 目录(Next 16 中可选),无需复制

EXPOSE 3000
CMD ["npm", "run", "start"]
