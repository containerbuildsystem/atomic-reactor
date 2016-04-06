# Testing Atomic Reactor (in OSBS)

## Creating build image

### Dockerfile

If you would like to test multiple PRs at once, put them to branch next.

```dockerfile
FROM $image

# el7
RUN yum -y update && yum -y install git koji python-setuptools docker docker-python python-pip

# fedora
RUN yum -y update && yum -y install git koji python-setuptools docker python-docker-py python-pip

# let's be cutting edge and use git version of squash tool
# use whatever branch of upstream atomic-reactor/osbs-client repo you want
RUN pip install git+https://github.com/goldmann/docker-squash && \
    cd /opt/ && git clone [-b next] https://github.com/projectatomic/atomic-reactor.git && cd atomic-reactor && python setup.py install && \
    cd /opt/ && git clone https://github.com/projectatomic/osbs.git && cd osbs && python setup.py install

CMD ["atomic-reactor", "--verbose", "inside-build", "--input", "osv3"]
```

### Testing in osbs

```shell
INSTANCE="..."
COMPONENT=""
DISTGIT=""
DISTGIT_BRANCH=""
KOJI_TARGET=""
osbs --instance $INSTANCE build -g ${GIT}${COMPONENT} -c $COMPONENT -t $KOJI_TARGET -u me --git-commit $DISTGIT_BRANCH
```
