FROM public.ecr.aws/docker/library/node:22-alpine AS build

# Use the Tsinghua University mirror for Alpine packages.
RUN sed -i \
    's#https\?://dl-cdn.alpinelinux.org/alpine#https://mirrors.tuna.tsinghua.edu.cn/alpine#g' \
    /etc/apk/repositories

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM public.ecr.aws/docker/library/nginx:1.27-alpine

# Keep the runtime image on the same Tsinghua University Alpine mirror.
RUN sed -i \
    's#https\?://dl-cdn.alpinelinux.org/alpine#https://mirrors.tuna.tsinghua.edu.cn/alpine#g' \
    /etc/apk/repositories

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
