FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY config/frontend/package*.json ./
RUN npm ci
COPY config/frontend/vite.config.ts config/frontend/tsconfig.json ./
COPY src/frontend ./source
RUN npm run build

FROM nginx:1.27-alpine
COPY src/proxy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /frontend/dist /usr/share/nginx/html
