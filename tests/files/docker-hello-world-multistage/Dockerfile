FROM fedora:latest as builder
RUN uname -a && env

FROM fedora
COPY --from=builder /etc/fedora-release /opt/release
