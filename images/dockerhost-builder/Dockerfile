FROM fedora:latest
RUN dnf -y install docker git python-docker-py python-setuptools desktop-file-utils e2fsprogs flatpak koji ostree python2-libmodulemd python2-flatpak-module-tools >= 0.10.0 python-backports-lzma osbs gssproxy && dnf clean all
ADD ./atomic-reactor.tar.gz /tmp/
RUN cd /tmp/atomic-reactor-*/ && python setup.py install
CMD ["atomic-reactor", "--verbose", "inside-build"]
