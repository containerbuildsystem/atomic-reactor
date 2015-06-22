## RPM build

Install tito and mock:

```bash
dnf install tito mock
```

Build RPM locally:

```bash
# build from the latest tagged release
tito build --rpm
# or build from the latest commit
tito build --rpm --test
```

Build RPM using mock:

```bash
SRPM=`tito build --srpm --test | egrep -o '/tmp/tito/atomic-reactor-.*\.src\.rpm'`
sudo mock -r fedora-21-x86_64 $SRPM
```

## Submit Build in Copr

First you need to set up rel-eng/releasers.conf:

```bash
sed "s/<USERNAME>/$USERNAME/" < rel-eng/releasers.conf.template > rel-eng/releasers.conf
```

Now you may submit build:

```bash
# submit build from latest commit
tito release copr-test
# or submit build from the latest tag
tito release copr
```


