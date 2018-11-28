FROM fedora:latest as builder
RUN uname -a && env

FROM koji/image-build
COPY --from=builder /etc/fedora-release /opt/release
