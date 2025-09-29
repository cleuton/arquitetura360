# Build
FROM golang:1.23.2 as builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o servidor main.go

# Runtime
FROM debian:12-slim
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/servidor /app/servidor
ENV PORTA=6000 COMPANHEIROS=""
CMD ["/app/servidor"]
