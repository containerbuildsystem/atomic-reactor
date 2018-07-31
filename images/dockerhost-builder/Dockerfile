FROM fedora:latest
RUN dnf -y install docker git python-docker-py python-setuptools desktop-file-utils e2fsprogs flatpak koji libmodulemd ostree python2-gobject-base python2-flatpak-module-tools python-backports-lzma osbs gssproxy && dnf clean all
ADD ./atomic-reactor.tar.gz /tmp/
RUN cd /tmp/atomic-reactor-*/ && python setup.py install
CMD ["atomic-reactor", "--verbose", "inside-build"]
