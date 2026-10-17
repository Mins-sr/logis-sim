FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY package*.json ./
RUN npm install
COPY vite.config.ts tsconfig.json ./
COPY src/frontend ./src/frontend
RUN npm run build

FROM nginx:1.27-alpine
COPY src/proxy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /frontend/dist /usr/share/nginx/html
