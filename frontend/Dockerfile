# ============================================================
# AudioGraphy frontend image (dev target)
# Multi-stage:
#   deps   — install node_modules (cached layer)
#   dev    — Vite dev server with HMR, bind-mounted src/
#   prod   — Vite build + nginx serve (M3+)
# ============================================================

# ---- deps stage ----
FROM node:22.22-slim AS deps
WORKDIR /app

# Copy manifests first for layer caching
COPY package.json package-lock.json ./
RUN npm ci

# ---- dev stage ----
FROM node:22.22-slim AS dev
WORKDIR /app

# Bring installed node_modules from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Source code is bind-mounted in docker-compose for HMR
COPY . .

EXPOSE 5173

# Vite dev server — bind all interfaces for docker port mapping
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# ---- prod stages ---------------------------------------------------
# 生产/私有化交付形态:构建一次,nginx 静态服务 + 同源反代。样式与资产
# 全部在镜像里(系统字体栈、Arco 图标打包为组件),离线环境零外部依赖
# ——dev target 的 Vite 服务器只属于开发,不是交付物。
FROM node:22.22-slim AS prod-builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM nginx:1.29-alpine AS prod
COPY --from=prod-builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
