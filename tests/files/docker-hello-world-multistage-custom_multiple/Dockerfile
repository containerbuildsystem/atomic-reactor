FROM fedora:latest as builder
RUN uname -a && env

FROM koji/image-build as custom
COPY --from=builder /etc/fedora-release /opt/release

FROM koji/image-build:different_config
COPY --from=custom /etc/fedora-release /opt/release
