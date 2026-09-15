FROM node:22-alpine AS build
WORKDIR /app
COPY apps/control-room/package.json apps/control-room/package-lock.json ./
RUN npm ci
COPY apps/control-room .
ARG NEXT_PUBLIC_DISPATCH_API
ENV NEXT_PUBLIC_DISPATCH_API=$NEXT_PUBLIC_DISPATCH_API
RUN npm run build
FROM node:22-alpine
WORKDIR /app
COPY --from=build /app ./
ENV PORT=3100
EXPOSE 3100
CMD ["sh", "-c", "npx next start -p ${PORT}"]
