FROM rust:1.82 AS builder
WORKDIR /usr/src/app

COPY Cargo.toml  ./
RUN mkdir -p src && echo "// placeholder" > src/lib.rs
RUN cargo fetch

COPY src ./src
RUN cargo build --release

FROM debian:12-slim
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /logs
COPY --from=builder /usr/src/app/target/release/servidor /app/servidor
ENV PORTA=7000 COMPANHEIROS=""
CMD ["/app/servidor"]
